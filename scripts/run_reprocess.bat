@echo off
:: Full reprocess: re-summarize FIRST, then re-classify off the fresh summaries.
:: The same filter args are passed to both stages; with no args, defaults to
:: --days 4. Aborts if the summarize pass fails, so classification never runs on
:: stale summaries.
:: Usage:
::   scripts\run_reprocess.bat
::   scripts\run_reprocess.bat --label Investment
::   scripts\run_reprocess.bat --days 7
::   scripts\run_reprocess.bat --gap
::   scripts\run_reprocess.bat --all

setlocal
set ARGS=%*
if "%ARGS%"=="" set ARGS=--gap

echo [1/2] Re-summarizing...
call "%~dp0..\clearfeed.bat" reprocess-summary %ARGS%
if errorlevel 1 (
    echo FAILED: re-summarization failed. Aborting before re-classification.
    pause
    exit /b 1
)

echo.
echo [2/2] Re-classifying...
call "%~dp0..\clearfeed.bat" reprocess %ARGS%
if errorlevel 1 (
    echo FAILED: re-classification failed.
    pause
    exit /b 1
)

echo.
echo Reprocess complete.
endlocal
timeout /t 100
