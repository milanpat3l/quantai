# Modified OI-PCR Analytics

A Quantsapp-style **Put/Call Ratio** analytics stack for Indian F&O (NSE), built
on the read-only **Upstox Analytics Token**. Computes standard OI-PCR,
premium-weighted "Modified" OI-PCR, and **Volume PCR** per underlying, plots them
against price with turning-point markers, and ships an interactive dashboard.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # paste your Upstox Analytics Token into UPSTOX_ACCESS_TOKEN
# (PNG export only) install Chrome for kaleido:
plotly_get_chrome
```

The **Analytics Token** is read-only (cannot trade), valid ~1 year, needs no
daily login or Static IP for market data. Generate it at
`account.upstox.com/developer/apps` → Analytics tab. Keep it in `.env` (git-ignored).

## Commands

```bash
# one-off backfill of a live expiry (daily candles incl. OI + the spot line)
python upstox_oi_pcr.py fetch --underlying "NSE_INDEX|Nifty 50" --expiry 2026-06-16 --from 2026-06-01

# daily forward-logger (auto front expiry, idempotent merge) — cron via run_daily.sh
python upstox_oi_pcr.py log

# aggregate -> daily PCR table (pcr, pcr_m, pcr_m_atm, vol_pcr, vol_pcr_atm, ...)
python upstox_oi_pcr.py pcr --underlying "NSE_INDEX|Nifty 50"

# static dual-axis chart -> data/<slug>/pcr_chart.{html,png}
python plot_pcr.py --series vol_pcr

# lead-lag gate: does PCR lead price?
python analysis.py --series pcr_m --max-lag 5

# interactive webapp
./run_dashboard.sh            # or: streamlit run dashboard.py
```

## Data flow

```
Upstox Analytics Token
   └─ upstox_oi_pcr.py  fetch/log  ── option-leg daily candles + spot
        └─ data/<slug>/expiry=*/chain.parquet   (+ price.parquet)
             └─ pcr  ──  data/<slug>/pcr_daily.parquet
                  ├─ plot_pcr.py   (Plotly chart)
                  ├─ analysis.py   (lead-lag check)
                  └─ dashboard.py  (Streamlit webapp)
```

Storage is local Parquet + DuckDB (free, unlimited). Partitioned by
`underlying/expiry`. See `CLAUDE.md` for formulas, caveats, and roadmap.

## Status

Live `fetch`/`log`/`pcr` validated against the real API (Nifty 50). Dashboard
renders all five PCR series + price + turning points + lead-lag panel. Building
history forward on the free token; the lead-lag edge needs ~weeks of data before
it is meaningful (gate before any ML).
