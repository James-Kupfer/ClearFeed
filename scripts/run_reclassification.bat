@echo off
:: Re-run ONLY the classify step on existing records (against the stored summary).
:: Filter args are passed straight through; with no args, defaults to --days 4.
:: Usage:
::   scripts\run_reclassification.bat             
::   scripts\run_reclassification.bat --label Investment
::   scripts\run_reclassification.bat --days 7
::   scripts\run_reprocess.bat --all
::   scripts\run_reclassification.bat --all

setlocal
set ARGS=%*
if "%ARGS%"=="" set ARGS=--gaps

call "%~dp0..\clearfeed.bat" reprocess %ARGS%
endlocal
pause
