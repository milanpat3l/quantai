"""
dashboard.py
============
Interactive Quantsapp-style dashboard for the OI-PCR model.

Left:  dual-axis chart — (Modified) OI-PCR vs underlying price, turning points.
Right: live data table (Date / OI-PCR / FUT price) with up/down trend arrows.
Top:   underlying picker, PCR-series tabs, date range, turning-point window.

Run
---
    streamlit run dashboard.py
    # then open the URL it prints (default http://localhost:8501)

Reads the Parquet tables produced by ``upstox_oi_pcr.py`` (run ``log``/``fetch``
first so there is data to show).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import config
config.load_env()
from upstox_oi_pcr import DATA_DIR, _slug
import plot_pcr

SERIES_LABELS = {
    "pcr": "OI-PCR (standard)",
    "pcr_m": "Modified OI-PCR (full chain)",
    "pcr_m_atm": "Modified OI-PCR (ATM window)",
    "vol_pcr": "Volume PCR (full chain)",
    "vol_pcr_atm": "Volume PCR (ATM window)",
}


@st.cache_data(show_spinner=False)
def list_underlyings() -> dict[str, str]:
    """Map a human label -> store slug for every underlying that has a PCR table."""
    out: dict[str, str] = {}
    if not DATA_DIR.exists():
        return out
    for d in sorted(p for p in DATA_DIR.iterdir() if p.is_dir()):
        if not (d / "pcr_daily.parquet").exists():
            continue
        label = d.name
        chains = list(d.glob("expiry=*/chain.parquet"))
        if chains:  # recover the real "NSE_INDEX|Nifty 50" name if we can
            try:
                label = pd.read_parquet(chains[0], columns=["underlying"])["underlying"].iloc[0]
            except Exception:
                pass
        out[label] = d.name
    return out


@st.cache_data(show_spinner=False)
def load(label: str) -> pd.DataFrame:
    return plot_pcr.load(label)


def _arrow(delta: float) -> str:
    if pd.isna(delta) or delta == 0:
        return "—"
    return "▲" if delta > 0 else "▼"


def main() -> None:
    st.set_page_config(page_title="OI-PCR Dashboard", layout="wide",
                       page_icon="📈")
    st.markdown("## 📈 OI-PCR Historical — Put/Call Ratio vs Price")

    unders = list_underlyings()
    if not unders:
        st.warning("No data yet. Populate the store first:\n\n"
                   "```\npython3 upstox_oi_pcr.py log\n```")
        st.stop()

    c1, c2, c3 = st.columns([3, 4, 2])
    label = c1.selectbox("Underlying", list(unders.keys()))
    series = c2.radio("Series", list(SERIES_LABELS.keys()),
                      format_func=lambda k: SERIES_LABELS[k], index=1,
                      horizontal=True)
    window = int(c3.number_input("Turning-point window", 1, 10, 1,
                                 help="neighbours each side for peak/trough detection"))

    df = load(label)
    if df.empty:
        st.error("Empty PCR table for this underlying.")
        st.stop()

    dmin, dmax = df["date"].min().date(), df["date"].max().date()
    if dmin == dmax:
        rng = (dmin, dmax)
        st.caption(f"Only one day of data ({dmin}). Keep the daily logger running to fill the series.")
    else:
        rng = st.slider("Date range", dmin, dmax, (dmin, dmax))
    mask = (df["date"].dt.date >= rng[0]) & (df["date"].dt.date <= rng[1])
    dff = df.loc[mask].reset_index(drop=True)

    left, right = st.columns([3, 1])

    fig = plot_pcr.build_figure(dff, series, window, f"{label} — {SERIES_LABELS[series]}")
    fig.update_layout(height=520, margin=dict(t=60, b=40, l=10, r=10))
    left.plotly_chart(fig, use_container_width=True)

    # latest snapshot metrics (mirrors the Quantsapp "Date / OI PCR / Fut" header)
    last = dff.iloc[-1]
    m1, m2, m3 = left.columns(3)
    m1.metric("Date", str(last["date"].date()))
    m2.metric(SERIES_LABELS[series], f"{last[series]:.3f}" if pd.notna(last[series]) else "—")
    if pd.notna(last.get("spot")):
        m3.metric("Price (spot)", f"{last['spot']:,.2f}")

    # right-side table, newest first, with trend arrows
    tbl = dff[["date", series]].copy()
    if "spot" in dff:
        tbl["price"] = dff["spot"]
    tbl["trend"] = tbl[series].diff().map(_arrow)
    tbl = tbl.sort_values("date", ascending=False)
    tbl["date"] = tbl["date"].dt.strftime("%d-%b-%y")
    tbl = tbl.rename(columns={series: "OI-PCR", "price": "FUT price", "trend": "Δ"})

    def _color(v):
        return "color: #2e9e5b" if v == "▲" else ("color: #d23b3b" if v == "▼" else "color: gray")

    right.markdown("**History**")
    sty = tbl.style.map(_color, subset=["Δ"]).format({"OI-PCR": "{:.3f}", "FUT price": "{:,.2f}"})
    right.dataframe(sty, hide_index=True, use_container_width=True, height=520)

    st.caption("Spot is used as the price proxy until a futures line is wired in. "
               "Turning-point arrows are heuristic (local PCR extrema), not trade signals.")


if __name__ == "__main__":
    main()
