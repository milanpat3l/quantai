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

import json
import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config
config.load_env()
from upstox_oi_pcr import DATA_DIR, _slug, log_daily
import plot_pcr
import analysis
import universe

SERIES_LABELS = {
    "pcr": "OI-PCR (standard)",
    "pcr_m": "Modified OI-PCR (full chain)",
    "pcr_m_atm": "Modified OI-PCR (ATM window)",
    "vol_pcr": "Volume PCR (full chain)",
    "vol_pcr_atm": "Volume PCR (ATM window)",
}

# friendly market label -> (universe group, ATM-window for fetching)
MARKETS = {
    "NSE Indices": ("nse_index", None),
    "NSE Stocks": ("nse_stocks", 12),
    "BSE Indices": ("bse_index", 15),
    "MCX Commodities": ("mcx", 12),
}


def _bridge_secret() -> None:
    """Let the Upstox token come from Streamlit secrets (cloud) as well as .env."""
    if not os.environ.get("UPSTOX_ACCESS_TOKEN"):
        try:
            tok = st.secrets.get("UPSTOX_ACCESS_TOKEN")
            if tok:
                os.environ["UPSTOX_ACCESS_TOKEN"] = str(tok)
        except Exception:
            pass


@st.cache_data(show_spinner=False)
def group_underlyings(group: str) -> list[tuple[str, str]]:
    try:
        return universe.underlyings(group)
    except Exception:
        return []


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
        meta = d / "meta.json"
        if meta.exists():  # written by the logger; survives without the raw chain
            try:
                label = json.loads(meta.read_text()).get("name", label)
            except Exception:
                pass
        else:
            chains = list(d.glob("expiry=*/chain.parquet"))
            if chains:  # recover the human display name from the chain if present
                for col in ("name", "underlying"):
                    try:
                        vals = pd.read_parquet(chains[0], columns=[col])[col].dropna()
                        if len(vals):
                            label = vals.iloc[0]
                            break
                    except Exception:
                        continue
        label = str(label)
        if "|" in label:  # raw instrument_key -> friendly name
            try:
                label = universe.display_name(label)
            except Exception:
                pass
        out[label] = d.name
    return out


@st.cache_data(show_spinner=False)
def load(slug: str) -> pd.DataFrame:
    return plot_pcr.load_slug(slug)


def _arrow(delta: float) -> str:
    if pd.isna(delta) or delta == 0:
        return "—"
    return "▲" if delta > 0 else "▼"


def main() -> None:
    st.set_page_config(page_title="OI-PCR Dashboard", layout="wide",
                       page_icon="📈")
    _bridge_secret()
    st.markdown("## 📈 OI-PCR Historical — Put/Call Ratio vs Price")

    unders = list_underlyings()

    # ---- sidebar: add / refresh data (no typing of instrument keys) -------- #
    with st.sidebar:
        st.header("➕ Add / refresh data")
        token_ok = bool(os.environ.get("UPSTOX_ACCESS_TOKEN"))
        if token_ok:
            st.caption("Upstox token: ✅ connected")
        else:
            st.error("No Upstox token. Add UPSTOX_ACCESS_TOKEN in app Settings → "
                     "Secrets (cloud) or a local .env file.")

        market = st.selectbox("Market", list(MARKETS.keys()), disabled=not token_ok)
        group, fetch_win = MARKETS[market]
        opts = group_underlyings(group) if token_ok else []
        names = [nm for _, nm in opts]
        keymap = {nm: uk for uk, nm in opts}
        pick = st.selectbox("Instrument", names, disabled=not names,
                            help="Pick what to download — no codes needed")
        if st.button("⬇ Get latest data", disabled=not (token_ok and pick),
                     type="primary"):
            uk = keymap[pick]
            with st.spinner(f"Downloading {pick} … (first run can take ~30s)"):
                try:
                    log_daily(uk, atm_window=fetch_win)
                    import upstox_oi_pcr
                    upstox_oi_pcr.pcr(uk)
                    st.cache_data.clear()
                    st.success(f"{pick} updated.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Fetch failed: {e}")
        st.caption("Data is built up over time — run this daily (or set the "
                   "cron in run_daily.sh) so the history grows.")

    if not unders:
        st.info("👈 No data loaded yet. In the left panel, pick a **Market** and "
                "**Instrument**, then click **Get latest data**.")
        st.stop()

    c1, c2, c3 = st.columns([3, 4, 2])
    label = c1.selectbox("Underlying", list(unders.keys()))
    series = c2.radio("Series", list(SERIES_LABELS.keys()),
                      format_func=lambda k: SERIES_LABELS[k], index=1,
                      horizontal=True)
    window = int(c3.number_input("Turning-point window", 1, 10, 1,
                                 help="neighbours each side for peak/trough detection"))

    df = load(unders[label])
    if df.empty:
        st.error("Empty PCR table for this underlying.")
        st.stop()

    # price source: futures (true Quantsapp line) if available, else spot
    has_fut = "fut" in df and df["fut"].notna().any()
    price_col = "fut" if has_fut else "spot"
    if has_fut:
        choice = c3.radio("Price", ["Futures", "Spot"], horizontal=True)
        price_col = "fut" if choice == "Futures" else "spot"
    price_label = "FUT price" if price_col == "fut" else "Price (spot)"

    dmin, dmax = df["date"].min().date(), df["date"].max().date()
    if dmin == dmax:
        rng = (dmin, dmax)
        st.caption(f"Only one day of data ({dmin}). Keep the daily logger running to fill the series.")
    else:
        rng = st.slider("Date range", dmin, dmax, (dmin, dmax))
    mask = (df["date"].dt.date >= rng[0]) & (df["date"].dt.date <= rng[1])
    dff = df.loc[mask].reset_index(drop=True)

    left, right = st.columns([3, 1])

    fig = plot_pcr.build_figure(dff, series, window, f"{label} — {SERIES_LABELS[series]}",
                                price_col=price_col)
    fig.update_layout(height=520, margin=dict(t=60, b=40, l=10, r=10))
    left.plotly_chart(fig, use_container_width=True)

    # latest snapshot metrics (mirrors the Quantsapp "Date / OI PCR / Fut" header)
    last = dff.iloc[-1]
    m1, m2, m3 = left.columns(3)
    m1.metric("Date", str(last["date"].date()))
    m2.metric(SERIES_LABELS[series], f"{last[series]:.3f}" if pd.notna(last[series]) else "—")
    if pd.notna(last.get(price_col)):
        m3.metric(price_label, f"{last[price_col]:,.2f}")

    # right-side table, newest first, with trend arrows
    tbl = dff[["date", series]].copy()
    if price_col in dff:
        tbl["price"] = dff[price_col]
    tbl["trend"] = tbl[series].diff().map(_arrow)
    tbl = tbl.sort_values("date", ascending=False)
    tbl["date"] = tbl["date"].dt.strftime("%d-%b-%y")
    pcr_col = "VOL-PCR" if series.startswith("vol") else "OI-PCR"
    tbl = tbl.rename(columns={series: pcr_col, "price": price_label, "trend": "Δ"})

    def _color(v):
        return "color: #2e9e5b" if v == "▲" else ("color: #d23b3b" if v == "▼" else "color: gray")

    right.markdown("**History**")
    sty = tbl.style.map(_color, subset=["Δ"]).format({pcr_col: "{:.3f}", price_label: "{:,.2f}"})
    right.dataframe(sty, hide_index=True, use_container_width=True, height=520)

    st.caption("Price line is the near-month future (toggle to spot). Turning-point "
               "arrows are heuristic (local PCR extrema), not trade signals.")

    # ---- lead-lag analysis: does PCR lead price? --------------------------- #
    with st.expander("🔬 Lead-lag analysis — does this PCR lead price?", expanded=False):
        max_lag = st.slider("Max lag (days)", 1, 10, min(5, max(1, len(dff) // 2)))
        ll = analysis.lead_lag(dff, series, max_lag=max_lag)
        if ll.empty or ll["corr"].notna().sum() == 0:
            st.info("Not enough overlapping PCR/price data yet to correlate.")
        else:
            bar = go.Figure(go.Bar(
                x=ll["lag"], y=ll["corr"],
                marker_color=["#d23b3b" if c < 0 else "#2e9e5b" for c in ll["corr"].fillna(0)]))
            bar.update_layout(
                height=300, template="plotly_white",
                title="corr( PCR[t] , price_return[t+lag] )  —  positive lag = PCR leads",
                xaxis_title="lag (days)", yaxis_title="correlation",
                margin=dict(t=50, b=40, l=10, r=10))
            st.plotly_chart(bar, use_container_width=True)
            st.write(analysis.interpret(ll))
        st.caption("Gate before any ML: a stable, signed correlation at positive lags is the "
                   "edge. With only a few days logged this is noise — it firms up as history grows.")

    # ---- ML signal: walk-forward model -> next-day direction --------------- #
    with st.expander("🤖 ML signal (experimental) — Logistic / Decision Tree / Random Forest", expanded=False):
        import model as mdl
        mc1, mc2 = st.columns([2, 2])
        mname = mc1.selectbox("Model", list(mdl.MODELS),
                              format_func=lambda m: {"logistic": "Logistic regression",
                              "tree": "Decision Tree", "forest": "Random Forest"}[m])
        # adapt min-train to however much data exists so the panel is illustrative now
        n_have = int(dff[series].notna().sum())
        min_train = mc2.slider("Min training days", 3, max(5, n_have - 2),
                               min(40, max(3, n_have // 2)))
        X, y, _ = mdl.build_features(dff, price_col)
        res = mdl.walk_forward(X, y, min_train, mname)
        if res["status"] == "insufficient":
            st.info(f"Insufficient data: {res['n']} labelled days, need ~{res['need']}. "
                    "The model framework is wired and ready — it just needs history.")
        else:
            a, b, c = st.columns(3)
            a.metric("Walk-forward accuracy", f"{res['accuracy']:.0%}")
            b.metric("Coin-flip / base rate", f"{res['baseline']:.0%}")
            c.metric("Edge over base", f"{res['edge']:+.0%}", help=f"tested on {res['tested']} days")
            if mdl.credible(res):
                call = mdl.latest_call(X, y, mname, min_train)
                if call["status"] == "ok":
                    pr = f" · confidence {call['prob']:.0%}" if call.get("prob") else ""
                    st.success(f"Next-session call: **{call['direction']}**{pr}")
            else:
                st.warning("⚠ Not a tradeable signal yet — too few out-of-sample days "
                           "and/or no real edge over a coin flip. Tree models in particular "
                           "**memorise** tiny datasets, so any high score here now is an "
                           "illusion. This becomes meaningful only after months of logged "
                           "data and a confirmed lead-lag edge above.")
        st.caption("Features: PCR levels + day-over-day changes at day t → next-day price "
                   "direction. Strict expanding-window walk-forward (no shuffling, no lookahead).")


if __name__ == "__main__":
    main()
