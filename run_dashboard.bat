@echo off
REM Started by Task Scheduler when you sign in (task "Order Scanner Dashboard").
REM Serves the dashboard at http://127.0.0.1:8765 on this PC only. Log: data\dashboard.log
REM Anaconda's python.exe is called directly: activating the environment first
REM added ~16 seconds before the page came up after sign-in.
cd /d C:\Users\tralp\Downloads\order_scanner\order_scanner
set PYTHONIOENCODING=utf-8
C:\Users\%USERNAME%\anaconda3\python.exe -m order_scanner.cli dashboard --port 8765 >> data\dashboard.log 2>&1
