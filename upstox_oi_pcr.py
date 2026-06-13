"""
upstox_oi_pcr.py
================
Foundation tool for a Quantsapp-style Modified OI-PCR model.

What it does
------------
1. Enumerates the option strikes (CE/PE) for an underlying + expiry.
2. Pulls DAILY historical candles for each option leg -- each candle carries
   Open Interest (7th field) and Close (used as the day's premium / LTP proxy).
3. Pulls the underlying FUTURES candle for the price line.
4. Writes everything to partitioned Parquet (your free local store).
5. Aggregates per day into:
       PCR     = sum(put_oi)            / sum(call_oi)
       PCR_M   = sum(put_oi * put_ltp)  / sum(call_oi * call_ltp)        (premium-weighted)
       PCR_M_ATM = same but restricted to +/- N strikes around the ATM   (Quantsapp-style)

Two data paths
--------------
* LIVE expiry  (free tier): regular V3 endpoint
      GET /v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}
* EXPIRED expiries (needs Upstox PLUS): expired-instruments endpoints
      GET /v2/expired-instruments/option/contract?instrument_key=..&expiry_date=..
      GET /v2/expired-instruments/historical-candle/{exp_key}/{interval}/{to_date}/{from_date}

SECURITY: the access token is read from the UPSTOX_ACCESS_TOKEN environment
variable. Never hard-code it. Run this on your own machine.

Usage
-----
    export UPSTOX_ACCESS_TOKEN="your_token_here"
    python upstox_oi_pcr.py fetch  --underlying "NSE_INDEX|Nifty 50" --expiry 2024-04-25 --from 2024-02-01
    python upstox_oi_pcr.py pcr    --underlying "NSE_INDEX|Nifty 50"
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests

import config

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
config.load_env()        # populate os.environ from .env (real env wins)

BASE = "https://api.upstox.com"
DATA_DIR = Path(os.environ.get("OI_PCR_DATA_DIR", "./data"))
ATM_WINDOW = 10          # +/- strikes around ATM for the PCR_M_ATM variant
REQ_PER_SEC = 25         # stay well under the 50/sec limit
_TOKEN_ENV = "UPSTOX_ACCESS_TOKEN"


@dataclass
class Contract:
    instrument_key: str   # for expired legs this is the expired_instrument_key
    opt_type: str         # "CE" / "PE"
    strike: float
    expiry: str
    expired: bool


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #
class Upstox:
    def __init__(self, token: str | None = None):
        self.token = token or os.environ.get(_TOKEN_ENV)
        if not self.token:
            sys.exit(f"ERROR: set {_TOKEN_ENV} in your environment first.")
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        })
        self._last = 0.0

    def _throttle(self):
        gap = 1.0 / REQ_PER_SEC
        wait = gap - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _get(self, url: str, params: dict | None = None) -> dict:
        self._throttle()
        for attempt in range(4):
            r = self.s.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:                      # rate limited -> back off
                time.sleep(2 ** attempt)
                continue
            # surface the useful Upstox error body and stop retrying on 4xx
            raise RuntimeError(f"{r.status_code} {url} -> {r.text[:300]}")
        raise RuntimeError(f"giving up after retries: {url}")

    # ----- contract enumeration ------------------------------------------- #
    def live_option_contracts(self, underlying: str, expiry: str) -> list[Contract]:
        """Active (not yet expired) expiry -- free tier."""
        url = f"{BASE}/v2/option/contract"
        data = self._get(url, {"instrument_key": underlying}).get("data", [])
        out = []
        for d in data:
            if d.get("expiry") == expiry and d.get("instrument_type") in ("CE", "PE"):
                out.append(Contract(d["instrument_key"], d["instrument_type"],
                                    float(d["strike_price"]), expiry, expired=False))
        return out

    def expired_option_contracts(self, underlying: str, expiry: str) -> list[Contract]:
        """Already-expired expiry -- requires Upstox PLUS."""
        url = f"{BASE}/v2/expired-instruments/option/contract"
        data = self._get(url, {"instrument_key": underlying, "expiry_date": expiry}).get("data", [])
        out = []
        for d in data:
            if d.get("instrument_type") in ("CE", "PE"):
                out.append(Contract(d["instrument_key"], d["instrument_type"],
                                    float(d["strike_price"]), expiry, expired=True))
        return out

    # ----- candles --------------------------------------------------------- #
    def candles_live(self, key: str, frm: str, to: str) -> list[list]:
        url = f"{BASE}/v3/historical-candle/{quote(key, safe='')}/days/1/{to}/{frm}"
        return self._get(url).get("data", {}).get("candles", [])

    def candles_expired(self, exp_key: str, frm: str, to: str) -> list[list]:
        url = f"{BASE}/v2/expired-instruments/historical-candle/{quote(exp_key, safe='')}/day/{to}/{frm}"
        return self._get(url).get("data", {}).get("candles", [])

    def candles(self, c: Contract, frm: str, to: str) -> list[list]:
        return self.candles_expired(c.instrument_key, frm, to) if c.expired \
            else self.candles_live(c.instrument_key, frm, to)


# --------------------------------------------------------------------------- #
# Fetch -> long-form Parquet
# --------------------------------------------------------------------------- #
# candle layout: [timestamp, open, high, low, close, volume, oi]
_COLS = ["ts", "open", "high", "low", "close", "volume", "oi"]


def _slug(underlying: str) -> str:
    return underlying.replace("|", "_").replace(" ", "_").replace(":", "_")


def fetch(underlying: str, expiry: str, frm: str, to: str, expired: bool) -> Path:
    api = Upstox()
    contracts = (api.expired_option_contracts(underlying, expiry) if expired
                 else api.live_option_contracts(underlying, expiry))
    if not contracts:
        sys.exit("No contracts returned -- check underlying key / expiry / plan.")

    print(f"{len(contracts)} legs for {underlying} @ {expiry}; pulling daily candles...")
    rows = []
    for i, c in enumerate(contracts, 1):
        try:
            for cd in api.candles(c, frm, to):
                rec = dict(zip(_COLS, cd))
                rec.update(strike=c.strike, opt_type=c.opt_type, expiry=expiry)
                rows.append(rec)
        except RuntimeError as e:
            print(f"  ! {c.instrument_key}: {e}")
        if i % 25 == 0:
            print(f"  ...{i}/{len(contracts)}")

    if not rows:
        sys.exit("No candle data -- nothing written.")

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["ts"]).dt.date.astype(str)
    df["underlying"] = underlying

    out = DATA_DIR / _slug(underlying) / f"expiry={expiry}"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "chain.parquet"
    df.to_parquet(path, index=False)
    print(f"wrote {len(df):,} rows -> {path}")

    # Underlying spot/price line: anchors ATM detection and is the step-2 price axis.
    _fetch_underlying_price(api, underlying, frm, to)
    return path


def _fetch_underlying_price(api: "Upstox", underlying: str, frm: str, to: str) -> None:
    """Pull the underlying's own daily candles -> data/<slug>/price.parquet.

    Merged across calls and de-duplicated by date so the price history
    accumulates as new expiries are fetched. ``close`` is the daily spot.
    """
    try:
        candles = api.candles_live(underlying, frm, to)
    except RuntimeError as e:
        print(f"  ! underlying price fetch failed ({e}); ATM will fall back to parity")
        return
    if not candles:
        return
    px = pd.DataFrame([dict(zip(_COLS, c)) for c in candles])
    px["date"] = pd.to_datetime(px["ts"]).dt.date.astype(str)
    px = px[["date", "close"]].rename(columns={"close": "spot"})

    out = DATA_DIR / _slug(underlying) / "price.parquet"
    if out.exists():
        prev = pd.read_parquet(out)
        px = pd.concat([prev, px]).drop_duplicates("date", keep="last")
    px = px.sort_values("date")
    px.to_parquet(out, index=False)
    print(f"wrote {len(px):,} price rows -> {out}")


# --------------------------------------------------------------------------- #
# Aggregate -> PCR / Modified OI-PCR  (DuckDB over the Parquet store)
# --------------------------------------------------------------------------- #
def pcr(underlying: str, atm_window: int = ATM_WINDOW) -> pd.DataFrame:
    import duckdb

    chain_glob = str(DATA_DIR / _slug(underlying) / "expiry=*" / "*.parquet")
    price_path = DATA_DIR / _slug(underlying) / "price.parquet"
    con = duckdb.connect()

    # Spot table is optional: if price.parquet is missing we fall back to the
    # put-call-parity proxy for ATM (less reliable on illiquid daily closes).
    if price_path.exists():
        spot_cte = f"""
        spot AS (SELECT date, spot FROM read_parquet('{price_path}'))"""
        # ATM = listed strike nearest to spot
        atm_pick_sql = """
        atm_pick AS (
            SELECT sr.date, sr.strike AS atm_strike,
                   ROW_NUMBER() OVER (PARTITION BY sr.date
                                      ORDER BY ABS(sr.strike - s.spot)) AS rn
            FROM strike_ranks sr JOIN spot s USING(date)
        )"""
    else:
        spot_cte = "\n        spot AS (SELECT NULL::VARCHAR AS date, NULL::DOUBLE AS spot WHERE 1=0)"
        # ATM proxy: strike where |CE premium - PE premium| is smallest
        atm_pick_sql = """
        atm AS (
            SELECT date, strike,
                   ABS(SUM(CASE WHEN opt_type='CE' THEN ltp ELSE -ltp END)) AS ce_pe_gap
            FROM raw GROUP BY date, strike
        ),
        atm_pick AS (
            SELECT date, strike AS atm_strike,
                   ROW_NUMBER() OVER (PARTITION BY date ORDER BY ce_pe_gap) AS rn
            FROM atm
        )"""

    q = f"""
    WITH raw AS (
        SELECT date, strike, opt_type, oi, close AS ltp
        FROM read_parquet('{chain_glob}', hive_partitioning = true)
        WHERE oi IS NOT NULL AND close IS NOT NULL
    ),
    -- rank each distinct strike within its date (1,2,3,... low->high)
    strike_ranks AS (
        SELECT date, strike,
               DENSE_RANK() OVER (PARTITION BY date ORDER BY strike) AS strike_rank
        FROM (SELECT DISTINCT date, strike FROM raw)
    ),
    {spot_cte},
    {atm_pick_sql},
    atm_final AS (                       -- ATM strike + its rank, one row per date
        SELECT ap.date, ap.atm_strike, sr.strike_rank AS atm_rank
        FROM (SELECT date, atm_strike FROM atm_pick WHERE rn=1) ap
        JOIN strike_ranks sr ON sr.date = ap.date AND sr.strike = ap.atm_strike
    ),
    banded AS (
        SELECT r.*, sr.strike_rank, af.atm_strike, af.atm_rank
        FROM raw r
        JOIN strike_ranks sr USING(date, strike)
        JOIN atm_final     af USING(date)
    )
    SELECT
        date,
        -- standard OI-PCR
        SUM(CASE WHEN opt_type='PE' THEN oi END)
          / NULLIF(SUM(CASE WHEN opt_type='CE' THEN oi END),0)                         AS pcr,
        -- premium-weighted (modified) OI-PCR, full chain
        SUM(CASE WHEN opt_type='PE' THEN oi*ltp END)
          / NULLIF(SUM(CASE WHEN opt_type='CE' THEN oi*ltp END),0)                     AS pcr_m,
        -- modified OI-PCR restricted to ATM +/- window
        SUM(CASE WHEN opt_type='PE' AND ABS(strike_rank-atm_rank)<={atm_window} THEN oi*ltp END)
          / NULLIF(SUM(CASE WHEN opt_type='CE' AND ABS(strike_rank-atm_rank)<={atm_window} THEN oi*ltp END),0)
                                                                                       AS pcr_m_atm,
        SUM(CASE WHEN opt_type='CE' THEN oi END) AS call_oi,
        SUM(CASE WHEN opt_type='PE' THEN oi END) AS put_oi,
        MAX(atm_strike) AS atm_strike
    FROM banded
    GROUP BY date
    ORDER BY date
    """
    df = con.execute(q).df()
    out = DATA_DIR / _slug(underlying) / "pcr_daily.parquet"
    df.to_parquet(out, index=False)
    print(f"PCR table: {len(df)} days -> {out}")
    return df


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description="Upstox OI / Modified-OI-PCR foundation tool")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="pull option-leg candles -> Parquet")
    f.add_argument("--underlying", required=True, help='e.g. "NSE_INDEX|Nifty 50"')
    f.add_argument("--expiry", required=True, help="YYYY-MM-DD")
    f.add_argument("--from", dest="frm", required=True, help="YYYY-MM-DD")
    f.add_argument("--to", default=dt.date.today().isoformat(), help="YYYY-MM-DD (default today)")
    f.add_argument("--expired", action="store_true",
                   help="use expired-instruments endpoints (needs Upstox PLUS)")

    a = sub.add_parser("pcr", help="aggregate Parquet -> daily PCR / PCR_M table")
    a.add_argument("--underlying", required=True)
    a.add_argument("--atm-window", type=int, default=ATM_WINDOW)

    args = p.parse_args()
    if args.cmd == "fetch":
        fetch(args.underlying, args.expiry, args.frm, args.to, args.expired)
    elif args.cmd == "pcr":
        df = pcr(args.underlying, args.atm_window)
        print(df.tail(15).to_string(index=False))


if __name__ == "__main__":
    main()
