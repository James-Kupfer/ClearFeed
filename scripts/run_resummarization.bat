@echo off
:: Re-run ONLY the summarize step on existing records (against stored body_text).
:: Filter args are passed straight through; with no args, defaults to --days 4.
:: Usage:
::   scripts\run_resummarization.bat
::   scripts\run_resummarization.bat --label Investment
::   scripts\run_resummarization.bat --days 7
::   scripts\run_reprocess.bat --all
::   scripts\run_resummarization.bat --all

setlocal
set ARGS=%*
if "%ARGS%"=="" set ARGS=--gap

call "%~dp0..\clearfeed.bat" reprocess-summary %ARGS%
endlocal
pause
