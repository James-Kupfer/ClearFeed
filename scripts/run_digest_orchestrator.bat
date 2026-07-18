@echo off
:: ClearFeed digest run. Runs every digest profile listed in orchestration/digest.yaml.
::
:: Usage:
::   scripts\run_digest_orchestrator.bat [--dry-run]

setlocal
set CLEARFEED_ROOT=%~dp0..
set PYTHONPATH=%CLEARFEED_ROOT%;%CLEARFEED_ROOT%\src;%PYTHONPATH%

python "%CLEARFEED_ROOT%\src\digest_orchestrator.py" %*

endlocal
