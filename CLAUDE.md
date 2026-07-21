# Modified OI-PCR Analytics Model — Project Brief

## Goal
Build a Quantsapp-style **Modified OI-PCR** model for Indian F&O (NSE). Produce a
daily (later intraday) series of standard OI-PCR and premium-weighted "Modified"
OI-PCR per underlying, plotted against the futures price, with turning-point
markers — and eventually an ML signal layer on top.

## Data source decision: Upstox (not Fyers)
Upstox bundles **Open Interest as the 7th field inside historical candles**, so OI
history comes free with OHLCV. Fyers history candles are OHLCV only (no OI), which
would force live-logging from scratch. So we use Upstox.

Token: read from `UPSTOX_ACCESS_TOKEN` env var. **Never hard-code or paste the token
anywhere.** It can read portfolio/funds and place orders — treat like a password,
keep in a local `.env`, rotate if exposed.

## Confirmed API endpoints
Candle array layout everywhere: `[timestamp, open, high, low, close, volume, oi]`.

**Live (free tier):**
- Historical candle V3:
  `GET /v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}`
  units = `minutes`(1–300) / `hours`(1–5) / `days` / `weeks` / `months`.
  Intraday available from **Jan 2022**; daily/weekly/monthly from **Jan 2000**.
- Option chain snapshot:
  `GET /v2/option/chain?instrument_key=..&expiry_date=..`
  → per strike CE/PE `market_data{ltp,volume,oi,prev_oi}` + `option_greeks{delta,gamma,theta,vega,iv,pop}`.
  Note: **no rho, no higher-order greeks** (vanna/charm/vomma) — compute from IV+spot if needed.
- Option contracts: `GET /v2/option/contract?instrument_key=..`

**Expired contracts (requires Upstox PLUS subscription — error UDAPI1149 otherwise):**
- `GET /v2/expired-instruments/option/contract?instrument_key=..&expiry_date=YYYY-MM-DD`
  → legs with `instrument_key` (embeds expiry), `instrument_type` (CE/PE), `strike_price`.
- `GET /v2/expired-instruments/historical-candle/{expired_instrument_key}/{interval}/{to_date}/{from_date}`
  intervals = `1minute|3minute|5minute|15minute|30minute|day`.
- `GET /v2/expired-instruments/expiries?instrument_key=..` — **verify exact params against docs.**

## Storage decision: local Parquet + DuckDB
Free and unlimited on disk. Cloud "free" DBs (Supabase/Neon/Mongo ≈ 500 MB) fill in
days for chain data — avoid. Partition Parquet by `underlying/expiry`. Query/aggregate
with DuckDB. Optional nightly backup to Cloudflare R2 (10 GB free) or Google Drive.

## Formulas
- `PCR        = Σ put_oi / Σ call_oi`
- `PCR_M      = Σ(put_oi × put_ltp) / Σ(call_oi × call_ltp)`  (premium-weighted, full chain)
- `PCR_M_ATM  = same, restricted to ATM ± N strikes`  (reconstruction of Quantsapp "near current price")
- `VOL_PCR    = Σ put_volume / Σ call_volume`  (Quantsapp VOL-PCR-H)
- `VOL_PCR_ATM= same, restricted to ATM ± N strikes`
- ATM = listed strike nearest to the underlying spot (from `price.parquet`).
  Falls back to the put-call-parity proxy (min |CE_ltp − PE_ltp|) only when no
  spot is available. The parity proxy alone proved unreliable on real daily
  closes — illiquid far strikes with ~equal CE/PE closes were wrongly picked.
- Premium weighting is **our reconstruction**; Quantsapp's exact weighting is proprietary. Tune the ATM window.

## Already built — `upstox_oi_pcr.py`
Two CLI commands:
- `fetch --underlying "NSE_INDEX|Nifty 50" --expiry YYYY-MM-DD --from YYYY-MM-DD [--expired]`
  → pulls each CE/PE leg's daily candles + the underlying spot → partitioned Parquet.
- `log [--underlying KEY] [--universe GROUP] [--expiry ..] [--lookback 7] [--atm-window N] [--max-underlyings M]`
  → forward-logger: auto-picks the nearest live expiry, pulls the last N days,
  and **idempotently merges** into the store (re-runs are safe). Master-driven,
  so it works for **NSE stocks & indices, BSE indices, and MCX commodities**.
  `--universe {nse_index,nse_stocks,bse_index,mcx,all}` logs a whole group;
  `--atm-window N` caps each chain to ATM±N strikes (essential for the broad
  universe — full universe is tens of thousands of calls otherwise).
- `universe [--group G]` → list option underlyings (see `universe.py`).
- Scale note: 5 NSE indices + 211 NSE stocks + 4 BSE indices + 11 MCX commodities
  (~231). Run groups on a cron with sensible `--atm-window`; the dashboard
  auto-lists every underlying that has data.
- `pcr --underlying "NSE_INDEX|Nifty 50" [--atm-window N]`
  → DuckDB aggregation → daily table: `pcr`, `pcr_m`, `pcr_m_atm`, `vol_pcr`,
  `vol_pcr_atm`, `call_oi`, `put_oi`, `call_vol`, `put_vol`, `atm_strike`.
- `plot_pcr.py --series {pcr,pcr_m,pcr_m_atm,vol_pcr,vol_pcr_atm} [--price fut|spot]` → dual-axis chart.
- `analysis.py` → lead-lag check corr(PCR[t], price_return[t+lag]).
- `model.py` → walk-forward (expanding-window) PCR→next-day-direction; models:
  logistic / decision-tree / random-forest. Reports accuracy vs majority baseline
  and refuses to emit a signal until enough out-of-sample days + real edge.
- `dashboard.py` (Streamlit) → interactive chart + history table + Futures/Spot toggle
  + lead-lag panel + experimental ML-signal panel; `streamlit run dashboard.py`.

Price line: near-month FUTURES close (resolved from Upstox's NSE instrument
master, cached) stored alongside spot in `price.parquet`; spot remains the ATM
anchor. Logger handles multiple underlyings (Nifty 50 + Bank Nifty in `run_daily.sh`).

Auth: uses the read-only **Upstox Analytics Token** (~1yr, no daily login, no
Static IP for market data) in `UPSTOX_ACCESS_TOKEN` (local `.env`, git-ignored).

Status: live `fetch` + `pcr` **validated end-to-end** against the real API on
Nifty 50 (expiry 2026-06-16). Verified: `/v2/option/contract` shape, V3 daily
candle layout `[ts,o,h,l,c,v,oi]` with OI populated. Fixed: ATM now spot-anchored
(parity proxy was picking illiquid far strikes). Account/portfolio endpoints
return UDAPI1221 without Static IP — expected; we don't use them.

## Known caveats
- **Expiry-roll discontinuity:** OI collapses to ~0 at expiry; naive concatenation of
  consecutive expiries creates fake spikes. Use near-month selection or clean rolls.
- Expired-instruments path is **blocked on the read-only Analytics token**
  (UDAPI100067) and also needs Plus. Decision: stay on the free Analytics token
  and **build history forward** via the daily `log` command (`run_daily.sh`).
- Daily data = small samples → ML overfits easily (see roadmap).

## Roadmap (do in this order)
1. ✅ **DONE — Validate live `fetch` + `pcr` end-to-end** (Nifty 50, 2026-06-16).
   Real-API shapes confirmed; ATM fixed to spot-anchored. Still TODO: date-window
   chunking for minute data (daily ranges are fine as one request).
2. **Lead-lag check (gate before any ML):** ✅ scaffolding built — `analysis.py`
   computes corr(PCR[t], price_return[t+lag]) and is surfaced in the dashboard.
   Needs ~weeks of logged data before the profile is meaningful (currently noise).
3. **Live WebSocket logger** → same Parquet store (forward-fill path if skipping Plus).
4. ✅ **DONE (v1) — Visualization:** `plot_pcr.py` builds a Plotly dual-axis chart
   (PCR series left, price right) + turning-point arrows, mirroring the Quantsapp
   screen → `data/<slug>/pcr_chart.{html,png}`. TODO: side data table; futures
   price line (currently spot proxy); intraday once logged.
5. **ML layer — only after step 2 confirms an edge.** ✅ scaffolding built —
   `model.py` does a strict walk-forward (expanding-window, no shuffle) logistic
   baseline on PCR features → next-day direction, comparing to the majority
   baseline and refusing on thin data. Climb later: LightGBM, then sequence
   models only if intraday + years of data. Watch small samples, lookahead bias,
   non-stationarity/regime change. Not usable until the logger has months of data
   and step-2 shows a real edge.

## Working style
Concise, modular, config-driven, reusable. Minimal rework. Prefer one clean module
over scattered scripts.

---
**First task for this session:** wire up a `.env` with `UPSTOX_ACCESS_TOKEN`, then run
`fetch` + `pcr` against the current Nifty expiry and fix whatever the real API responses
break. Then build the step-2 lead-lag check.
