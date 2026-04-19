#!/bin/bash
pip install -r requirements.txt

# Background keepalive: pings EXTERNAL URL every 10 minutes to prevent F1 sleep
(while true; do
  sleep 600
  curl -s --max-time 10 https://praias-pe-bot.azurewebsites.net/ > /dev/null 2>&1
  echo "[keepalive] pinged external URL at $(date)"
done) &

python bot_praias.py
