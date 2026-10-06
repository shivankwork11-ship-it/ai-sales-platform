# Regular Mongo -> laptop sync (read-only), registered with Windows Task Scheduler.
#
#   Register (run once, in a normal PowerShell — no admin needed):
#       powershell -ExecutionPolicy Bypass -File .\schedule_sync.ps1 -Register
#   Change the cadence:
#       ... -Register -EveryMinutes 120
#   Check / remove:
#       ... -Status
#       ... -Unregister
#
# What it runs: sync.py (incremental — the watermark bug is fixed, it now
# watermarks on updatedAt with a 90-minute overlap) then audit_pii.py. If the
# audit is not CLEAN the run is marked failed and the log says so, because an
# unaudited cache must never be treated as usable.
#
# Reads only. readonly.py patches every pymongo write to raise, so a scheduled
# job cannot mutate production even if something upstream changes.
# Signature: CI Team - citeam.claudemax@gmail.com

param(
    [switch]$Register,
    [switch]$Unregister,
    [switch]$Status,
    [switch]$RunNow,
    [int]$EveryMinutes = 60
)

$ErrorActionPreference = 'Stop'
$Here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = 'ANGAD-sync'
$LogDir   = Join-Path $Here 'RELEASES\sync_logs'

function Get-Python {
    $c = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $c) { throw 'python not found on PATH' }
    return $c.Source
}

function Invoke-Sync {
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force $LogDir | Out-Null }
    $stamp = Get-Date -Format 'yyyy-MM-dd_HHmm'
    $log   = Join-Path $LogDir "sync_$stamp.log"
    $py    = Get-Python
    Set-Location $Here

    "=== ANGAD sync $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Encoding utf8
    $env:PYTHONIOENCODING = 'utf-8'

    & $py -u sync.py *>> $log
    $syncOk = $?

    & $py -u audit_pii.py *>> $log
    $auditText = Get-Content $log -Raw
    $clean = $auditText -match 'CLEAN'

    if (-not $syncOk) { "RESULT: FAILED (sync error)" | Out-File $log -Append -Encoding utf8; exit 1 }
    if (-not $clean)  { "RESULT: FAILED (PII audit NOT clean - cache must not be used)" |
                        Out-File $log -Append -Encoding utf8; exit 2 }
    "RESULT: OK (sync + PII audit clean)" | Out-File $log -Append -Encoding utf8

    # keep the log directory from growing without bound
    Get-ChildItem $LogDir -Filter 'sync_*.log' |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 60 |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

if ($RunNow) { Invoke-Sync; return }

if ($Status) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $t) { "Not registered." ; return }
    $i = Get-ScheduledTaskInfo -TaskName $TaskName
    [pscustomobject]@{
        Task      = $TaskName
        State     = $t.State
        LastRun   = $i.LastRunTime
        LastResult= $i.LastTaskResult
        NextRun   = $i.NextRunTime
    } | Format-List
    if (Test-Path $LogDir) {
        "`nLatest logs:"
        Get-ChildItem $LogDir -Filter 'sync_*.log' |
            Sort-Object LastWriteTime -Descending | Select-Object -First 5 |
            ForEach-Object {
                $r = (Select-String -Path $_.FullName -Pattern '^RESULT:' |
                      Select-Object -Last 1).Line
                "  {0}  {1}" -f $_.Name, ($(if ($r) { $r } else { 'no RESULT line (still running?)' }))
            }
    }
    return
}

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    "Removed scheduled task '$TaskName'."
    return
}

if ($Register) {
    $self   = Join-Path $Here 'schedule_sync.ps1'
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
              -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$self`" -RunNow" `
              -WorkingDirectory $Here
    # repeat forever, starting 2 minutes from now; only when the machine is on
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
               -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
                -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
                -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description 'ANGAD read-only Mongo -> local parquet sync + PII audit' `
        -Force | Out-Null
    "Registered '$TaskName' - every $EveryMinutes minutes, logs in RELEASES\sync_logs."
    "Check it with:  powershell -ExecutionPolicy Bypass -File .\schedule_sync.ps1 -Status"
    return
}

"Usage: -Register [-EveryMinutes N] | -Status | -RunNow | -Unregister"
