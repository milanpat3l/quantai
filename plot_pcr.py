"""
plot_pcr.py
===========
Quantsapp-style visualization for the OI-PCR store: a dual-axis chart with the
(Modified) OI-PCR on the left axis and the underlying price on the right axis,
plus turning-point markers (green up = PCR trough, red down = PCR peak).

Reads the tables produced by ``upstox_oi_pcr.py``:
    data/<slug>/pcr_daily.parquet   (pcr, pcr_m, pcr_m_atm, ...)
    data/<slug>/price.parquet       (date, spot)

Usage
-----
    python plot_pcr.py --underlying "NSE_INDEX|Nifty 50" --series pcr_m
    # writes data/<slug>/pcr_chart.html (interactive) and .png (static)
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import config
config.load_env()
from upstox_oi_pcr import DATA_DIR, _slug   # reuse store layout

PCR_GREEN = "#2e9e5b"
VOL_BLUE = "#3b82c4"      # Quantsapp uses blue for the Volume-PCR line
PRICE_ORANGE = "#e8912a"


def _series_color(series: str) -> str:
    return VOL_BLUE if series.startswith("vol") else PCR_GREEN


def _local_extrema(y: pd.Series, window: int) -> tuple[list[int], list[int]]:
    """Return (trough_idx, peak_idx): positions that are the strict min / max
    within +/- window neighbours. Endpoints are ignored."""
    troughs, peaks = [], []
    v = y.to_numpy()
    n = len(v)
    for i in range(1, n - 1):
        lo, hi = max(0, i - window), min(n, i + window + 1)
        seg = v[lo:hi]
        if v[i] == seg.min() and v[i] < seg.max():
            troughs.append(i)
        elif v[i] == seg.max() and v[i] > seg.min():
            peaks.append(i)
    return troughs, peaks


def load(underlying: str) -> pd.DataFrame:
    base = DATA_DIR / _slug(underlying)
    pcr = pd.read_parquet(base / "pcr_daily.parquet")
    price_path = base / "price.parquet"
    if price_path.exists():
        price = pd.read_parquet(price_path)
        df = pcr.merge(price, on="date", how="left")
    else:
        df = pcr.assign(spot=pd.NA)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def build_figure(df: pd.DataFrame, series: str, window: int, title: str,
                 price_col: str = "fut") -> go.Figure:
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    color = _series_color(series)

    # left axis: PCR series (green for OI-PCR, blue for Volume-PCR)
    fig.add_trace(go.Scatter(
        x=df["date"], y=df[series], name=series.upper().replace("_", "-"),
        mode="lines", line=dict(color=color, width=2)), secondary_y=False)

    # right axis: price line — prefer futures, fall back to spot
    if price_col not in df or not df[price_col].notna().any():
        price_col = "spot"
    if price_col in df and df[price_col].notna().any():
        pname = "FUT price" if price_col == "fut" else "Price (spot)"
        fig.add_trace(go.Scatter(
            x=df["date"], y=df[price_col], name=pname,
            mode="lines", line=dict(color=PRICE_ORANGE, width=2)), secondary_y=True)

    # turning-point markers on the PCR series
    troughs, peaks = _local_extrema(df[series].ffill(), window)
    if troughs:
        fig.add_trace(go.Scatter(
            x=df["date"].iloc[troughs], y=df[series].iloc[troughs], mode="markers",
            name="PCR trough", marker=dict(symbol="triangle-up", size=12,
            color=PCR_GREEN, line=dict(width=1, color="#14532d"))), secondary_y=False)
    if peaks:
        fig.add_trace(go.Scatter(
            x=df["date"].iloc[peaks], y=df[series].iloc[peaks], mode="markers",
            name="PCR peak", marker=dict(symbol="triangle-down", size=12,
            color="#d23b3b", line=dict(width=1, color="#7f1d1d"))), secondary_y=False)

    fig.update_layout(
        title=title, template="plotly_white", hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0))
    fig.update_xaxes(title_text="Date")
    fig.update_yaxes(title_text=series.upper().replace("_", "-"), secondary_y=False)
    fig.update_yaxes(title_text="Price", secondary_y=True)
    return fig


def main() -> None:
    p = argparse.ArgumentParser(description="Dual-axis OI-PCR vs price chart")
    p.add_argument("--underlying", default="NSE_INDEX|Nifty 50")
    p.add_argument("--series", default="pcr_m",
                   choices=["pcr", "pcr_m", "pcr_m_atm", "vol_pcr", "vol_pcr_atm"],
                   help="which PCR line")
    p.add_argument("--window", type=int, default=1,
                   help="neighbours each side for turning-point detection")
    p.add_argument("--price", default="fut", choices=["fut", "spot"],
                   help="price line source (default futures, falls back to spot)")
    p.add_argument("--out", default=None, help="output path stem (default in store)")
    args = p.parse_args()

    df = load(args.underlying)
    title = f"{args.underlying} — {args.series.upper().replace('_','-')} vs Price"
    fig = build_figure(df, args.series, args.window, title, price_col=args.price)

    stem = Path(args.out) if args.out else DATA_DIR / _slug(args.underlying) / "pcr_chart"
    stem.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(f"{stem}.html")
    print(f"wrote {stem}.html")
    try:
        fig.write_image(f"{stem}.png", width=1100, height=520, scale=2)
        print(f"wrote {stem}.png")
    except Exception as e:  # kaleido missing/broken — HTML still works
        print(f"(PNG export skipped: {e})")


if __name__ == "__main__":
    main()
