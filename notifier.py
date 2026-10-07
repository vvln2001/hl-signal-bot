"""Telegram sender with a queue (<=1 msg/s), retries, and log-only fallback."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from logging.handlers import RotatingFileHandler

import aiohttp

log = logging.getLogger("notifier")


class Notifier:
    def __init__(self, cfg: dict, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.session = session
        self.q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.log_only = cfg["log_only"]
        self.chat_id = str(cfg.get("telegram_chat_id") or "")
        self.bound_from_start = False
        if not self.chat_id and not self.log_only:
            try:  # previously bound owner, saved in the state JSON
                with open(cfg["state_file"], encoding="utf-8") as f:
                    self.chat_id = str(json.load(f).get("telegram_chat_id") or "")
                self.bound_from_start = bool(self.chat_id)
            except (OSError, ValueError):
                pass
        self.sent = 0
        self.failed = 0
        self.slog = logging.getLogger("signals")
        self.slog.propagate = False
        if not self.slog.handlers:
            h = RotatingFileHandler(cfg["signals_log"], maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self.slog.addHandler(h)
            self.slog.setLevel(logging.INFO)

    def record(self, text: str, kind: str = "SIGNAL") -> None:
        """Always written to signals.log (plain text)."""
        plain = re.sub(r"<[^>]+>", "", text)
        self.slog.info("[%s]\n%s\n", kind, plain)

    def send(self, text: str, silent: bool = False, kind: str = "SIGNAL") -> None:
        self.record(text, kind)
        if self.log_only:
            print(f"\n===== [{kind}] (log-only) =====\n{re.sub(r'<[^>]+>', '', text)}\n", flush=True)
            return
        try:
            self.q.put_nowait((text, silent))
        except asyncio.QueueFull:
            log.error("telegram queue full, dropping message")

    async def run(self) -> None:
        if self.log_only:
            log.warning("TELEGRAM_BOT_TOKEN missing (or LOG_ONLY=1): log-only mode")
            return
        api = f"{self.cfg.get('telegram_api', 'https://api.telegram.org')}/bot{self.cfg['telegram_token']}"
        url = api + "/sendMessage"
        if not self.chat_id:
            await self.bind_owner(api)
        else:
            log.info("Telegram chat id: %s%s", self.chat_id, " (bound earlier via /start)" if self.bound_from_start else "")
        while True:
            text, silent = await self.q.get()
            ok = False
            for attempt in range(6):
                try:
                    async with self.session.post(url, json={
                        "chat_id": self.chat_id, "text": text[:4000],
                        "parse_mode": "HTML", "disable_web_page_preview": True,
                        "disable_notification": silent,
                    }, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        data = await r.json(content_type=None)
                        if r.status == 200 and data.get("ok"):
                            ok = True
                            break
                        if r.status == 429:
                            wait = (data.get("parameters") or {}).get("retry_after", 5)
                            log.warning("telegram 429, retry after %ss", wait)
                            await asyncio.sleep(wait + 0.5)
                            continue
                        if r.status == 400 and "parse" in str(data.get("description", "")).lower():
                            text = re.sub(r"<[^>]+>", "", text)  # fall back to plain text
                            continue
                        log.warning("telegram error %s %s", r.status, data)
                        if r.status in (401, 403, 404):
                            break
                except Exception as e:  # network
                    log.warning("telegram send failed (%s): %s", attempt, e)
                await asyncio.sleep(min(2 ** attempt, 30))
            if ok:
                self.sent += 1
            else:
                self.failed += 1
                log.error("telegram message dropped after retries")
            await asyncio.sleep(1.05)  # <= 1 msg/s

    async def bind_owner(self, api: str) -> None:
        """No TELEGRAM_CHAT_ID: long-poll getUpdates until a *private* chat sends /start,
        bind to it (saved as telegram_chat_id in the state JSON), confirm, and from then on
        send only there. Signals produced meanwhile wait in the queue."""
        log.warning("TELEGRAM_CHAT_ID not set: waiting for /start in a private chat with the bot ...")
        offset = None
        while not self.chat_id:
            try:
                params = {"timeout": 30, "allowed_updates": json.dumps(["message"])}
                if offset is not None:
                    params["offset"] = offset
                async with self.session.get(api + "/getUpdates", params=params,
                                            timeout=aiohttp.ClientTimeout(total=45)) as r:
                    data = await r.json(content_type=None)
                if not data.get("ok"):
                    log.warning("getUpdates error: %s", data)
                    await asyncio.sleep(10 if r.status != 409 else 30)
                    continue
                for u in data.get("result", []):
                    offset = u["update_id"] + 1
                    m = u.get("message") or {}
                    chat = m.get("chat") or {}
                    text = (m.get("text") or "").strip()
                    if chat.get("type") == "private" and text.split("@")[0].split(" ")[0] == "/start":
                        self.chat_id = str(chat["id"])
                        break
                    log.info("ignoring update from chat %s (%s): not a private /start", chat.get("id"), chat.get("type"))
            except Exception as e:
                log.warning("getUpdates failed: %s", e)
                await asyncio.sleep(5)
        # acknowledge the consumed updates so they are not delivered again
        try:
            async with self.session.get(api + "/getUpdates", params={"offset": offset, "timeout": 0},
                                        timeout=aiohttp.ClientTimeout(total=15)):
                pass
        except Exception:
            pass
        self.save_chat_id()
        log.info("Telegram bound to private chat %s", self.chat_id)
        for attempt in range(5):
            try:
                async with self.session.post(api + "/sendMessage", json={"chat_id": self.chat_id,
                                             "text": "已绑定，开始推送信号"},
                                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        break
            except Exception as e:
                log.warning("bind confirmation failed: %s", e)
            await asyncio.sleep(2 ** attempt)
        await asyncio.sleep(1.05)

    def save_chat_id(self) -> None:
        """Merge telegram_chat_id into the state JSON (Monitor.save_state keeps it too)."""
        p = self.cfg["state_file"]
        try:
            with open(p, encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        st["telegram_chat_id"] = self.chat_id
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, separators=(",", ":"))
        os.replace(tmp, p)
