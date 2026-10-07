"""On-chain whale watcher (BSC ERC20 Transfer logs).

Reads onchain_watch.json next to this file. For every token in it, polls recent
Transfer logs from the watched holder addresses and alerts when a holder:
  - sells on a DEX (transfer into a DEX vault / pool manager)
  - sends to an exchange hot wallet (known list, or an EOA with a huge nonce)
  - sends a large amount to an unknown address (then that address is followed
    for 48h; if it forwards to an exchange/DEX we alert "via deposit address")
Only uses public RPCs, polling recent blocks (no archive access needed).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import aiohttp

log = logging.getLogger("onchain")
HERE = os.path.dirname(os.path.abspath(__file__))
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEFAULT_RPCS = ["https://bsc-rpc.publicnode.com", "https://bsc.drpc.org", "https://bsc-dataseed.bnbchain.org"]


def _topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower()[2:]


def _addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _fmt_amt(x: float) -> str:
    if x >= 1e8:
        return f"{x / 1e8:.2f}亿"
    if x >= 1e4:
        return f"{x / 1e4:.1f}万"
    return f"{x:,.0f}"


def _fmt_usd(x: float) -> str:
    if x >= 1e4:
        return f"${x / 1e4:.1f}万"
    return f"${x:,.0f}"


class TokenWatch:
    def __init__(self, t: dict):
        self.symbol = t["symbol"]
        self.token = t["token"].lower()
        self.dec = int(t.get("decimals", 18))
        self.min_amt = float(t.get("min_amount", 0))
        self.price_pair = t.get("gate_pair")  # e.g. CNPY_USDT
        self.watch = {k.lower(): v for k, v in t.get("watch", {}).items()}
        self.exchanges = {k.lower(): v for k, v in t.get("exchanges", {}).items()}
        self.dex = {k.lower(): v for k, v in t.get("dex", {}).items()}
        self.hops: dict = {}  # unknown destination -> (expires_ts, origin label)


class OnchainWatcher:
    def __init__(self, notifier, session: aiohttp.ClientSession, path: str = None):
        self.n, self.s = notifier, session
        path = path or os.path.join(HERE, "onchain_watch.json")
        self.cfg = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.cfg = json.load(f)
        self.enabled = bool(self.cfg.get("enabled")) and bool(self.cfg.get("tokens"))
        self.rpcs = self.cfg.get("rpcs") or DEFAULT_RPCS
        self.poll_s = float(self.cfg.get("poll_s", 12))
        self.hot_nonce = int(self.cfg.get("exchange_nonce_min", 50000))
        self.tokens = [TokenWatch(t) for t in self.cfg.get("tokens", [])]
        self.state_path = os.path.join(HERE, "onchain_state.json")
        self.last_block = 0
        self.nonce_cache: dict = {}
        self.code_cache: dict = {}
        self.price_cache: dict = {}
        self.rpc_i = 0

    async def rpc(self, method: str, params: list):
        err = None
        for k in range(len(self.rpcs)):
            url = self.rpcs[(self.rpc_i + k) % len(self.rpcs)]
            try:
                async with self.s.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                       headers={"User-Agent": "Mozilla/5.0"},
                                       timeout=aiohttp.ClientTimeout(total=20)) as r:
                    d = await r.json(content_type=None)
                if "result" in d:
                    self.rpc_i = (self.rpc_i + k) % len(self.rpcs)
                    return d["result"]
                err = d.get("error")
            except Exception as e:  # noqa: BLE001
                err = e
        raise RuntimeError(f"all RPCs failed for {method}: {err}")

    def load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
            self.last_block = int(st.get("last_block", 0))
            now = time.time()
            for tw in self.tokens:
                for a, (exp, lab) in st.get("hops", {}).get(tw.token, {}).items():
                    if exp > now:
                        tw.hops[a] = (exp, lab)
        except Exception:  # noqa: BLE001
            pass

    def save_state(self):
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump({"last_block": self.last_block,
                           "hops": {tw.token: tw.hops for tw in self.tokens}}, f)
        except Exception:  # noqa: BLE001
            log.exception("save onchain state failed")

    async def price(self, tw: TokenWatch) -> float:
        if not tw.price_pair:
            return 0.0
        p, ts = self.price_cache.get(tw.symbol, (0.0, 0))
        if time.time() - ts < 60:
            return p
        try:
            async with self.s.get(f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={tw.price_pair}",
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json(content_type=None)
            p = float(d[0]["last"])
            self.price_cache[tw.symbol] = (p, time.time())
        except Exception:  # noqa: BLE001
            pass
        return p

    async def classify(self, tw: TokenWatch, to: str):
        """-> (kind, label) kind in dex/exchange/watch/contract/unknown"""
        if to in tw.dex:
            return "dex", tw.dex[to]
        if to in tw.exchanges:
            return "exchange", tw.exchanges[to]
        if to in tw.watch:
            return "watch", tw.watch[to]
        try:
            if to not in self.code_cache:
                self.code_cache[to] = len(await self.rpc("eth_getCode", [to, "latest"])) > 2
            if self.code_cache[to]:
                return "contract", "合约地址"
            if to not in self.nonce_cache:
                self.nonce_cache[to] = int(await self.rpc("eth_getTransactionCount", [to, "latest"]), 16)
            if self.nonce_cache[to] >= self.hot_nonce:
                return "exchange", "疑似交易所热钱包"
        except Exception:  # noqa: BLE001
            pass
        return "unknown", "新地址"

    async def handle(self, tw: TokenWatch, lg: dict):
        frm, to = _addr(lg["topics"][1]), _addr(lg["topics"][2])
        amt = int(lg["data"], 16) / 10 ** tw.dec
        now = time.time()
        if frm in tw.watch:
            if amt < tw.min_amt:
                return
            kind, lab = await self.classify(tw, to)
            src = tw.watch[frm]
            if kind == "dex":
                head, act = "🔴", f"DEX 卖出（{lab}）"
            elif kind == "exchange":
                head, act = "🔴", f"转入交易所（{lab}）"
            elif kind == "watch":
                head, act = "🔁", f"内部转移 → {lab}"
            elif kind == "contract":
                head, act = "🟠", "转入合约（可能是聚合器/跨链桥）"
            else:
                head, act = "🟠", "大额转出 → 新地址（继续跟踪 48h）"
                tw.hops[to] = (now + 48 * 3600, src)
        elif frm in tw.hops:
            exp, src = tw.hops[frm]
            if exp < now:
                tw.hops.pop(frm, None)
                return
            if amt < tw.min_amt * 0.5:
                return
            kind, lab = await self.classify(tw, to)
            if kind not in ("dex", "exchange"):
                return
            head = "🔴"
            act = f"经中转地址{'DEX 卖出' if kind == 'dex' else '转入交易所'}（{lab}）"
        else:
            return
        p = await self.price(tw)
        val = f" ≈ {_fmt_usd(amt * p)}" if p else ""
        ptxt = f"\n价格 ${p:.4f}" if p else ""
        text = (f"{head} {tw.symbol} 大户异动\n{src}：{act}\n"
                f"数量 {_fmt_amt(amt)} {tw.symbol}{val}{ptxt}\ntx {lg['transactionHash'][:10]}…")
        log.info("onchain alert %s %s -> %s %.0f", tw.symbol, frm, to, amt)
        self.n.send(text, kind="ONCHAIN")

    async def poll_once(self):
        head = int(await self.rpc("eth_blockNumber", []), 16) - 2  # small reorg margin
        if not self.last_block or head - self.last_block > 3000:
            self.last_block = head - 30
        if head <= self.last_block:
            return
        start = self.last_block + 1
        while start <= head:
            end = min(start + 199, head)
            for tw in self.tokens:
                now = time.time()
                for a in [a for a, (e, _) in tw.hops.items() if e < now]:
                    tw.hops.pop(a, None)
                senders = list(tw.watch) + list(tw.hops)[:200]
                logs = await self.rpc("eth_getLogs", [{"address": tw.token, "fromBlock": hex(start),
                                                      "toBlock": hex(end),
                                                      "topics": [TRANSFER, [_topic(a) for a in senders]]}])
                for lg in logs:
                    if not lg.get("removed"):
                        await self.handle(tw, lg)
            self.last_block = end
            start = end + 1
        self.save_state()

    async def run(self):
        if not self.enabled:
            log.info("onchain watcher disabled (no onchain_watch.json or enabled=false)")
            return
        self.load_state()
        log.info("onchain watcher: %s",
                 ", ".join(f"{t.symbol} {len(t.watch)} holders, min {t.min_amt:,.0f}" for t in self.tokens))
        fails = 0
        while True:
            try:
                await self.poll_once()
                fails = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                fails += 1
                if fails in (1, 10) or fails % 100 == 0:
                    log.warning("onchain poll failed (%d): %s", fails, e)
            await asyncio.sleep(self.poll_s)
