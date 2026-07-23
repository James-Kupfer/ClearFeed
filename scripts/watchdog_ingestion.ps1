<#
ClearFeed ingestion-loop watchdog.

Checks whether the continuous ingestion loop (run_ingestion_service.bat, started
by the Ingestion_Service task's at-logon trigger) is actually alive, and relaunches
it if it's gone or wedged. Runs on its own schedule (Ingestion_Watchdog task) so a
dead or hung loop gets noticed within minutes instead of sitting dead until the
next login.

Only ever acts after confirming the current state via a live process check --
never blind. MultipleInstancesPolicy=IgnoreNew on Ingestion_Service does NOT
protect against a duplicate loop, because that task's wscript.exe launcher action
is fire-and-forget and exits within milliseconds of detaching the real loop, so a
naive timer-based relaunch could easily double the loop. See
system_architecture.md for the full explanation.

When more than one loop is found, this script trims the duplicates itself: it
keeps the oldest (the presumed original) and kills the rest. This is safe even
though it's not "blind" launching -- it only ever removes processes it already
confirmed are excess copies of the same script, never starts a new one in this
branch, so it can't itself cause the doubling described above.
#>

$ErrorActionPreference = 'Stop'

$root      = Split-Path -Parent $PSScriptRoot
$logDir    = Join-Path $root 'logs'
$watchLog  = Join-Path $logDir 'watchdog.txt'
$launchVbs = Join-Path $root 'scripts\launch_hidden.vbs'
$batName   = 'run_ingestion_service.bat'

function Write-WatchLog([string]$message) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    Add-Content -Path $watchLog -Value "$stamp $message"
}

try {
    # Poll interval, read from config.py so the staleness threshold always tracks
    # the real configured cadence. Falls back to 15 (the shipped default) if the
    # config read fails for any reason -- that failure is not fatal to the
    # liveness check below.
    $intervalMinutes = 15
    try {
        $env:PYTHONPATH = "$root\src;$root;$env:PYTHONPATH"
        $intervalRaw = & python -c "import config; print(config.INGEST_POLL_INTERVAL_MINUTES)"
        if ($LASTEXITCODE -eq 0 -and $intervalRaw -match '^\d+$') {
            $intervalMinutes = [int]$intervalRaw
        }
    } catch {
        # keep default
    }
    $staleThresholdMinutes = $intervalMinutes * 3

    $matches = @(Get-CimInstance Win32_Process -Filter "Name = 'cmd.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$batName*" })

    if ($matches.Count -gt 1) {
        $pidList = ($matches | ForEach-Object { $_.ProcessId }) -join ', '
        $sorted = $matches | Sort-Object CreationDate
        $survivor = $sorted[0]
        $duplicates = $sorted | Select-Object -Skip 1
        Write-WatchLog "WARNING multiple ingestion loops detected (PIDs: $pidList) -- keeping oldest PID $($survivor.ProcessId), killing duplicate(s)"
        foreach ($dup in $duplicates) {
            try {
                & taskkill.exe /PID $dup.ProcessId /T /F | Out-Null
                Write-WatchLog "Killed duplicate ingestion loop PID $($dup.ProcessId)"
            } catch {
                Write-WatchLog "ERROR failed to kill duplicate ingestion loop PID $($dup.ProcessId): $($_.Exception.Message)"
            }
        }
        return
    }

    $newestLog = Get-ChildItem $logDir -Filter '*_ingest_orchestrator.txt' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $ageMinutes = if ($newestLog) { ((Get-Date) - $newestLog.LastWriteTime).TotalMinutes } else { $null }

    if ($matches.Count -eq 1) {
        $proc = $matches[0]
        $isStale = ($null -ne $ageMinutes) -and ($ageMinutes -gt $staleThresholdMinutes)
        if (-not $isStale) {
            # Alive and either mid-cycle or within a normal sleep window -- healthy, stay quiet.
            return
        }
        Write-WatchLog "Loop PID $($proc.ProcessId) alive but newest log is $([int]$ageMinutes)m old (threshold ${staleThresholdMinutes}m) -- killing tree and relaunching"
        & taskkill.exe /PID $proc.ProcessId /T /F | Out-Null
        Start-Sleep -Seconds 2
    } else {
        $ageDesc = if ($newestLog) { "$([int]$ageMinutes)m old" } else { 'none' }
        Write-WatchLog "No ingestion loop process found (newest log: $ageDesc) -- relaunching"
    }

    & wscript.exe $launchVbs
    Write-WatchLog "Relaunch issued via $launchVbs"
}
catch {
    Write-WatchLog "ERROR watchdog failed: $($_.Exception.Message)"
}
