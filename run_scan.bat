@echo off
REM Run by Windows Task Scheduler (task "Order Scanner"): every hour, 08:00-22:00 IST.
REM Each scan also covers everything since the previous run, so hours the PC was
REM off or asleep are caught up on the next run (up to 4 days; use backfill beyond).
cd /d C:\Users\tralp\Downloads\order_scanner\order_scanner
call C:\Users\%USERNAME%\anaconda3\Scripts\activate.bat
set PYTHONIOENCODING=utf-8
python -m order_scanner.cli scan >> data\scan.log 2>&1
