"""Chinese Telegram message formatting (HTML parse mode)."""
from __future__ import annotations

import html
import time
from typing import List, Optional

ACTION = {
    ("open", "long"): ("🟢", "开多"),
    ("open", "short"): ("🔴", "开空"),
    ("add", "long"): ("➕", "加仓(多)"),
    ("add", "short"): ("➕", "加仓(空)"),
    ("reduce", "long"): ("➖", "减仓(多)"),
    ("reduce", "short"): ("➖", "减仓(空)"),
    ("close", "long"): ("⚪", "平仓(多)"),
    ("close", "short"): ("⚪", "平仓(空)"),
    ("flip", "long"): ("🔄", "反手→多"),
    ("flip", "short"): ("🔄", "反手→空"),
}
CLS = {"stock": "美股/股票", "commodity": "商品", "crypto": ""}


def usd(x: Optional[float], sign: bool = False) -> str:
    if x is None:
        return "n/a"
    s = "+" if sign and x > 0 else ("-" if x < 0 else "")
    a = abs(x)
    if a >= 1e6:
        body = f"${a / 1e6:.2f}M"
    elif a >= 1e3:
        body = f"${a / 1e3:.1f}k"
    else:
        body = f"${a:.0f}"
    return s + body


def px(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    if x >= 1000:
        return f"{x:,.1f}"
    if x >= 1:
        return f"{x:.4g}" if x < 10 else f"{x:.2f}"
    return f"{x:.6g}"


def ts(t: float, tz_h: int = 8) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t + tz_h * 3600)) + f" (UTC+{tz_h})"


def signal_text(ev: dict, w: dict, inst: str, cls: str, mult: int, cfg: dict,
                fill_px: Optional[float] = None, t: Optional[float] = None) -> str:
    t = t or time.time()
    emo, act = ACTION[(ev["action"], ev["side"])]
    e = html.escape
    lines = [f"{emo} <b>{act}</b> | {w['tier']}级 | <b>{e(ev['coin'])}</b>"]
    lines.append(f"钱包: <code>{w['short']}</code> {e(w['label'])}")
    cls_s = CLS.get(cls, "")
    m = f" (HL价格为每{mult}枚)" if mult != 1 else ""
    lines.append(f"OKX: <code>{inst}</code>{' · ' + cls_s if cls_s else ''}{m}")
    a = ev["action"]
    if a == "close":
        lines.append(f"平仓规模: {usd(ev['prev_value'])} ({abs(ev['prev_szi']):g})")
        lines.append(f"原开仓均价: {px(ev['entry_px'])} | 参考平仓价: {px(fill_px or ev['mark_px'])}")
        lines.append(f"平仓前浮盈: {usd(ev['prev_upnl'], True)} (约等于已实现)")
        if ev.get("lev"):
            lines.append(f"杠杆: {ev['lev']}x {'全仓' if ev.get('lev_type') == 'cross' else '逐仓'}")
    else:
        if a in ("add", "reduce"):
            chg = (abs(ev["szi"]) / abs(ev["prev_szi"]) - 1) * 100 if ev["prev_szi"] else 0
            lines.append(f"仓位: {usd(ev['prev_value'])} → <b>{usd(ev['value'])}</b> ({chg:+.0f}%, {abs(ev['szi']):g})")
        elif a == "flip":
            lines.append(f"原仓位: {'多' if ev['prev_szi'] > 0 else '空'} {usd(ev['prev_value'])} → 现 <b>{usd(ev['value'])}</b> ({abs(ev['szi']):g})")
        else:
            late = " (分批建仓达阈值)" if ev.get("late_open") else ""
            lines.append(f"仓位: <b>{usd(ev['value'])}</b> ({abs(ev['szi']):g}){late}")
        lines.append(f"开仓均价: {px(ev['entry_px'])} | 标记价: {px(ev['mark_px'])}")
        lev = f"{ev['lev']}x {'全仓' if ev.get('lev_type') == 'cross' else '逐仓'}" if ev.get("lev") else "n/a"
        liq = f" | 强平价: {px(ev['liq_px'])}" if ev.get("liq_px") else ""
        lines.append(f"杠杆: {lev}{liq}")
        roe = f" ({ev['roe'] * 100:+.1f}%)" if ev.get("roe") is not None else ""
        lines.append(f"未实现盈亏: {usd(ev['upnl'], True)}{roe}")
    lines.append(f"30日胜率: {w['win30']} | PF {w['pf']} | 样本交易 {w['trades']}")
    lines.append(f"🔗 {cfg['explorer_url']}{w['address']}")
    lines.append(f"🕒 {ts(t, cfg['tz_offset_h'])}")
    return "\n".join(lines)


def confluence_text(coin: str, inst: str, side: str, members: List[dict], cfg: dict,
                    now: Optional[float] = None) -> str:
    now = now or time.time()
    d = "做多" if side == "long" else "做空"
    emo = "🟩" if side == "long" else "🟥"
    lines = [f"🔥🔥 <b>共振信号</b> {emo} <b>{html.escape(coin)} {d}</b>",
             f"{cfg['confluence_window_h']}小时内 <b>{len(members)}</b> 个A/B级钱包同向开仓 → <code>{inst}</code>"]
    for m in sorted(members, key=lambda x: x["t"]):
        ago = int((now - m["t"]) / 60)
        lines.append(f"• {m['tier']} <code>{m['short']}</code> {html.escape(m.get('label', ''))} "
                     f"{usd(m.get('value'))} @ {px(m.get('entry_px'))} ({ago}分钟前)")
    lines.append(f"🕒 {ts(now, cfg['tz_offset_h'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------- brief format (v2)
BRIEF = {
    ("open", "long"): ("🟢", "开多"), ("open", "short"): ("🔴", "开空"),
    ("add", "long"): ("🟢", "加仓多"), ("add", "short"): ("🔴", "加仓空"),
    ("reduce", "long"): ("⚪", "减仓多"), ("reduce", "short"): ("⚪", "减仓空"),
    ("close", "long"): ("⚪", "平多"), ("close", "short"): ("⚪", "平空"),
    ("flip", "long"): ("🔄", "反手开多"), ("flip", "short"): ("🔄", "反手开空"),
}


def signal_brief(ev: dict, w: dict, n_wallets: int, price: Optional[float], track_line: str,
                 advice: str) -> str:
    emo, act = BRIEF[(ev["action"], ev["side"])]
    coin = html.escape(ev["coin"].split(":")[-1])
    a = ev["action"]
    lines = [f"{emo} <b>{coin} {act}</b> | {w['tier']}级 第{w['rank']}/{n_wallets}名"]
    lev = f" · {ev['lev']}x" if ev.get("lev") and a != "close" else ""
    if a == "close":
        lines.append(f"平仓 {usd(ev['prev_value'])} · 价 {px(price)} · 盈亏 {usd(ev['prev_upnl'], True)}")
    elif a in ("add", "reduce"):
        lines.append(f"仓位 {usd(ev['prev_value'])} → <b>{usd(ev['value'])}</b>{lev} · 价 {px(price)}")
    else:
        lines.append(f"仓位 <b>{usd(ev['value'])}</b>{lev} · 价 {px(price)}")
    lines.append(track_line)
    lines.append(f"建议：{advice}")
    return "\n".join(lines)


def confluence_brief(coin: str, side: str, members: List[dict], n_wallets: int, ranks: dict,
                     track_line: str, advice: str) -> str:
    d = "做多" if side == "long" else "做空"
    emo = "🟢" if side == "long" else "🔴"
    tiers = {}
    for m in members:
        tiers[m["tier"]] = tiers.get(m["tier"], 0) + 1
    ts_ = " ".join(f"{k}×{v}" for k, v in sorted(tiers.items()))
    total = sum(m.get("value") or 0 for m in members)
    lines = [f"🔥 <b>共振 {html.escape(coin)} {d}</b> {emo} | {len(members)}个钱包 ({ts_})",
             f"合计仓位 <b>{usd(total)}</b>"]
    for m in sorted(members, key=lambda x: x["t"]):
        lines.append(f"• {m['tier']}级第{ranks.get(m['addr'], '?')}名 {usd(m.get('value'))} @ {px(m.get('entry_px'))}")
    lines.append(track_line)
    lines.append(f"建议：{advice}")
    return "\n".join(lines)
