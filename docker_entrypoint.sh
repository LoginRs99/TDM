#!/bin/bash
# Headless healthcheck - no Xvfb needed

TIMESTAMP_FILE="/home/appuser/app/healthcheck.timestamp"
MAX_AGE_SECONDS=300 # 5 minutes

# Check if main process is running (NO Xvfb check)
if ! pgrep -f "python main.py" > /dev/null; then
  echo "Healthcheck FAILED: main.py process not found."
  exit 1
fi

# Check timestamp freshness
if [ ! -f "$TIMESTAMP_FILE" ]; then
  echo "Healthcheck: Timestamp file not found yet, but process is running. Startup in progress."
  exit 0
else
  timestamp_value=$(cat "$TIMESTAMP_FILE" 2>/dev/null | cut -d',' -f1)
  
  if ! [[ "$timestamp_value" =~ ^[0-9]+$ ]]; then
    echo "Healthcheck FAILED: Invalid timestamp format."
    exit 1
  fi
  
  current_time=$(date +%s)
  age=$((current_time - timestamp_value))
  
  if [ "$age" -gt "$MAX_AGE_SECONDS" ]; then
    echo "Healthcheck FAILED: Timestamp is too old ($age seconds)."
    exit 1
  fi
  
  echo "Healthcheck OK: Fresh timestamp ($age seconds old) and process running."
fi

exit 0