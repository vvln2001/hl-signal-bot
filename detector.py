"""Position-state diff -> signals. Pure functions, no I/O (unit-tested).

A "book" is {coin: pos} for one wallet on one dex, where pos is a dict:
  szi (signed size), entry_px, value (USD notional at mark), mark_px,
  lev, lev_type, upnl, roe, liq_px,
  ref   : signed size at the last announced signal (base for the +-20% rule)
  ann   : True once the position has been announced (or was in the baseline)
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

EPS = 1e-12


def parse_clearinghouse(resp: dict) -> Dict[str, dict]:
    """clearinghouseState response -> book (without ref/ann)."""
    book: Dict[str, dict] = {}
    for ap in (resp or {}).get("assetPositions", []) or []:
        p = ap.get("position") or {}
        try:
            szi = float(p.get("szi") or 0)
        except (TypeError, ValueError):
            continue
        if abs(szi) < EPS:
            continue
        val = _f(p.get("positionValue"))
        lev = p.get("leverage") or {}
        book[p["coin"]] = {
            "szi": szi,
            "entry_px": _f(p.get("entryPx")),
            "value": val,
            "mark_px": (val / abs(szi)) if val else None,
            "lev": lev.get("value"),
            "lev_type": lev.get("type"),
            "upnl": _f(p.get("unrealizedPnl")),
            "roe": _f(p.get("returnOnEquity")),
            "liq_px": _f(p.get("liquidationPx")),
        }
    return book


def _f(x) -> Optional[float]:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def baseline(book: Dict[str, dict]) -> Dict[str, dict]:
    """Mark every position as already known: no alert, ref = current size."""
    out = {}
    for c, p in book.items():
        q = dict(p)
        q["ref"] = p["szi"]
        q["ann"] = True
        out[c] = q
    return out


def diff(prev: Dict[str, dict], cur: Dict[str, dict], threshold: float = 0.20,
         min_notional: float = 0.0) -> Tuple[List[dict], Dict[str, dict]]:
    """Compare two books. Returns (events, new_book_with_ref_ann).

    Actions: open, add, reduce, close, flip.
    Each event carries `suppressed=True` when it is below min_notional (or is
    the close/reduce of a position that was never announced); the caller logs
    suppressed events but does not alert.
    A suppressed open stays un-announced; once it grows past min_notional an
    `open` is emitted then (so a position built in small clips still alerts once).
    """
    events: List[dict] = []
    new: Dict[str, dict] = {}
    for coin in sorted(set(prev) | set(cur)):
        p = prev.get(coin)
        q = cur.get(coin)
        if q is not None:
            q = dict(q)
        ps = p["szi"] if p else 0.0
        qs = q["szi"] if q else 0.0
        if q is None and p is None:
            continue
        # ---------- open ----------
        if _sign(ps) == 0 and _sign(qs) != 0:
            q["ref"] = qs
            ev = _ev("open", coin, p, q)
            ev["suppressed"] = (q.get("value") or 0) < min_notional
            q["ann"] = not ev["suppressed"]
            events.append(ev)
            new[coin] = q
            continue
        # ---------- close ----------
        if _sign(ps) != 0 and _sign(qs) == 0:
            ev = _ev("close", coin, p, None)
            ev["suppressed"] = (not p.get("ann", True)) or (p.get("value") or 0) < min_notional
            events.append(ev)
            continue
        # ---------- flip ----------
        if _sign(ps) != _sign(qs):
            q["ref"] = qs
            ev = _ev("flip", coin, p, q)
            ev["suppressed"] = max(q.get("value") or 0, p.get("value") or 0) < min_notional
            q["ann"] = not ev["suppressed"]
            events.append(ev)
            new[coin] = q
            continue
        # ---------- same side: add / reduce / nothing ----------
        ref = p.get("ref", ps) or ps
        ann = p.get("ann", True)
        q["ref"] = ref
        q["ann"] = ann
        if not ann:
            # pending open (was below min_notional): announce when it qualifies
            q["ref"] = qs
            if (q.get("value") or 0) >= min_notional:
                ev = _ev("open", coin, p, q)
                ev["suppressed"] = False
                ev["late_open"] = True
                q["ann"] = True
                events.append(ev)
            new[coin] = q
            continue
        ratio = abs(qs) / abs(ref) if abs(ref) > EPS else 1.0
        if ratio >= 1.0 + threshold:
            q["ref"] = qs
            ev = _ev("add", coin, p, q)
            ev["ratio"] = ratio
            ev["suppressed"] = (q.get("value") or 0) < min_notional
            events.append(ev)
        elif ratio <= 1.0 - threshold:
            q["ref"] = qs
            ev = _ev("reduce", coin, p, q)
            ev["ratio"] = ratio
            ev["suppressed"] = (p.get("value") or 0) < min_notional
            events.append(ev)
        new[coin] = q
    return events, new


def _ev(action: str, coin: str, p: Optional[dict], q: Optional[dict]) -> dict:
    ref = q if q is not None else p
    side = "long" if ref["szi"] > 0 else "short"
    return {
        "action": action,
        "coin": coin,
        "side": side,
        "szi": q["szi"] if q else 0.0,
        "prev_szi": p["szi"] if p else 0.0,
        "value": q.get("value") if q else None,
        "prev_value": p.get("value") if p else None,
        "entry_px": q.get("entry_px") if q else (p.get("entry_px") if p else None),
        "mark_px": q.get("mark_px") if q else (p.get("mark_px") if p else None),
        "lev": (q or p).get("lev"),
        "lev_type": (q or p).get("lev_type"),
        "upnl": q.get("upnl") if q else None,
        "roe": q.get("roe") if q else None,
        "prev_upnl": p.get("upnl") if p else None,
        "liq_px": q.get("liq_px") if q else None,
    }
