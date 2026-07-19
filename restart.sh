#!/bin/bash
# Strategy Lab restart script (for Git Bash)
cd "$(dirname "$0")"

echo "[1/3] Stopping old services on port 8000..."
for pid in $(netstat -ano | grep ":8000" | grep LISTENING | awk '{print $5}'); do
  taskkill //F //PID "$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null
done

echo "[2/3] Waiting..."
sleep 3

echo "[3/3] Starting new service..."
/c/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m strategylab &

sleep 2
echo "Done! Visit http://localhost:8000"
