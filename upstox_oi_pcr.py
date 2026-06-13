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
import gzip
import io
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests

import config
import universe

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
    def option_contracts_raw(self, underlying: str) -> list[dict]:
        """All live option contracts for an underlying (all listed expiries)."""
        url = f"{BASE}/v2/option/contract"
        return self._get(url, {"instrument_key": underlying}).get("data", [])

    def live_option_contracts(self, underlying: str, expiry: str) -> list[Contract]:
        """Active (not yet expired) expiry -- free tier."""
        out = []
        for d in self.option_contracts_raw(underlying):
            if d.get("expiry") == expiry and d.get("instrument_type") in ("CE", "PE"):
                out.append(Contract(d["instrument_key"], d["instrument_type"],
                                    float(d["strike_price"]), expiry, expired=False))
        return out

    def front_expiry(self, underlying: str, on: str | None = None) -> str:
        """Nearest listed expiry on/after ``on`` (default today). YYYY-MM-DD."""
        on = on or dt.date.today().isoformat()
        expiries = sorted({d["expiry"] for d in self.option_contracts_raw(underlying)
                           if d.get("instrument_type") in ("CE", "PE")})
        future = [e for e in expiries if e >= on]
        if not future:
            sys.exit(f"No live expiry on/after {on} for {underlying}.")
        return future[0]

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


# de-dup key for the chain store: one row per (date, strike, opt_type) per expiry
_CHAIN_KEY = ["date", "strike", "opt_type"]


def _pull_legs(api: "Upstox", contracts: list[Contract], frm: str, to: str,
               expiry: str) -> pd.DataFrame:
    """Pull daily candles for every leg into a long-form DataFrame."""
    print(f"{len(contracts)} legs @ {expiry}; pulling daily candles {frm}..{to} ...")
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
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["ts"]).dt.date.astype(str)
    return df


def _merge_chain(df: pd.DataFrame, underlying: str, expiry: str,
                 name: str | None = None) -> Path:
    """Idempotently merge new rows into this expiry's chain.parquet.

    Existing rows with the same (date, strike, opt_type) are overwritten by the
    new pull, so re-running (e.g. a daily logger) is safe and gap-filling works.
    """
    df = df.copy()
    df["underlying"] = underlying
    df["name"] = name or underlying
    out = DATA_DIR / _slug(underlying) / f"expiry={expiry}"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "chain.parquet"
    if path.exists():
        prev = pd.read_parquet(path)
        df = pd.concat([prev, df]).drop_duplicates(_CHAIN_KEY, keep="last")
    df = df.sort_values(_CHAIN_KEY)
    df.to_parquet(path, index=False)
    print(f"chain now {len(df):,} rows -> {path}")
    return path


def _spot_now(api: "Upstox", underlying_key: str) -> float | None:
    """Latest close of the underlying itself (index/stock spot, or the MCX
    future that backs commodity options) for ATM windowing."""
    to = dt.date.today().isoformat()
    frm = (dt.date.today() - dt.timedelta(days=10)).isoformat()
    try:
        cs = api.candles_live(underlying_key, frm, to)
    except RuntimeError:
        return None
    return cs[0][4] if cs else None


def _resolve_contracts(underlying_key: str, expiry: str, atm_window: int | None,
                       api: "Upstox") -> list[Contract]:
    """Master-driven option legs for any underlying/exchange. If ``atm_window``
    is set, keep only strikes within +/- that many ranks of the spot-nearest
    strike — the key lever for logging a broad universe without exploding calls.
    """
    rows = universe.legs(underlying_key, expiry)
    contracts = [Contract(r["instrument_key"], r["opt_type"], r["strike"], expiry, expired=False)
                 for r in rows]
    if atm_window and contracts:
        spot = _spot_now(api, underlying_key)
        if spot:
            strikes = sorted({c.strike for c in contracts})
            ai = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
            keep = set(strikes[max(0, ai - atm_window): ai + atm_window + 1])
            contracts = [c for c in contracts if c.strike in keep]
    return contracts


def fetch(underlying: str, expiry: str, frm: str, to: str, expired: bool,
          atm_window: int | None = None) -> Path:
    api = Upstox()
    if expired:
        contracts = api.expired_option_contracts(underlying, expiry)
    else:
        contracts = _resolve_contracts(underlying, expiry, atm_window, api)
    if not contracts:
        sys.exit("No contracts returned -- check underlying key / expiry / plan.")

    df = _pull_legs(api, contracts, frm, to, expiry)
    if df.empty:
        sys.exit("No candle data -- nothing written.")
    path = _merge_chain(df, underlying, expiry, universe.display_name(underlying))
    _fetch_underlying_price(api, underlying, frm, to)
    return path


def log_daily(underlying: str, lookback: int = 7, expiry: str | None = None,
              atm_window: int | None = None) -> Path | None:
    """Forward-logger: capture recent OI for the front (or given) expiry and
    merge into the store. Master-driven, so it works for NSE stocks/indices,
    BSE indices, and MCX commodities. Run daily after close (cron) so history
    accumulates on the free Analytics token.
    """
    api = Upstox()
    expiry = expiry or universe.front_expiry(underlying)
    if not expiry:
        print(f"  ! no live expiry for {underlying}; skipping")
        return None
    to = dt.date.today().isoformat()
    frm = (dt.date.today() - dt.timedelta(days=lookback)).isoformat()
    contracts = _resolve_contracts(underlying, expiry, atm_window, api)
    if not contracts:
        print(f"  ! no live contracts for {underlying} @ {expiry}; skipping")
        return None
    df = _pull_legs(api, contracts, frm, to, expiry)
    if df.empty:
        print("No new candle data (market holiday?) -- store unchanged.")
        return DATA_DIR / _slug(underlying) / f"expiry={expiry}" / "chain.parquet"
    path = _merge_chain(df, underlying, expiry, universe.display_name(underlying))
    _fetch_underlying_price(api, underlying, frm, to)
    return path


def log_universe(group: str, lookback: int = 7, atm_window: int | None = 20,
                 max_underlyings: int | None = None) -> None:
    """Log + aggregate an entire group (nse_index / nse_stocks / bse_index /
    mcx / all). ATM-windowed by default to keep the call volume sane across a
    broad universe; pass atm_window=None for full chains.
    """
    us = universe.underlyings(group)
    if max_underlyings:
        us = us[:max_underlyings]
    print(f"=== logging {len(us)} underlyings in '{group}' "
          f"(atm_window={atm_window}, lookback={lookback}) ===")
    ok = 0
    for i, (uk, nm) in enumerate(us, 1):
        print(f"[{i}/{len(us)}] {nm} ({uk})")
        try:
            if log_daily(uk, lookback, atm_window=atm_window):
                pcr(uk)
                ok += 1
        except Exception as e:
            print(f"  ! {nm} failed: {e}")
    print(f"=== done: {ok}/{len(us)} underlyings updated ===")


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

    # near-month FUTURES close (the true Quantsapp price line); spot stays the
    # ATM anchor. Best-effort: if the instrument master / future is unavailable
    # we keep just spot.
    fut_key = nearest_future_key(underlying)
    if fut_key:
        try:
            fc = api.candles_live(fut_key, frm, to)
            if fc:
                fdf = pd.DataFrame([dict(zip(_COLS, c)) for c in fc])
                fdf["date"] = pd.to_datetime(fdf["ts"]).dt.date.astype(str)
                px = px.merge(fdf[["date", "close"]].rename(columns={"close": "fut"}),
                              on="date", how="outer")
        except RuntimeError as e:
            print(f"  ! futures fetch failed ({e}); price line falls back to spot")

    out = DATA_DIR / _slug(underlying) / "price.parquet"
    if out.exists():
        prev = pd.read_parquet(out)
        px = pd.concat([prev, px])
    # one row per date, keeping the latest non-null value in each column
    px = px.sort_values("date").groupby("date", as_index=False).last()
    px.to_parquet(out, index=False)
    print(f"wrote {len(px):,} price rows -> {out}")


_FUT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


def nearest_future_key(underlying: str, on: str | None = None) -> str | None:
    """instrument_key of the nearest non-expired monthly FUTURE for ``underlying``.

    Uses Upstox's NSE instrument master, caching the FUT subset locally (7-day
    TTL) so we don't re-download ~2 MB on every run. Returns None if unavailable.
    """
    on = on or dt.date.today().isoformat()
    cache = DATA_DIR / ".instruments_fut_NSE.json"
    futs = None
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 7 * 86400:
        try:
            futs = json.loads(cache.read_text())
        except Exception:
            futs = None
    if futs is None:
        try:
            r = requests.get(_FUT_MASTER_URL, timeout=60)
            r.raise_for_status()
            data = json.load(gzip.GzipFile(fileobj=io.BytesIO(r.content)))
        except Exception as e:
            print(f"  ! instrument master unavailable ({e}); no futures line")
            return None
        futs = [x for x in data if x.get("instrument_type") == "FUT"]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(futs))

    cand = []
    for x in futs:
        if x.get("underlying_key") != underlying:
            continue
        exp = x.get("expiry")
        d = (dt.datetime.utcfromtimestamp(exp / 1000).date().isoformat()
             if isinstance(exp, (int, float)) else str(exp)[:10])
        if d >= on:
            cand.append((d, x["instrument_key"]))
    if not cand:
        return None
    return min(cand)[1]


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
        SELECT date, strike, opt_type, oi, volume, close AS ltp
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
        -- volume PCR (Quantsapp VOL-PCR), full chain
        SUM(CASE WHEN opt_type='PE' THEN volume END)
          / NULLIF(SUM(CASE WHEN opt_type='CE' THEN volume END),0)                     AS vol_pcr,
        -- volume PCR restricted to ATM +/- window
        SUM(CASE WHEN opt_type='PE' AND ABS(strike_rank-atm_rank)<={atm_window} THEN volume END)
          / NULLIF(SUM(CASE WHEN opt_type='CE' AND ABS(strike_rank-atm_rank)<={atm_window} THEN volume END),0)
                                                                                       AS vol_pcr_atm,
        SUM(CASE WHEN opt_type='CE' THEN oi END) AS call_oi,
        SUM(CASE WHEN opt_type='PE' THEN oi END) AS put_oi,
        SUM(CASE WHEN opt_type='CE' THEN volume END) AS call_vol,
        SUM(CASE WHEN opt_type='PE' THEN volume END) AS put_vol,
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
    f.add_argument("--atm-window", type=int, default=None,
                   help="only fetch strikes within +/-N ranks of spot (caps calls)")

    lg = sub.add_parser("log", help="daily forward-logger: append latest OI for the front expiry")
    lg.add_argument("--underlying", default="NSE_INDEX|Nifty 50",
                    help='instrument_key, e.g. "NSE_INDEX|Nifty 50"')
    lg.add_argument("--universe", default=None,
                    choices=list(universe.GROUPS) + ["all"],
                    help="log an entire group instead of one underlying")
    lg.add_argument("--expiry", default=None,
                    help="YYYY-MM-DD (default: nearest live expiry)")
    lg.add_argument("--lookback", type=int, default=7,
                    help="days back to (re)pull for gap-fill (default 7)")
    lg.add_argument("--atm-window", type=int, default=None,
                    help="only log strikes within +/-N ranks of spot "
                         "(default None for single underlying; 20 for --universe)")
    lg.add_argument("--max-underlyings", type=int, default=None,
                    help="cap how many underlyings a --universe run touches")

    a = sub.add_parser("pcr", help="aggregate Parquet -> daily PCR / PCR_M table")
    a.add_argument("--underlying", required=True)
    a.add_argument("--atm-window", type=int, default=ATM_WINDOW)

    ls = sub.add_parser("universe", help="list option underlyings in a group")
    ls.add_argument("--group", default="nse_index", choices=list(universe.GROUPS) + ["all"])

    args = p.parse_args()
    if args.cmd == "fetch":
        fetch(args.underlying, args.expiry, args.frm, args.to, args.expired, args.atm_window)
    elif args.cmd == "log":
        if args.universe:
            win = args.atm_window if args.atm_window is not None else 20
            log_universe(args.universe, args.lookback, win, args.max_underlyings)
        else:
            log_daily(args.underlying, args.lookback, args.expiry, args.atm_window)
    elif args.cmd == "pcr":
        df = pcr(args.underlying, args.atm_window)
        print(df.tail(15).to_string(index=False))
    elif args.cmd == "universe":
        for uk, nm in universe.underlyings(args.group):
            print(f"{nm:<16} {uk}")


if __name__ == "__main__":
    main()
