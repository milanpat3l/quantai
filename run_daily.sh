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

echo "=== $(date -Is) daily log run ==="

# Indices: full chain (small, high-value). Stocks/commodities: ATM-windowed to
# keep the daily call volume sane. Comment/uncomment groups as you like.
python3 upstox_oi_pcr.py log --universe nse_index               --lookback 7
python3 upstox_oi_pcr.py log --universe bse_index --atm-window 15 --lookback 7
python3 upstox_oi_pcr.py log --universe mcx        --atm-window 12 --lookback 7
python3 upstox_oi_pcr.py log --universe nse_stocks --atm-window 12 --lookback 7

echo "=== done ==="
