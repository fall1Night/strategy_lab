@echo off
cd /d E:\量化交易\strategy_lab

echo [1/3] Stopping old services on port 8000...
for /f "tokens=5" %%i in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /f /pid %%i >nul 2>&1
)

echo [2/3] Waiting for port release...
ping -n 3 127.0.0.1 >nul

echo [3/3] Starting new service...
start "StrategyLab" "C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m strategylab

ping -n 3 127.0.0.1 >nul
echo Done! Please visit http://localhost:8000
pause
