"""Hyperliquid coin -> OKX USDT perpetual instId."""
from __future__ import annotations

import json
from typing import Dict, Optional, Tuple


class CoinMapper:
    def __init__(self, bases_path: str, noncrypto_path: Optional[str] = None,
                 overrides: Optional[Dict[str, str]] = None):
        with open(bases_path, encoding="utf-8") as f:
            self.bases = set(json.load(f))
        self.cat: Dict[str, str] = {}
        if noncrypto_path:
            try:
                with open(noncrypto_path, encoding="utf-8") as f:
                    self.cat = json.load(f)
            except FileNotFoundError:
                pass
        self.overrides = overrides or {}

    def map(self, coin: str) -> Tuple[Optional[str], str, int]:
        """Return (instId or None, note, multiplier).

        multiplier = 1000 for k-prefixed / 1000-prefixed coins (price shown on HL
        is per 1000 units). note explains a skip or the asset class.
        """
        if coin.startswith("@"):
            return None, "spot pair", 1
        if coin in self.overrides:
            base = self.overrides[coin]
            return (f"{base}-USDT-SWAP" if base in self.bases else None,
                    "override" if base in self.bases else f"override {base} not on OKX", 1)
        dex, sym = ("", coin)
        if ":" in coin:
            dex, sym = coin.split(":", 1)
        mult = 1
        base = sym
        if sym not in self.bases:
            if len(sym) > 1 and sym[0] == "k" and sym[1:] in self.bases:
                base, mult = sym[1:], 1000
            elif sym.startswith("1000") and sym[4:] in self.bases:
                base, mult = sym[4:], 1000
        if base not in self.bases:
            kind = "HIP-3 stock/commodity" if dex else "crypto"
            return None, f"no OKX USDT swap for {kind} {sym}", mult
        cls = self.cat.get(base, "crypto")
        return f"{base}-USDT-SWAP", cls, mult
