@echo off
:: ClearFeed stage runner. Usage: clearfeed.bat <stage> [args]
::   clearfeed.bat ingest
::   clearfeed.bat ingest --dry-run
::   clearfeed.bat ingest --dry-run --limit 5
::   clearfeed.bat dispatch profiles\investment_digest.yaml
::   clearfeed.bat action  profiles\task_connection.yaml
::   clearfeed.bat export  profiles\job_export.yaml
::   clearfeed.bat reprocess --label Investment
::   clearfeed.bat reprocess-summary --label Investment

setlocal
set PYTHONPATH=%~dp0;%~dp0src
set CLEARFEED_ROOT=%~dp0

if "%1"=="" (
    echo Usage: clearfeed.bat ^<ingest^|dispatch^|reprocess^|reprocess-summary^> [args]
    exit /b 1
)

if "%1"=="ingest" (
    python "%~dp0src\ingest_gmail.py" %2 %3 %4 %5
    goto :end
)

if "%1"=="dispatch" (
    if "%2"=="" (
        echo dispatch requires a profile path, e.g.: clearfeed.bat dispatch profiles\investment_digest.yaml
        exit /b 1
    )
    python "%~dp0src\dispatch.py" "%2"
    goto :end
)

if "%1"=="reprocess" (
    python "%~dp0src\reprocess.py" %2 %3 %4 %5
    goto :end
)

if "%1"=="reprocess-summary" (
    python "%~dp0src\reprocess_summary.py" %2 %3 %4 %5
    goto :end
)

if "%1"=="export" (
    if "%~2"=="" (
        echo export requires a profile path, e.g.: clearfeed.bat export profiles\job_export.yaml
        exit /b 1
    )
    python "%~dp0src\export.py" "%~2"
    goto :end
)

if "%1"=="action" (
    if "%~2"=="" (
        echo action requires a profile path, e.g.: clearfeed.bat action profiles\task_connection.yaml
        exit /b 1
    )
    python "%~dp0src\action_dispatch.py" "%~2"
    goto :end
)

echo Unknown stage: %1
exit /b 1

:end
endlocal
