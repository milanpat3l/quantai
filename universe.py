"""
universe.py
===========
Instrument-universe discovery for the OI-PCR toolkit, driven by Upstox's
**complete instrument master** (one cached download covers every exchange).

Why master-driven: option legs for ANY underlying — NSE stocks & indices, BSE
indices, MCX commodities — are enumerated directly from the master, uniformly,
with no per-underlying `/option/contract` call (which doesn't behave the same
across exchanges). Candles are then pulled per leg via the normal V3 endpoint.

Groups
------
    nse_index   NSE index options (Nifty 50, Bank Nifty, FinNifty, Midcap, ...)
    nse_stocks  NSE single-stock options (all F&O stocks)
    bse_index   BSE index options (SENSEX, BANKEX, SENSEX50)
    mcx         MCX commodity options (GOLD, SILVER, CRUDE OIL, NATURALGAS, ...)
    all         everything above

Each "underlying" is identified by its Upstox instrument_key; `display_name`
gives the human label (RELIANCE, GOLD, SENSEX, Nifty 50, ...).
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import os
import time
from pathlib import Path

import requests

DATA_DIR = Path(os.environ.get("OI_PCR_DATA_DIR", "./data"))
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
_CACHE = DATA_DIR / ".instruments_master.json"
_TTL = 7 * 86400

GROUPS = {
    "nse_index":  lambda x: x.get("segment") == "NSE_FO" and str(x.get("underlying_key", "")).startswith("NSE_INDEX"),
    "nse_stocks": lambda x: x.get("segment") == "NSE_FO" and str(x.get("underlying_key", "")).startswith("NSE_EQ"),
    "bse_index":  lambda x: x.get("segment") == "BSE_FO",
    "mcx":        lambda x: x.get("segment") == "MCX_FO",
}


def _norm_expiry(e) -> str:
    if isinstance(e, (int, float)):
        return dt.datetime.utcfromtimestamp(e / 1000).date().isoformat()
    return str(e)[:10]


def load_master(force: bool = False) -> list[dict]:
    """Return the derivatives subset (CE/PE/FUT) of the instrument master,
    cached locally (7-day TTL) so we download ~MBs at most weekly."""
    if not force and _CACHE.exists() and (time.time() - _CACHE.stat().st_mtime) < _TTL:
        try:
            return json.loads(_CACHE.read_text())
        except Exception:
            pass
    r = requests.get(MASTER_URL, timeout=120)
    r.raise_for_status()
    data = json.load(gzip.GzipFile(fileobj=io.BytesIO(r.content)))
    deriv = [x for x in data if x.get("instrument_type") in ("CE", "PE", "FUT")]
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE.write_text(json.dumps(deriv))
    return deriv


def _name(x: dict) -> str:
    return (x.get("underlying_symbol") or x.get("name")
            or str(x.get("underlying_key", "")).split("|")[-1])


def underlyings(group: str) -> list[tuple[str, str]]:
    """(underlying_key, display_name) for every option underlying in a group.

    De-duplicated by display name to the nearest *active* contract — important
    for MCX, where each commodity has one option chain per monthly future
    (e.g. COPPER appears 4×). We keep the chain with the soonest non-expired
    expiry so each commodity shows up once.
    """
    master = load_master()
    if group == "all":
        preds = list(GROUPS.values())
        pred = lambda x: any(p(x) for p in preds)
    else:
        if group not in GROUPS:
            raise SystemExit(f"unknown group '{group}'. choose: {', '.join(GROUPS)}, all")
        pred = GROUPS[group]

    today = dt.date.today().isoformat()
    # per underlying_key: (name, soonest future expiry)
    info: dict[str, tuple[str, str]] = {}
    for x in master:
        if x.get("instrument_type") not in ("CE", "PE") or not pred(x):
            continue
        uk = x.get("underlying_key")
        if not uk:
            continue
        exp = _norm_expiry(x.get("expiry"))
        fexp = exp if exp >= today else "9999"
        cur = info.get(uk)
        if cur is None or fexp < cur[1]:
            info[uk] = (_name(x), fexp)

    # dedupe by name -> key with soonest front expiry
    by_name: dict[str, tuple[str, str]] = {}
    for uk, (nm, fexp) in info.items():
        if nm not in by_name or fexp < by_name[nm][1]:
            by_name[nm] = (uk, fexp)
    return sorted(((uk, nm) for nm, (uk, _) in by_name.items()), key=lambda kv: kv[1])


def display_name(underlying_key: str) -> str:
    for x in load_master():
        if x.get("underlying_key") == underlying_key and x.get("instrument_type") in ("CE", "PE"):
            return _name(x)
    return underlying_key.split("|")[-1]


def expiries(underlying_key: str) -> list[str]:
    s = {(_norm_expiry(x.get("expiry"))) for x in load_master()
         if x.get("underlying_key") == underlying_key and x.get("instrument_type") in ("CE", "PE")}
    return sorted(s)


def front_expiry(underlying_key: str, on: str | None = None) -> str | None:
    on = on or dt.date.today().isoformat()
    fut = [e for e in expiries(underlying_key) if e >= on]
    return fut[0] if fut else None


def legs(underlying_key: str, expiry: str) -> list[dict]:
    """Option legs (CE/PE) for an underlying + expiry, from the master.
    Returns dicts: {instrument_key, opt_type, strike, expiry}."""
    out = []
    for x in load_master():
        if (x.get("underlying_key") == underlying_key
                and x.get("instrument_type") in ("CE", "PE")
                and _norm_expiry(x.get("expiry")) == expiry):
            out.append({"instrument_key": x["instrument_key"],
                        "opt_type": x["instrument_type"],
                        "strike": float(x.get("strike_price") or 0.0),
                        "expiry": expiry})
    return out


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="List the option universe")
    p.add_argument("--group", default="nse_index",
                   choices=list(GROUPS) + ["all"])
    args = p.parse_args()
    us = underlyings(args.group)
    print(f"{len(us)} underlyings in '{args.group}':")
    for uk, nm in us:
        fe = front_expiry(uk)
        print(f"  {nm:<16} {uk:<28} front_expiry={fe}")


if __name__ == "__main__":
    main()
