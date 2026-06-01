#!/bin/bash
cd "$(dirname "$0")"
export npm_config_cache=/tmp/npm-cache-hanaa
exec npx browser-sync start \
  --proxy localhost:8000 \
  --files 'frontend/**/*' \
  --host 0.0.0.0 \
  --port 3000 \
  --no-ui --no-open --no-notify
