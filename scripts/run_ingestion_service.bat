@echo off
:: ClearFeed continuous ingest loop.
::
:: Runs ingest, sleeps INGEST_POLL_INTERVAL_MINUTES, repeats forever.
:: Poll interval is read from src/config.py so it stays in one place.
::
:: Usage:
::   Directly (shows window):  scripts\run_ingestion.bat
::   Hidden background:        wscript scripts\launch_hidden.vbs

setlocal
set CLEARFEED_ROOT=%~dp0..
set PYTHONPATH=%CLEARFEED_ROOT%;%CLEARFEED_ROOT%\src;%PYTHONPATH%

:: Read poll interval from config (converts minutes to seconds)
for /f %%i in ('python -c "import config; print(config.INGEST_POLL_INTERVAL_MINUTES * 60)"') do set SLEEP_SECS=%%i

echo ClearFeed ingest polling — interval %SLEEP_SECS%s. Press Ctrl+C to stop.

:loop
python "%CLEARFEED_ROOT%\src\ingest_orchestrator.py"
timeout /t %SLEEP_SECS% /nobreak >nul
goto loop

endlocal
