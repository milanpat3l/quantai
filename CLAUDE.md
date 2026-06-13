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
- ATM = listed strike nearest to the underlying spot (from `price.parquet`).
  Falls back to the put-call-parity proxy (min |CE_ltp − PE_ltp|) only when no
  spot is available. The parity proxy alone proved unreliable on real daily
  closes — illiquid far strikes with ~equal CE/PE closes were wrongly picked.
- Premium weighting is **our reconstruction**; Quantsapp's exact weighting is proprietary. Tune the ATM window.

## Already built — `upstox_oi_pcr.py`
Two CLI commands:
- `fetch --underlying "NSE_INDEX|Nifty 50" --expiry YYYY-MM-DD --from YYYY-MM-DD [--expired]`
  → pulls each CE/PE leg's daily candles + the underlying spot → partitioned Parquet.
- `log [--underlying ..] [--expiry ..] [--lookback 7]`
  → forward-logger: auto-picks the nearest live expiry, pulls the last N days,
  and **idempotently merges** into the store (re-runs are safe). Run daily after
  market close via `run_daily.sh` (cron) so OI history accumulates on the free
  Analytics token — the only depth path without Plus/expired backfill.
- `pcr --underlying "NSE_INDEX|Nifty 50" [--atm-window N]`
  → DuckDB aggregation → daily table: `pcr`, `pcr_m`, `pcr_m_atm`, `call_oi`, `put_oi`, `atm_strike`.

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
2. **Lead-lag check (gate before any ML):** pull the futures candle, merge with `pcr_m`,
   print the lagged-correlation profile. Confirm PCR_M actually *leads* price (the article
   claims a negative relationship). If no edge here, stop — no model will fix it.
3. **Live WebSocket logger** → same Parquet store (forward-fill path if skipping Plus).
4. ✅ **DONE (v1) — Visualization:** `plot_pcr.py` builds a Plotly dual-axis chart
   (PCR series left, price right) + turning-point arrows, mirroring the Quantsapp
   screen → `data/<slug>/pcr_chart.{html,png}`. TODO: side data table; futures
   price line (currently spot proxy); intraday once logged.
5. **ML layer — only after step 2 confirms an edge.** Climb: statistics baseline
   (logistic/linear on OI-derived features) → LightGBM with **strict walk-forward CV**
   (never random shuffle — leakage kills it) → sequence models only if intraday + years of
   data. Watch small samples, lookahead bias, non-stationarity/regime change.

## Working style
Concise, modular, config-driven, reusable. Minimal rework. Prefer one clean module
over scattered scripts.

---
**First task for this session:** wire up a `.env` with `UPSTOX_ACCESS_TOKEN`, then run
`fetch` + `pcr` against the current Nifty expiry and fix whatever the real API responses
break. Then build the step-2 lead-lag check.
