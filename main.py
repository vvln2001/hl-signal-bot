#!/usr/bin/env python3
"""Hyperliquid smart-money signal listener -> Telegram (Chinese alerts).

Hybrid monitoring:
  * WS userFills for wallets with monitor=ws_userFills (max 10 users per IP)
    -> a fill triggers a clearinghouseState fetch (debounced) and the diff.
  * REST clearinghouseState round-robin for the rest (one request per tick).
  * HIP-3 builder dexes polled every 30s for hip3_dex_poll wallets.
  * 60s REST reconcile for WS wallets.
All REST calls go through one ticker, so total weight can never exceed
max_weight_per_min (clearinghouseState = weight 2).

Run:  python main.py          (RUN_SECONDS=300 to stop automatically)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections import deque
from logging.handlers import RotatingFileHandler

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import aiohttp  # noqa: E402

from coinmap import CoinMapper  # noqa: E402
from config import load_config, load_wallets  # noqa: E402
from detector import baseline, diff, parse_clearinghouse  # noqa: E402
from fmt import confluence_brief, signal_brief  # noqa: E402
from tracker import Tracker, advise, fmtp, pct  # noqa: E402
from notifier import Notifier  # noqa: E402
from onchain import OnchainWatcher  # noqa: E402

log = logging.getLogger("bot")
W_CH = 2  # clearinghouseState weight


def rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return -1


class Monitor:
    def __init__(self, cfg: dict, wallets: list, mapper: CoinMapper, notifier: Notifier,
                 session: aiohttp.ClientSession):
        self.cfg, self.mapper, self.n, self.session = cfg, mapper, notifier, session
        self.wallets = {w["address"]: w for w in wallets}
        self.ws_wallets = [w["address"] for w in wallets if w["ws"]][:10]
        self.rr_wallets = [w["address"] for w in wallets if w["address"] not in self.ws_wallets]
        self.hip3_wallets = [w["address"] for w in wallets if w["hip3"]]
        self.all_dexes: list = []
        self.books: dict = {}     # (addr, dex) -> book
        self.known: set = set()   # (addr, dex) with a baseline
        self.q = {p: deque() for p in ("urgent", "base", "norm", "low")}
        self.queued: set = set()
        self.inflight: set = set()
        self.trig: dict = {}      # (addr, dex) -> {coin: startPosition}
        self.trig_at: dict = {}   # (addr, dex) -> release time (debounce)
        self.last_fill_px: dict = {}
        self.rr_i = 0
        self.rr_last: dict = {}
        self.rr_gaps: deque = deque(maxlen=200)
        self.pause_until = 0.0
        self.meter: deque = deque()
        self.stats = {"req": 0, "429": 0, "err": 0, "ws_msgs": 0, "ws_fills": 0, "ws_connects": 0,
                      "signals": 0, "alerts": 0, "skipped_okx": 0, "suppressed": 0}
        self.by_reason: dict = {}
        self.ws_connected = False
        self.ws_last_msg = 0.0
        self.recent: dict = {}    # dedupe key -> t
        self.conf_events: list = []
        self.conf_sent: dict = {}
        self.dirty = False
        self.timers = {"hip3": 0.0, "reconcile": time.time() + cfg["ws_reconcile_s"],
                       "rescan": time.time() + cfg["hip3_rescan_min"] * 60, "stats": time.time() + cfg["stats_every_s"],
                       "save": time.time() + 10}
        self.started = time.time()
        self.baseline_logged = False
        self.skip_logged: dict = {}
        self.tracker = Tracker()
        self.report_day = ""
        self.n_wallets = len(wallets)

    # ------------------------------------------------------------------ state
    def load_state(self) -> None:
        p = self.cfg["state_file"]
        if not os.path.exists(p):
            log.info("no state file: fresh baseline (no alerts for existing positions)")
            return
        try:
            with open(p, encoding="utf-8") as f:
                st = json.load(f)
        except Exception as e:
            log.warning("state file unreadable (%s): fresh baseline", e)
            return
        age = time.time() - st.get("saved_at", 0)
        self.books = {tuple(k.split("|", 1)): v for k, v in st.get("books", {}).items()
                      if k.split("|", 1)[0] in self.wallets}
        self.conf_events = [e for e in st.get("conf_events", []) if time.time() - e["t"] < self.cfg["confluence_window_h"] * 3600]
        self.conf_sent = {tuple(k.split("|", 1)): tuple(v) for k, v in st.get("conf_sent", {}).items()}
        self.recent = {k: v for k, v in st.get("recent", {}).items() if time.time() - v < 3600}
        self.tracker.load(st)
        self.report_day = st.get("report_day", "")
        if age <= self.cfg["restart_rediff_max_age_s"]:
            self.known = {k for k in self.books}
            log.info("state %.0fs old: resuming diff from saved positions (%d books)", age, len(self.books))
        else:
            log.info("state %.0fs old (> %ss): silent re-baseline", age, self.cfg["restart_rediff_max_age_s"])

    def save_state(self) -> None:
        st = {"saved_at": time.time(),
              "books": {f"{a}|{d}": b for (a, d), b in self.books.items()},
              "conf_events": self.conf_events,
              "conf_sent": {f"{k[0]}|{k[1]}": list(v) for k, v in self.conf_sent.items()},
              "recent": self.recent, "report_day": self.report_day, **self.tracker.dump()}
        if getattr(self.n, "chat_id", "") and not self.cfg.get("telegram_chat_id"):
            st["telegram_chat_id"] = self.n.chat_id  # owner bound via /start
        if getattr(self.n, "qq", None) and self.n.qq.group and not os.environ.get("QQ_GROUP_OPENID"):
            st["qq_group_openid"] = self.n.qq.group
        tmp = self.cfg["state_file"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, separators=(",", ":"))
        os.replace(tmp, self.cfg["state_file"])
        self.dirty = False

    # -------------------------------------------------------------- queueing
    def enqueue(self, prio: str, addr: str, dex: str, reason: str, front: bool = False) -> None:
        key = (addr, dex)
        if key in self.queued or key in self.inflight:
            return
        self.queued.add(key)
        (self.q[prio].appendleft if front else self.q[prio].append)((addr, dex, reason))

    def _pop(self):
        now = time.time()
        for key, at in list(self.trig_at.items()):
            if now >= at and key not in self.inflight:
                del self.trig_at[key]
                self.queued.discard(key)  # promote even if already queued elsewhere
                for dq in self.q.values():
                    for item in list(dq):
                        if (item[0], item[1]) == key:
                            dq.remove(item)
                log.info("WS fills %s %s -> state fetch (%s)", self.wallets[key[0]]["short"], key[1] or "main",
                         ",".join(sorted(self.trig.get(key, {}))))
                self.enqueue("urgent", key[0], key[1], "ws_fill")
        for prio in ("urgent", "base", "norm", "low"):
            dq = self.q[prio]
            for _ in range(len(dq)):
                addr, dex, reason = dq.popleft()
                if (addr, dex) in self.inflight:
                    dq.append((addr, dex, reason))
                    continue
                self.queued.discard((addr, dex))
                return addr, dex, reason
        # round robin over REST wallets (main dex)
        for _ in range(len(self.rr_wallets)):
            addr = self.rr_wallets[self.rr_i % len(self.rr_wallets)]
            self.rr_i += 1
            if (addr, "") not in self.inflight:
                if addr in self.rr_last:
                    self.rr_gaps.append(now - self.rr_last[addr])
                self.rr_last[addr] = now
                return addr, "", "rr"
        return None

    def hip3_poll_set(self, addr: str) -> set:
        s = set(self.cfg["hip3_default_dexes"]) if addr in self.hip3_wallets else set()
        s |= {d for (a, d), b in self.books.items() if a == addr and d and b}
        return s

    def due_timers(self) -> None:
        now, c, t = time.time(), self.cfg, self.timers
        if now >= t["hip3"]:
            t["hip3"] = now + c["hip3_poll_s"]
            for a in self.hip3_wallets:
                for d in sorted(self.hip3_poll_set(a)):
                    if (a, d) in self.known:
                        self.enqueue("norm", a, d, "hip3")
        if now >= t["reconcile"]:
            t["reconcile"] = now + c["ws_reconcile_s"]
            for a in self.ws_wallets:
                self.enqueue("norm", a, "", "reconcile")
                if a not in self.hip3_wallets:
                    for d in self.hip3_poll_set(a):
                        self.enqueue("norm", a, d, "reconcile")
        if now >= t["rescan"]:
            t["rescan"] = now + c["hip3_rescan_min"] * 60
            for a in self.hip3_wallets:
                for d in self.all_dexes:
                    if d not in self.hip3_poll_set(a):
                        self.enqueue("low", a, d, "discover")
        if now >= t["save"]:
            t["save"] = now + 10
            if self.dirty:
                try:
                    self.save_state()
                except Exception as e:
                    log.error("save_state failed: %s", e)
        if now >= t["stats"]:
            t["stats"] = now + c["stats_every_s"]
            self.log_stats()

    def weight_per_min(self) -> int:
        now = time.time()
        while self.meter and now - self.meter[0][0] > 60:
            self.meter.popleft()
        return sum(w for _, w in self.meter)

    def log_stats(self) -> None:
        gaps = list(self.rr_gaps)
        avg_gap = sum(gaps) / len(gaps) if gaps else 0
        base_main = sum(1 for a in self.wallets if (a, "") in self.known)
        log.info("STATS weight/min=%d req=%d 429=%d err=%d by=%s | baseline main %d/%d, books=%d | "
                 "WS conn=%s msgs=%d fills=%d | rr gap avg=%.1fs | q=%s | signals=%d alerts=%d skipped_okx=%d "
                 "suppressed=%d | RSS=%.1fMB", self.weight_per_min(), self.stats["req"], self.stats["429"],
                 self.stats["err"], self.by_reason, base_main, len(self.wallets), len(self.books),
                 self.ws_connected, self.stats["ws_msgs"], self.stats["ws_fills"], avg_gap,
                 {k: len(v) for k, v in self.q.items()}, self.stats["signals"], self.stats["alerts"],
                 self.stats["skipped_okx"], self.stats["suppressed"], rss_mb())

    # --------------------------------------------------------------- REST
    async def post(self, body: dict, weight: int):
        self.meter.append((time.time(), weight))
        async with self.session.post(self.cfg["api_url"], json=body,
                                     timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status == 429:
                return 429, None
            if r.status != 200:
                return r.status, None
            return 200, await r.json(content_type=None)

    async def load_dexes(self) -> None:
        for attempt in range(5):
            try:
                st, data = await self.post({"type": "perpDexs"}, 20)
                if st == 200 and isinstance(data, list):
                    self.all_dexes = [d["name"] for d in data if d and d.get("name")]
                    log.info("HIP-3 perp dexes (perpDexs): %s", self.all_dexes)
                    return
            except Exception as e:
                log.warning("perpDexs failed: %s", e)
            await asyncio.sleep(3 * (attempt + 1))
        self.all_dexes = list(self.cfg["hip3_default_dexes"])
        log.warning("perpDexs unavailable, using defaults %s", self.all_dexes)

    async def fetch(self, addr: str, dex: str, reason: str) -> None:
        key = (addr, dex)
        self.inflight.add(key)
        body = {"type": "clearinghouseState", "user": addr}
        if dex:
            body["dex"] = dex
        try:
            st, data = await self.post(body, W_CH)
            self.stats["req"] += 1
            self.by_reason[reason] = self.by_reason.get(reason, 0) + 1
            if st == 429:
                self.stats["429"] += 1
                self.pause_until = time.time() + self.cfg["backoff_429_s"]
                log.warning("429 from Hyperliquid: pausing REST %ss", self.cfg["backoff_429_s"])
                if reason != "rr":
                    self.inflight.discard(key)
                    self.enqueue("urgent" if reason == "ws_fill" else "base", addr, dex, reason, front=True)
                return
            if st != 200 or data is None:
                self.stats["err"] += 1
                log.warning("HTTP %s for %s %s", st, addr[:10], dex or "main")
                if reason in ("baseline", "ws_fill"):
                    self.inflight.discard(key)
                    self.enqueue("base", addr, dex, reason)
                return
        except Exception as e:
            self.stats["err"] += 1
            log.warning("request error %s %s: %s", addr[:10], dex or "main", e)
            if reason in ("baseline", "ws_fill"):
                self.inflight.discard(key)
                self.enqueue("base", addr, dex, reason)
            return
        finally:
            self.inflight.discard(key)
        try:
            self.apply(addr, dex, data, reason)
        except Exception:
            log.exception("apply failed for %s %s", addr, dex)

    def apply(self, addr: str, dex: str, data: dict, reason: str) -> None:
        key = (addr, dex)
        cur = parse_clearinghouse(data)
        trig = self.trig.pop(key, None)
        thr, mn = self.cfg["change_threshold"], self.cfg["min_notional_usd"]
        if key not in self.known:
            if trig:
                # first look at this dex, caused by a live fill: rebuild "before" from startPosition
                prev = baseline({c: v for c, v in cur.items() if c not in trig})
                for coin, sp in trig.items():
                    if abs(sp) > 1e-12:
                        mpx = (cur.get(coin) or {}).get("mark_px") or self.last_fill_px.get((addr, coin))
                        prev[coin] = {"szi": sp, "ref": sp, "ann": True, "value": abs(sp) * mpx if mpx else None,
                                      "mark_px": mpx, "entry_px": None, "upnl": None}
                events, new = diff(prev, cur, thr, mn)
            else:
                events, new = [], baseline(cur)
                w = self.wallets[addr]
                if cur:
                    log.info("baseline %s %s [%s] %s: %s", w["tier"], w["short"], reason, dex or "main",
                             ", ".join(f"{c} {'L' if p['szi'] > 0 else 'S'} ${(p['value'] or 0) / 1e3:.0f}k"
                                       for c, p in cur.items()))
            self.known.add(key)
        else:
            events, new = diff(self.books.get(key, {}), cur, thr, mn)
        if new or key in self.books:
            self.books[key] = new
            self.dirty = True
        for ev in events:
            self.handle(addr, ev)
        if not self.baseline_logged and all((a, "") in self.known for a in self.wallets):
            self.baseline_logged = True
            log.info("BASELINE READY (fresh or resumed): %d/%d wallets main-dex loaded in %.1fs, %d books with positions",
                     len(self.wallets), len(self.wallets), time.time() - self.started,
                     sum(1 for b in self.books.values() if b))

    # ------------------------------------------------------------- signals
    def handle(self, addr: str, ev: dict) -> None:
        w = self.wallets[addr]
        coin, act = ev["coin"], ev["action"]
        self.stats["signals"] += 1
        dk = f"{addr}|{coin}|{act}|{ev['szi']:.8g}"
        now = time.time()
        if now - self.recent.get(dk, 0) < 900:
            log.info("dedupe: %s", dk)
            return
        self.recent[dk] = now
        if len(self.recent) > 2000:
            self.recent = {k: v for k, v in self.recent.items() if now - v < 3600}
        inst, note, mult = self.mapper.map(coin)
        summary = (f"{w['tier']} {w['short']} {act} {ev['side']} {coin} szi {ev['prev_szi']:g}->{ev['szi']:g} "
                   f"value {ev['value'] or ev['prev_value'] or 0:,.0f}")
        if act == "close" or (act == "flip"):
            self._conf_remove(addr, inst or coin, "long" if ev["prev_szi"] > 0 else "short")
        if inst is None:
            self.stats["skipped_okx"] += 1
            log.info("SKIP (%s): %s", note, summary)
            return
        if ev.get("suppressed"):
            self.stats["suppressed"] += 1
            log.info("below min notional $%s / unannounced: %s", f"{self.cfg['min_notional_usd']:,.0f}", summary)
            return
        price = self.last_fill_px.get((addr, coin)) or ev.get("mark_px")
        side = ev["side"]
        alert = w["tier"] in self.cfg["alert_tiers"] and not (self.cfg["quiet_mode"] and act in ("add", "reduce"))
        kind = f"{w['tier']}{'加仓' if act == 'add' else '开仓'}"
        chase = None
        if act in ("close", "reduce"):
            tr = self.tracker.get(inst, side)
            if tr:
                ch = pct(tr["first_px"], price, side)
                track_line = (f"该币首次{'开多' if side == 'long' else '开空'}播报价 {fmtp(tr['first_px'])}"
                              f" → 现 {fmtp(price)} ({ch:+.2f}%)" if ch is not None else "")
            else:
                track_line = "此前无该方向开仓播报"
        else:
            prev = self.tracker.hit(inst, side, price, addr, now) if alert else (self.tracker.get(inst, side) or {})
            if prev and prev.get("n"):
                chase = pct(prev["first_px"], price, side)
            track_line = self.tracker.line(prev, price, side, now)
            nw = len(set((prev.get("wallets") or []) + [addr])) if prev else 1
            if nw > 1:
                track_line += f" · 24h内同向钱包{nw}个"
            if w["tier"] in ("A", "B"):
                self.tracker.add_sample(kind, coin, side, price, now)
                self.dirty = True
        text = signal_brief(ev, w, self.n_wallets, price, track_line,
                            advise(kind, act, ev.get("lev"), chase, self.tracker))
        if w["tier"] not in self.cfg["alert_tiers"]:
            log.info("tier %s logged only: %s", w["tier"], summary)
            self.n.record(text, f"LOGGED_TIER_{w['tier']}")
        elif not alert:
            log.info("quiet mode, logged only: %s", summary)
            self.n.record(text, "LOGGED_QUIET")
        else:
            log.info("ALERT %s -> %s", summary, inst)
            self.stats["alerts"] += 1
            self.n.send(text, silent=self.in_quiet_hours(now))
        if act in ("open", "flip") and w["tier"] in self.cfg["confluence_tiers"]:
            self._conf_add(addr, w, inst, ev, now, price)

    def in_quiet_hours(self, now: float) -> bool:
        qh = self.cfg.get("quiet_hours")
        if not qh:
            return False
        lt = time.gmtime(now + self.cfg["tz_offset_h"] * 3600)
        m = lt.tm_hour * 60 + lt.tm_min
        s, e = [int(x.split(":")[0]) * 60 + int(x.split(":")[1]) for x in qh]
        return (s <= m < e) if s < e else (m >= s or m < e)

    def _conf_remove(self, addr, inst, side):
        self.conf_events = [e for e in self.conf_events
                            if not (e["addr"] == addr and e["inst"] == inst and e["side"] == side)]

    def _conf_add(self, addr, w, inst, ev, now, price=None):
        win = self.cfg["confluence_window_h"] * 3600
        self.conf_events = [e for e in self.conf_events if now - e["t"] < win]
        self._conf_remove(addr, inst, ev["side"])
        self.conf_events.append({"addr": addr, "inst": inst, "side": ev["side"], "t": now, "tier": w["tier"],
                                 "short": w["short"], "label": w["label"], "value": ev["value"],
                                 "entry_px": ev["entry_px"], "coin": ev["coin"]})
        members = [e for e in self.conf_events if e["inst"] == inst and e["side"] == ev["side"]]
        n = len(members)
        k = (inst, ev["side"])
        last = self.conf_sent.get(k)
        if n >= self.cfg["confluence_min_wallets"] and (last is None or now - last[1] > win or n > last[0]):
            self.conf_sent[k] = (n, now)
            self.dirty = True
            log.info("CONFLUENCE %s %s x%d", inst, ev["side"], n)
            tr = self.tracker.get(inst, ev["side"])
            ch = pct(tr["first_px"], price, ev["side"]) if tr else None
            if tr and ch is not None:
                line = f"首次播报价 {fmtp(tr['first_px'])} → 现 {fmtp(price)} ({ch:+.2f}%)"
            else:
                line = f"现价 {fmtp(price)}"
            kind = "共振3+" if n >= 3 else "共振2"
            self.tracker.add_sample(kind, ev["coin"], ev["side"], price, now)
            ranks = {a: self.wallets[a]["rank"] for a in self.wallets}
            self.n.send(confluence_brief(inst.replace("-USDT-SWAP", ""), ev["side"], members, self.n_wallets, ranks,
                                         line, advise(kind, "open", None, ch, self.tracker)), kind="CONFLUENCE")

    # ------------------------------------------------------------- loops
    async def eval_loop(self) -> None:
        """Re-price past signals (1h/4h/24h) and send a daily review."""
        while True:
            await asyncio.sleep(600)
            now = time.time()
            mids = {}
            for d in self.tracker.pending_dexes(now):
                body = {"type": "allMids"}
                if d:
                    body["dex"] = d
                try:
                    st, data = await self.post(body, 2)
                    if st == 200 and isinstance(data, dict):
                        mids[d] = data
                except Exception as e:
                    log.warning("allMids failed: %s", e)
            if mids and self.tracker.fill(mids, now):
                self.dirty = True
            lt = time.gmtime(now + self.cfg["tz_offset_h"] * 3600)
            day = time.strftime("%Y-%m-%d", lt)
            if lt.tm_hour >= int(self.cfg.get("report_hour", 21)) and self.report_day != day:
                self.report_day = day
                self.dirty = True
                if self.tracker.samples:
                    self.n.send(self.tracker.report(), kind="REPORT")

    async def scheduler_loop(self) -> None:
        tick = 60.0 / (self.cfg["max_weight_per_min"] / W_CH)
        log.info("REST ticker: one request every %.0f ms (cap %d weight/min)", tick * 1000,
                 self.cfg["max_weight_per_min"])
        loop = asyncio.get_running_loop()
        while True:
            t0 = loop.time()
            try:
                self.due_timers()
                if time.time() >= self.pause_until and len(self.inflight) < self.cfg["max_inflight"] \
                        and self.weight_per_min() + W_CH <= self.cfg["max_weight_per_min"]:
                    task = self._pop()
                    if task:
                        asyncio.create_task(self.fetch(*task))
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(max(0.0, tick - (loop.time() - t0)))

    def on_fill(self, user: str, f: dict) -> None:
        user = user.lower()
        if user not in self.wallets:
            return
        coin = f.get("coin", "")
        if coin.startswith("@"):
            return  # spot
        self.stats["ws_fills"] += 1
        dex = coin.split(":", 1)[0] if ":" in coin else ""
        key = (user, dex)
        try:
            self.last_fill_px[(user, coin)] = float(f.get("px"))
            sp = float(f.get("startPosition") or 0)
        except (TypeError, ValueError):
            sp = 0.0
        self.trig.setdefault(key, {}).setdefault(coin, sp)
        self.trig_at.setdefault(key, time.time() + self.cfg["ws_debounce_s"])
        log.debug("WS fill %s %s %s %s @ %s (start %s)", self.wallets[user]["short"], coin, f.get("dir"),
                 f.get("sz"), f.get("px"), f.get("startPosition"))

    async def ws_loop(self) -> None:
        backoff = 1
        first = True
        while True:
            try:
                async with self.session.ws_connect(self.cfg["ws_url"], heartbeat=None, autoping=True,
                                                   max_msg_size=8 * 1024 * 1024,
                                                   timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                    for a in self.ws_wallets:
                        await ws.send_json({"method": "subscribe", "subscription": {"type": "userFills", "user": a}})
                    self.ws_connected = True
                    self.stats["ws_connects"] += 1
                    self.ws_last_msg = time.time()
                    log.info("WS connected, subscribed userFills for %d wallets", len(self.ws_wallets))
                    backoff = 1
                    if not first:  # catch up whatever happened while disconnected
                        for a in self.ws_wallets:
                            self.enqueue("norm", a, "", "reconcile")
                    first = False
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        while True:
                            try:
                                msg = await ws.receive(timeout=self.cfg["ws_ping_s"] * 2 + 20)
                            except asyncio.TimeoutError:
                                log.warning("WS silent too long, reconnecting")
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self.ws_last_msg = time.time()
                                self.stats["ws_msgs"] += 1
                                self._on_ws(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
                                              aiohttp.WSMsgType.ERROR):
                                log.warning("WS closed: %s", msg.type)
                                break
                    finally:
                        pinger.cancel()
            except Exception as e:
                log.warning("WS error: %s", e)
            self.ws_connected = False
            log.info("WS reconnect in %ss", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _ping(self, ws) -> None:
        while True:
            await asyncio.sleep(self.cfg["ws_ping_s"])
            await ws.send_json({"method": "ping"})

    def _on_ws(self, raw: str) -> None:
        try:
            m = json.loads(raw)
        except ValueError:
            return
        ch = m.get("channel")
        if ch == "userFills":
            d = m.get("data") or {}
            if d.get("isSnapshot"):
                log.debug("WS snapshot ignored for %s (%d fills)", d.get("user"), len(d.get("fills") or []))
                return
            for f in d.get("fills") or []:
                self.on_fill(d.get("user", ""), f)
        elif ch == "error":
            log.warning("WS error message: %s", m.get("data"))
        elif ch == "subscriptionResponse":
            log.debug("WS sub ok: %s", m.get("data"))

    async def run(self, run_seconds: float = 0) -> None:
        self.load_state()
        await self.load_dexes()
        for a in self.wallets:
            if (a, "") not in self.known:
                self.enqueue("base", a, "", "baseline")
        for a in self.hip3_wallets:
            for d in self.all_dexes:
                if (a, d) not in self.known:
                    self.enqueue("base", a, d, "baseline")
        for (a, d) in list(self.known):  # resumed: refresh soon so downtime changes surface
            self.enqueue("base", a, d, "resume")
        tasks = [asyncio.create_task(supervise(n, f)) for n, f in
                 (("scheduler", self.scheduler_loop), ("ws", self.ws_loop), ("telegram", self.n.run), ("qq", self.n.qq.run), ("eval", self.eval_loop),
                  ("onchain", OnchainWatcher(self.n, self.session).run))]
        try:
            if run_seconds:
                await asyncio.sleep(run_seconds)
            else:
                await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            self.log_stats()
            self.save_state()


async def supervise(name, fn):
    while True:
        try:
            await fn()
            if name in ("telegram", "qq", "onchain"):
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s loop crashed, restarting in 5s", name)
            await asyncio.sleep(5)


def setup_logging(cfg: dict) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(cfg["log_level"]).upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = RotatingFileHandler(cfg["bot_log"], maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers = [sh, fh]
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def amain(cfg: dict, run_seconds: float) -> None:
    wallets = load_wallets(cfg)
    mapper = CoinMapper(cfg["okx_bases"], cfg["okx_categories"], cfg.get("coin_overrides"))
    conn = aiohttp.TCPConnector(limit=8, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=conn, headers={"Content-Type": "application/json"}) as s:
        n = Notifier(cfg, s)
        mon = Monitor(cfg, wallets, mapper, n, s)
        log.info("start: %d wallets (WS %d, REST rr %d, HIP-3 %d), alert tiers %s, min notional $%s, log_only=%s",
                 len(wallets), len(mon.ws_wallets), len(mon.rr_wallets), len(mon.hip3_wallets),
                 cfg["alert_tiers"], f"{cfg['min_notional_usd']:,.0f}", cfg["log_only"])
        await mon.run(run_seconds)
        if run_seconds and not n.log_only:
            await asyncio.sleep(min(10, n.q.qsize() * 1.1))


def main() -> None:
    cfg = load_config()
    setup_logging(cfg)
    run_seconds = float(os.environ.get("RUN_SECONDS", "0") or 0)
    while True:
        try:
            asyncio.run(amain(cfg, run_seconds))
            if run_seconds:
                break
        except KeyboardInterrupt:
            log.info("stopped by user")
            break
        except Exception:
            log.exception("fatal error, restarting in 10s")
            time.sleep(10)


if __name__ == "__main__":
    main()
