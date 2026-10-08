"""Config: config.json + .env file + environment variables (env wins)."""
from __future__ import annotations

import csv
import json
import os
from typing import Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "watchlist": "watchlist.json",
    "wallets_csv": "smart_wallets.csv",
    "okx_bases": "okx_usdt_perp_bases.json",
    "okx_categories": "okx_noncrypto_categories.json",
    "state_file": "state.json",
    "signals_log": "signals.log",
    "bot_log": "bot.log",
    "min_notional_usd": 50000,
    "alert_tiers": ["A", "B"],
    "confluence_tiers": ["A", "B"],
    "confluence_window_h": 4,
    "confluence_min_wallets": 2,
    "change_threshold": 0.20,
    "quiet_mode": False,
    "quiet_hours": None,
    "tz_offset_h": 8,
    "max_weight_per_min": 750,
    "max_inflight": 6,
    "hip3_poll_s": 30,
    "hip3_default_dexes": ["xyz"],
    "hip3_rescan_min": 30,
    "ws_reconcile_s": 60,
    "ws_debounce_s": 1.5,
    "ws_ping_s": 50,
    "backoff_429_s": 10,
    "restart_rediff_max_age_s": 600,
    "stats_every_s": 60,
    "labels": {},
    "coin_overrides": {},
    "exclude_coins": [],
    "log_level": "INFO",
    "api_url": "https://api.hyperliquid.xyz/info",
    "ws_url": "wss://api.hyperliquid.xyz/ws",
    "explorer_url": "https://app.hyperliquid.xyz/explorer/address/",
}


def _load_dotenv(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def _bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_config(path: str = None) -> dict:
    _load_dotenv(os.path.join(HERE, ".env"))
    cfg = dict(DEFAULTS)
    path = path or os.environ.get("BOT_CONFIG") or os.path.join(HERE, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    env = os.environ
    cfg["telegram_token"] = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    cfg["telegram_chat_id"] = env.get("TELEGRAM_CHAT_ID", "").strip()
    if env.get("MIN_NOTIONAL_USD"):
        cfg["min_notional_usd"] = float(env["MIN_NOTIONAL_USD"])
    if env.get("ALERT_TIERS"):
        cfg["alert_tiers"] = [t.strip().upper() for t in env["ALERT_TIERS"].split(",") if t.strip()]
    if env.get("QUIET_MODE"):
        cfg["quiet_mode"] = _bool(env["QUIET_MODE"])
    if env.get("MAX_WEIGHT_PER_MIN"):
        cfg["max_weight_per_min"] = int(env["MAX_WEIGHT_PER_MIN"])
    # chat id is optional: without it the bot binds to the first private chat that sends /start
    cfg["log_only"] = _bool(env.get("LOG_ONLY", "0")) or not cfg["telegram_token"]
    for k in ("watchlist", "wallets_csv", "okx_bases", "okx_categories", "state_file",
              "signals_log", "bot_log"):
        if not os.path.isabs(cfg[k]):
            cfg[k] = os.path.join(HERE, cfg[k])
    return cfg


def load_wallets(cfg: dict) -> List[dict]:
    with open(cfg["watchlist"], encoding="utf-8") as f:
        wl = json.load(f)
    stats: Dict[str, dict] = {}
    if os.path.exists(cfg["wallets_csv"]):
        with open(cfg["wallets_csv"], encoding="utf-8") as f:
            for row in csv.DictReader(f):
                stats[row["address"].lower()] = row
    out = []
    for i, w in enumerate(wl, 1):
        a = w["address"].lower()
        s = stats.get(a, {})
        coins = [c.split(":")[0] for c in (s.get("main_coins") or "").split() if c][:2]
        label = cfg["labels"].get(a) or ("/".join(coins) if coins else "")
        out.append({
            "rank": i,
            "address": a,
            "short": a[:6] + "…" + a[-4:],
            "tier": (w.get("tier") or s.get("tier") or "C").upper(),
            "ws": w.get("monitor") == "ws_userFills",
            "hip3": bool(w.get("hip3_dex_poll")),
            "label": label,
            "win30": _pct(s.get("win_rate_30d")),
            "pf": s.get("profit_factor") or "",
            "trades": s.get("closed_trades") or "",
            "hold_h": s.get("median_hold_h") or "",
            "flag": s.get("red_flag") or "",
        })
    return out


def _pct(x):
    try:
        return f"{float(x) * 100:.0f}%"
    except (TypeError, ValueError):
        return "n/a"
