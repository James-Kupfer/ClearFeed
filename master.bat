@echo off
:: ClearFeed master runner. Runs tests then a full ingest + dispatch pipeline.
:: Usage: master.bat
::
:: Stops on first failure. Run this before any live deployment.

setlocal
set PYTHONPATH=%~dp0;%~dp0src

echo ========================================
echo ClearFeed Master Run
echo ========================================

:: Step 1: tests
echo.
echo [1/3] Running test suite...
python -m pytest tests\ -v --tb=short
if errorlevel 1 (
    echo FAILED: tests did not pass. Aborting.
    exit /b 1
)
echo Tests passed.

:: Step 2: ingest
echo.
echo [2/3] Running ingest...
python "%~dp0src\ingest_gmail.py"
if errorlevel 1 (
    echo FAILED: ingest exited with error. Aborting.
    exit /b 1
)

:: Step 3: dispatch all profiles
echo.
echo [3/3] Running dispatch for all profiles...
for %%f in ("%~dp0profiles\*.yaml") do (
    echo   Dispatching: %%f
    python "%~dp0src\dispatch.py" "%%f"
    if errorlevel 1 (
        echo FAILED: dispatch failed for %%f. Aborting.
        exit /b 1
    )
)

echo.
echo ========================================
echo Master run complete.
echo ========================================
endlocal
