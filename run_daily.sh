#!/usr/bin/env bash
# Daily forward-logger for the OI-PCR store. Run AFTER market close (NSE closes
# 15:30 IST); the daily candle is only final post-close. Reads the Analytics
# token from .env in this directory.
#
# Cron (server clock in IST), weekdays 18:00 IST:
#   0 18 * * 1-5  /path/to/quantai/run_daily.sh >> /path/to/quantai/log.txt 2>&1
# If your server is on UTC, 18:00 IST == 12:30 UTC:
#   30 12 * * 1-5 /path/to/quantai/run_daily.sh >> /path/to/quantai/log.txt 2>&1
set -euo pipefail

cd "$(dirname "$0")"
# Activate a venv if present (optional)
[ -f .venv/bin/activate ] && source .venv/bin/activate

UNDERLYINGS=("NSE_INDEX|Nifty 50")   # add more, e.g. "NSE_INDEX|Nifty Bank"

echo "=== $(date -Is) daily log run ==="
for u in "${UNDERLYINGS[@]}"; do
    python3 upstox_oi_pcr.py log --underlying "$u" --lookback 7
    python3 upstox_oi_pcr.py pcr --underlying "$u" >/dev/null
done
echo "=== done ==="
