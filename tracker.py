"""Per-coin price tracking across broadcasts + follow advice + self-evaluation.

- tracks: (inst|side) -> first/last broadcast price, count, wallets. Lets every later
  broadcast on the same coin/side show how far price moved since the FIRST one.
- samples: each entry signal (open / add / confluence) is re-priced 1h / 4h / 24h later
  with Hyperliquid allMids, so the bot measures which signal types actually worked.
- advise(): rule-based follow advice, overridden by measured 7-day stats once a type
  has >= 10 samples.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger("tracker")
TRACK_TTL = 24 * 3600
HORIZONS = (("r1h", 3600), ("r4h", 4 * 3600), ("r24h", 24 * 3600))
KEEP = 7 * 24 * 3600


def pct(a: Optional[float], b: Optional[float], side: str) -> Optional[float]:
    """Move from a to b in the signal's favour (%): long up = +, short down = +."""
    if not a or not b:
        return None
    r = (b / a - 1) * 100
    return r if side == "long" else -r


def ago(sec: float) -> str:
    m = int(sec // 60)
    if m < 1:
        return "刚刚"
    if m < 60:
        return f"{m}分钟前"
    h = m // 60
    return f"{h}小时{m % 60}分前" if h < 24 else f"{h // 24}天前"


class Tracker:
    def __init__(self):
        self.tracks: dict = {}
        self.samples: list = []

    # ---------------------------------------------------------- state
    def dump(self) -> dict:
        return {"tracks": self.tracks, "samples": self.samples[-2000:]}

    def load(self, st: dict) -> None:
        now = time.time()
        self.tracks = {k: v for k, v in (st.get("tracks") or {}).items() if now - v["last_t"] < TRACK_TTL}
        self.samples = [s for s in (st.get("samples") or []) if now - s["t"] < KEEP]

    # ---------------------------------------------------------- tracking
    def get(self, inst: str, side: str) -> Optional[dict]:
        tr = self.tracks.get(f"{inst}|{side}")
        if tr and time.time() - tr["last_t"] < TRACK_TTL:
            return tr
        return None

    def hit(self, inst: str, side: str, price: Optional[float], addr: str, now: float) -> dict:
        """Register a broadcast; returns the track (with first price) BEFORE this hit is counted."""
        k = f"{inst}|{side}"
        tr = self.get(inst, side)
        if not tr:
            tr = {"first_t": now, "first_px": price, "n": 0, "wallets": [], "last_t": now, "last_px": price}
            self.tracks[k] = tr
        prev = dict(tr)
        tr["n"] += 1
        tr["last_t"], tr["last_px"] = now, price or tr["last_px"]
        if not tr["first_px"]:
            tr["first_px"] = price
        if addr and addr not in tr["wallets"]:
            tr["wallets"].append(addr)
        return prev

    def line(self, prev: dict, price: Optional[float], side: str, now: float) -> str:
        if not prev or prev["n"] == 0:
            return "首次播报"
        ch = pct(prev["first_px"], price, side)
        chs = f"{ch:+.2f}%" if ch is not None else "n/a"
        return (f"第{prev['n'] + 1}次播报 · 首次价 {fmtp(prev['first_px'])} ({ago(now - prev['first_t'])}) "
                f"· 至今 {chs}{'(顺势)' if ch and ch > 0 else ('(逆势)' if ch and ch < 0 else '')}")

    # ---------------------------------------------------------- evaluation
    def add_sample(self, kind: str, coin: str, side: str, price: Optional[float], now: float) -> None:
        if not price:
            return
        self.samples.append({"t": now, "kind": kind, "coin": coin, "side": side, "px": price})
        if len(self.samples) > 2000:
            self.samples = self.samples[-2000:]

    def pending_dexes(self, now: float) -> set:
        out = set()
        for s in self.samples:
            for h, sec in HORIZONS:
                if h not in s and now - s["t"] >= sec:
                    out.add(s["coin"].split(":")[0] if ":" in s["coin"] else "")
        return out

    def fill(self, mids_by_dex: dict, now: float) -> int:
        n = 0
        for s in self.samples:
            dex = s["coin"].split(":")[0] if ":" in s["coin"] else ""
            mids = mids_by_dex.get(dex)
            if mids is None:
                continue
            m = mids.get(s["coin"]) or mids.get(s["coin"].split(":")[-1])
            for h, sec in HORIZONS:
                if h not in s and now - s["t"] >= sec:
                    s[h] = pct(s["px"], float(m), s["side"]) if m else None
                    n += 1
        self.samples = [s for s in self.samples if now - s["t"] < KEEP]
        return n

    def stats(self, kind: str) -> Optional[dict]:
        rs = [s["r4h"] for s in self.samples if s["kind"] == kind and s.get("r4h") is not None]
        if not rs:
            return None
        return {"n": len(rs), "win": sum(1 for r in rs if r > 0) / len(rs) * 100, "avg": sum(rs) / len(rs)}

    def report(self) -> str:
        kinds = ["共振3+", "共振2", "A开仓", "B开仓", "A加仓", "B加仓"]
        lines = ["📊 <b>近7天信号复盘</b>（播报后按价格走势统计，顺势为正）"]
        any_ = False
        for k in kinds:
            ss = [s for s in self.samples if s["kind"] == k]
            if not ss:
                continue
            any_ = True
            parts = []
            for h, _ in HORIZONS:
                rs = [s[h] for s in ss if s.get(h) is not None]
                if rs:
                    parts.append(f"{h[1:]} 胜率{sum(1 for r in rs if r > 0) / len(rs) * 100:.0f}% 均{sum(rs) / len(rs):+.2f}%")
            lines.append(f"• {k}（{len(ss)}次）: " + (" | ".join(parts) if parts else "数据未满1小时"))
        if not any_:
            lines.append("暂无样本")
        lines.append("胜率≥55%且均值为正的类型可跟；低于45%的不建议跟。")
        return "\n".join(lines)


def fmtp(x: Optional[float]) -> str:
    if not x:
        return "n/a"
    if x >= 1000:
        return f"{x:,.1f}"
    if x >= 10:
        return f"{x:.2f}"
    return f"{x:.4g}"


def advise(kind: str, act: str, lev, chase: Optional[float], tr: Tracker) -> str:
    """kind: 共振3+/共振2/A开仓/B开仓/A加仓/B加仓/平仓/减仓."""
    if act in ("close", "reduce"):
        return "⛔ 不建议跟（离场信号；同向持仓可考虑止盈）"
    if chase is not None and chase > 3:
        return f"⚠️ 不建议追（较首次播报已顺势{chase:.1f}%）"
    try:
        if lev and float(lev) >= 25:
            return f"⚠️ 谨慎（{lev}x 高杠杆，波动大）"
    except (TypeError, ValueError):
        pass
    st = tr.stats(kind)
    if st and st["n"] >= 10:
        if st["win"] < 45 or st["avg"] < 0:
            return f"⛔ 不建议跟（近7天此类4h胜率{st['win']:.0f}%）"
        if st["win"] >= 55 and st["avg"] > 0:
            return f"✅ 可跟（近7天此类4h胜率{st['win']:.0f}%，均{st['avg']:+.2f}%）"
    base = {
        "共振3+": "✅ 可跟（3个以上聪明钱包同向）",
        "共振2": "✅ 可跟，轻仓（2个钱包同向）",
        "A开仓": "🟡 可小仓试，等共振更稳",
        "A加仓": "🟡 可参考（A级加码看好）",
        "B开仓": "👀 观察，不建议单独跟",
        "B加仓": "👀 观察",
    }
    return base.get(kind, "👀 观察")
