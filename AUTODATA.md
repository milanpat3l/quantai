# Automatic daily data storage (free)

A **GitHub Action** logs the OI/Volume‑PCR data every weekday after market close
and saves it back into this repo. Your Streamlit dashboard reads it from there,
so the history is stored for free and is always available — no computer of yours
needs to stay on.

- **When:** weekdays at 19:00 UTC (00:30 IST), after NSE/BSE and MCX close.
- **What it stores:** the small daily tables (`pcr_daily.parquet`) and the price
  line (`price.parquet`) for indices, F&O stocks, BSE indices, and MCX commodities.
  (The bulky raw option chains are not stored — they’re re‑fetched as needed, so
  the repo stays small.)
- **Where:** committed into the repo, branch `claude/magical-goodall-yasu9r`.

## One‑time setup (2 minutes)

The Action needs your Upstox token, stored as a **GitHub secret** (separate from
the Streamlit one).

1. Open **https://github.com/milanpat3l/quantai/settings/secrets/actions**
2. Click **“New repository secret.”**
3. **Name:** `UPSTOX_ACCESS_TOKEN`
   **Secret:** paste your Analytics token (just the token, no quotes here).
4. **Add secret.**

That’s it. The first automatic run happens the next weekday at 19:00 UTC.

## Run it now (optional, to test)

1. Go to the repo’s **Actions** tab → **“Daily OI‑PCR log.”**
2. Click **“Run workflow”** → **Run**. It takes ~10–15 minutes.
3. When it finishes, your Streamlit app will refresh with the new data shortly
   after (Streamlit redeploys whenever the repo updates).

## Notes

- Use the **same token** as Streamlit. If you regenerate the token, update it in
  **both** places (this Actions secret **and** the Streamlit Secrets box).
- The store grows slowly. If it ever gets large, we can move it to free object
  storage (Cloudflare R2 10 GB / Google Drive) — the code is ready for that.
- Scheduled Actions are paused if the repo has no activity for 60 days; the daily
  commits keep it active automatically.
