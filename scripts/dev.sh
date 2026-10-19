#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python3 scripts/init_env.py
docker compose --env-file .env.compose up --build --wait api
printf '\nQueuePilot: http://127.0.0.1:8000/docs\n'
