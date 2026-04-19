#!/bin/bash
pip install -r requirements.txt

# Background keepalive: pings own health endpoint every 10 minutes
(while true; do
  sleep 600
  curl -s --max-time 10 http://localhost:${PORT:-8080}/ > /dev/null 2>&1
  echo "[keepalive] pinged localhost:${PORT:-8080} at $(date)"
done) &

python bot_praias.py
