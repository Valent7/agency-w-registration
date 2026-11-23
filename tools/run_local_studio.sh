#!/bin/sh
set -eu
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
data=${KADRIAN_LOCAL_DATA:-"$repo/../.local-studio"}
python=${KADRIAN_LOCAL_PYTHON:-"$data/venv/bin/python"}
export KADRIAN_LOCAL_DATA="$data"
export PATH="$data/bin:$PATH"
cd "$repo"
exec "$python" -m streamlit run tools/run_local_studio.py \
  --server.address=127.0.0.1 --server.port=8765 --server.headless=true \
  --browser.gatherUsageStats=false
