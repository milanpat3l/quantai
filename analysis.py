"""
analysis.py
===========
Step-2 gate: does the (OI/Volume) PCR actually *lead* price?

Computes the lagged cross-correlation between a PCR series and forward price
returns. If PCR carries predictive information, correlation should be non-trivial
at positive lags (PCR today vs returns over the next k days). The Quantsapp
article frames PCR as contrarian, so a negative correlation at positive lags is
the interesting case.

    corr(k) = corr( pcr[t] , price_return[t+k] )      for k in [-max_lag, max_lag]

Usage
-----
    python analysis.py --underlying "NSE_INDEX|Nifty 50" --series pcr_m --max-lag 5
"""

from __future__ import annotations

import argparse

import pandas as pd

import config
config.load_env()
import plot_pcr


def lead_lag(df: pd.DataFrame, series: str = "pcr_m", price_col: str = "spot",
             max_lag: int = 5) -> pd.DataFrame:
    """Lagged correlation of ``series`` vs forward returns of ``price_col``.

    Positive lag k -> PCR today vs the price return k days *ahead* (PCR leads).
    Returns a tidy frame [lag, corr, n] sorted by lag.
    """
    if price_col not in df or df[price_col].notna().sum() < 3:
        return pd.DataFrame(columns=["lag", "corr", "n"])
    s = pd.to_numeric(df[series], errors="coerce")
    ret = pd.to_numeric(df[price_col], errors="coerce").pct_change()
    rows = []
    for k in range(-max_lag, max_lag + 1):
        paired = pd.concat([s, ret.shift(-k)], axis=1).dropna()
        n = len(paired)
        c = paired.iloc[:, 0].corr(paired.iloc[:, 1]) if n >= 3 else float("nan")
        rows.append({"lag": k, "corr": c, "n": n})
    return pd.DataFrame(rows)


def interpret(ll: pd.DataFrame) -> str:
    """One-line read of the lead-lag profile (guarded for tiny samples)."""
    lead = ll[ll["lag"] > 0].dropna(subset=["corr"])
    if lead.empty:
        return "Not enough data to assess lead-lag yet — keep logging."
    best = lead.iloc[lead["corr"].abs().argmax()]
    n = int(best["n"])
    if n < 20:
        return (f"Strongest forward link at lag +{int(best['lag'])} "
                f"(corr {best['corr']:+.2f}) — but only n={n}; treat as noise "
                f"until ~40-60 days are logged.")
    direction = "contrarian (negative)" if best["corr"] < 0 else "trend (positive)"
    return (f"PCR leads price most at lag +{int(best['lag'])}d, corr "
            f"{best['corr']:+.2f} ({direction}, n={n}).")


def main() -> None:
    p = argparse.ArgumentParser(description="PCR lead-lag check vs price")
    p.add_argument("--underlying", default="NSE_INDEX|Nifty 50")
    p.add_argument("--series", default="pcr_m",
                   choices=["pcr", "pcr_m", "pcr_m_atm", "vol_pcr", "vol_pcr_atm"])
    p.add_argument("--max-lag", type=int, default=5)
    args = p.parse_args()

    df = plot_pcr.load(args.underlying)
    ll = lead_lag(df, args.series, max_lag=args.max_lag)
    if ll.empty:
        print("No price/PCR overlap to correlate.")
        return
    print(ll.to_string(index=False))
    print("\n" + interpret(ll))


if __name__ == "__main__":
    main()
