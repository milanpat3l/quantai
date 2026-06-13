# Put the dashboard online (no coding) — 10 minutes

This hosts your dashboard on **Streamlit Community Cloud** (free) and gives you a
web link you can open from any browser or phone. You only do this once.

## Before you start
- A free **GitHub** account (you already have the code there).
- Your **Upstox Analytics Token** (from `account.upstox.com/developer/apps` →
  Analytics tab → copy the token).

## Steps

1. **Go to** [share.streamlit.io](https://share.streamlit.io) and click
   **“Sign in with GitHub.”** Approve the access it asks for.

2. Click **“Create app”** → **“Deploy a public app from GitHub.”**

3. Fill the boxes:
   - **Repository:** `milanpat3l/quantai`
   - **Branch:** `claude/magical-goodall-yasu9r` (or `main` once it’s merged)
   - **Main file path:** `dashboard.py`

4. Click **“Advanced settings”** → in the **Secrets** box paste this one line
   (replace with your real token), then **Save**:
   ```
   UPSTOX_ACCESS_TOKEN = "paste-your-analytics-token-here"
   ```

5. Click **“Deploy.”** Wait ~2–3 minutes for it to build. You’ll get a link like
   `https://your-app-name.streamlit.app` — **that’s your dashboard.** Bookmark it.

## Using it

- The first time, it’s empty. In the **left panel**: pick a **Market**
  (NSE Indices / Stocks / BSE / MCX), pick an **Instrument** (e.g. Nifty 50,
  Reliance, Gold), and click **“Get latest data.”** Wait a few seconds.
- Use the **Series** buttons to switch between OI‑PCR, Modified OI‑PCR, and
  Volume PCR. Toggle **Futures / Spot** for the price line.
- Open **“Lead‑lag analysis”** at the bottom to see whether PCR leads price.

## Two things to know

- **History grows over time.** Each “Get latest data” adds the most recent days.
  The lead‑lag/signal sections only become meaningful after a few weeks of data.
  For automatic daily updates, the `run_daily.sh` cron (see `README.md`) running
  on an always‑on computer is the long‑term setup.
- **Keep your token private.** It lives only in the Secrets box (step 4), never
  in the code. If it ever leaks, regenerate it on the Upstox Analytics tab.

## Prefer to run it on your own computer instead?
```
pip install -r requirements.txt
cp .env.example .env        # paste your token into UPSTOX_ACCESS_TOKEN
./run_dashboard.sh          # opens http://localhost:8501
```
