#!/bin/bash
cd "$(dirname "$0")"
exec .venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
