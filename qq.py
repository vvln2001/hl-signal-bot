"""QQ official bot (bot.q.qq.com, API v2) group broadcaster.

Env:
  QQ_BOT_APPID, QQ_BOT_SECRET   -> enable QQ push (both required)
  QQ_GROUP_OPENID (optional)    -> fixed target group; otherwise the bot binds to the
                                   first group that adds it or @-mentions it ("绑定").
The group owner must turn on 「机器人主动在群聊内发言」 for proactive messages.
Links are stripped (QQ rejects unapproved URLs). Max ~18 msgs/min per group.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import aiohttp

log = logging.getLogger("qq")
API = "https://api.bot.qq.com"
TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
INTENTS = 1 << 25  # GROUP_AND_C2C_EVENT


def to_plain(text: str) -> str:
    t = re.sub(r"<[^>]+>", "", text)
    t = t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    t = re.sub(r"^.*https?://\S+.*$\n?", "", t, flags=re.M)  # drop link lines
    return t.strip()[:1800]


class QQGroup:
    def __init__(self, cfg: dict, session: aiohttp.ClientSession):
        self.cfg, self.session = cfg, session
        self.appid = os.environ.get("QQ_BOT_APPID", "").strip()
        self.secret = os.environ.get("QQ_BOT_SECRET", "").strip()
        self.enabled = bool(self.appid and self.secret)
        self.group = os.environ.get("QQ_GROUP_OPENID", "").strip()
        if self.enabled and not self.group:
            try:
                with open(cfg["state_file"], encoding="utf-8") as f:
                    self.group = str(json.load(f).get("qq_group_openid") or "")
            except (OSError, ValueError):
                pass
        self.q: asyncio.Queue = asyncio.Queue(maxsize=300)
        self._tok, self._tok_exp = "", 0.0
        self.sent = self.failed = 0
        self._seq = None

    # ------------------------------------------------------------ public
    def send(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            self.q.put_nowait(to_plain(text))
        except asyncio.QueueFull:
            log.error("qq queue full, dropping message")

    async def run(self) -> None:
        if not self.enabled:
            return
        log.info("QQ enabled (appid %s), group %s", self.appid, self.group or "not bound yet: add the bot to your group or @it with 绑定")
        await asyncio.gather(self.gateway_loop(), self.sender_loop())

    # ------------------------------------------------------------ auth
    async def token(self) -> str:
        if self._tok and time.time() < self._tok_exp - 90:
            return self._tok
        async with self.session.post(TOKEN_URL, json={"appId": self.appid, "clientSecret": self.secret},
                                     timeout=aiohttp.ClientTimeout(total=15)) as r:
            d = await r.json(content_type=None)
        if not d.get("access_token"):
            raise RuntimeError(f"QQ token error: {d.get('code')} {d.get('message')}")
        self._tok, self._tok_exp = d["access_token"], time.time() + int(d.get("expires_in", 7200))
        return self._tok

    async def api(self, method: str, path: str, body=None):
        h = {"Authorization": f"QQBot {await self.token()}"}
        async with self.session.request(method, API + path, json=body, headers=h,
                                        timeout=aiohttp.ClientTimeout(total=15)) as r:
            return r.status, await r.json(content_type=None)

    # ------------------------------------------------------------ sending
    async def sender_loop(self) -> None:
        while True:
            text = await self.q.get()
            while not self.group:
                await asyncio.sleep(5)
            ok = False
            for attempt in range(4):
                try:
                    st, d = await self.api("POST", f"/v2/groups/{self.group}/messages",
                                           {"content": text, "msg_type": 0})
                    if st in (200, 201, 202) and not d.get("code"):
                        ok = True
                        break
                    log.warning("qq send error %s %s", st, d)
                    if st in (400, 401, 403, 404):
                        break
                except Exception as e:
                    log.warning("qq send failed (%s): %s", attempt, e)
                await asyncio.sleep(min(2 ** attempt * 2, 30))
            if ok:
                self.sent += 1
            else:
                self.failed += 1
            await asyncio.sleep(3.3)  # stay under 20 msgs/min per group

    async def reply(self, group: str, msg_id: str, text: str) -> None:
        try:
            await self.api("POST", f"/v2/groups/{group}/messages",
                           {"content": text, "msg_type": 0, "msg_id": msg_id})
        except Exception as e:
            log.warning("qq reply failed: %s", e)

    def bind(self, group: str) -> None:
        if not group or group == self.group:
            return
        self.group = group
        p = self.cfg["state_file"]
        try:
            with open(p, encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        st["qq_group_openid"] = group
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(st, f, separators=(",", ":"))
        os.replace(p + ".tmp", p)
        log.info("QQ bound to group %s", group)

    # ------------------------------------------------------------ gateway (events)
    async def gateway_loop(self) -> None:
        backoff = 5
        while True:
            try:
                st, d = await self.api("GET", "/gateway")
                url = d.get("url") or "wss://api.bot.qq.com/websocket/"
                async with self.session.ws_connect(url, heartbeat=None,
                                                   timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                    hb_task = None
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        p = json.loads(msg.data)
                        if p.get("s") is not None:
                            self._seq = p["s"]
                        op = p.get("op")
                        if op == 10:
                            iv = p["d"]["heartbeat_interval"] / 1000
                            await ws.send_json({"op": 2, "d": {
                                "token": f"QQBot {await self.token()}", "intents": INTENTS,
                                "shard": [0, 1], "properties": {"$os": "linux", "$browser": "hl-bot", "$device": "hl-bot"}}})
                            hb_task = asyncio.create_task(self._heartbeat(ws, iv))
                        elif op == 0:
                            backoff = 5
                            await self.on_event(p.get("t"), p.get("d") or {})
                        elif op in (7, 9):
                            log.warning("qq gateway asked to reconnect (op %s)", op)
                            break
                    if hb_task:
                        hb_task.cancel()
            except Exception as e:
                log.warning("qq gateway error: %s", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

    async def _heartbeat(self, ws, iv: float) -> None:
        while not ws.closed:
            await asyncio.sleep(iv)
            await ws.send_json({"op": 1, "d": self._seq})

    async def on_event(self, t: str, d: dict) -> None:
        if t == "READY":
            log.info("QQ gateway ready as %s", (d.get("user") or {}).get("username"))
        elif t == "GROUP_ADD_ROBOT":
            self.bind(d.get("group_openid", ""))
            self.q.put_nowait("机器人已加入本群，开始播报信号。请群主在机器人资料页开启「主动在群聊内发言」。")
        elif t == "GROUP_AT_MESSAGE_CREATE":
            g = d.get("group_openid", "")
            txt = (d.get("content") or "").strip()
            if "绑定" in txt or "/start" in txt:
                self.bind(g)
                await self.reply(g, d.get("id", ""), "已绑定本群，开始推送信号")
            elif g == self.group:
                await self.reply(g, d.get("id", ""), f"运行中：已推送 {self.sent} 条，失败 {self.failed} 条")
        elif t == "GROUP_DEL_ROBOT" and d.get("group_openid") == self.group:
            log.warning("QQ bot removed from bound group")
        elif t in ("GROUP_MSG_REJECT",):
            log.warning("QQ group turned off proactive messages")
