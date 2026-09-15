@echo off
REM One-off history backfill: stores and scores, never alerts.
REM Usage: run_backfill.bat [days]   (default 90). Days already stored are skipped
REM quickly, so re-running after an interruption just continues. Log: data\backfill.log
cd /d C:\Users\tralp\Downloads\order_scanner\order_scanner
call C:\Users\%USERNAME%\anaconda3\Scripts\activate.bat
set PYTHONIOENCODING=utf-8
set DAYS=%1
if "%DAYS%"=="" set DAYS=90
python -m order_scanner.cli backfill --days %DAYS% --probe-pdfs >> data\backfill.log 2>&1
