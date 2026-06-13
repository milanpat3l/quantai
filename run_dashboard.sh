#!/usr/bin/env bash
# Launch the OI-PCR Streamlit webapp. Reads the Analytics token from .env.
#   ./run_dashboard.sh            -> http://localhost:8501
#   PORT=9000 ./run_dashboard.sh  -> custom port
set -euo pipefail
cd "$(dirname "$0")"
[ -f .venv/bin/activate ] && source .venv/bin/activate
exec streamlit run dashboard.py --server.port "${PORT:-8501}"
