param([ValidateRange(1, 300)][int]$WaitSeconds = 30)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$workerPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'coding_worker.py'))
$runtimePath = Join-Path $projectRoot '.coding-workspaces'
$pidPath = Join-Path $runtimePath 'worker.pid.json'
if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Output 'No project coding worker PID record exists.'
    return
}
$controlLock = [System.IO.File]::Open((Join-Path $runtimePath 'worker-control.lock'), 'OpenOrCreate', 'ReadWrite', 'None')

function Get-MatchingWorker($Record) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$Record.pid)"
    if ($null -eq $process) { return $null }
    if (-not $process.CommandLine -or -not $process.ExecutablePath) {
        # CIM can briefly return a terminating process after its image vanished.
        Start-Sleep -Milliseconds 200
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$Record.pid)"
        if ($null -eq $process) { return $null }
    }
    if ($Record.script -ne $workerPath -or -not $Record.instance_id -or
        $process.ExecutablePath -ne $Record.python -or -not $process.CommandLine -or
        -not $process.CommandLine.Contains($workerPath) -or
        -not $process.CommandLine.Contains([string]$Record.instance_id) -or
        $process.CreationDate.ToUniversalTime().Ticks -ne ([DateTime]$Record.created_utc).ToUniversalTime().Ticks) {
        throw 'PID identity does not match this project worker; no process was touched.'
    }
    return $process
}

try {
    $record = Get-Content -Raw -LiteralPath $pidPath | ConvertFrom-Json
    $process = Get-MatchingWorker $record
    if ($null -ne $process) {
        Set-Content -LiteralPath (Join-Path $runtimePath 'worker.stop') -Value 'stop' -Encoding UTF8
        $deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
        do {
            Start-Sleep -Milliseconds 300
            $process = Get-MatchingWorker $record
        } while ($null -ne $process -and [DateTime]::UtcNow -lt $deadline)
        if ($null -ne $process) {
            # Identity was checked immediately before force-stopping this tree.
            & taskkill.exe /PID ([string]$record.pid) /T /F | Out-Null
            if ($LASTEXITCODE -ne 0 -and $null -ne (Get-MatchingWorker $record)) {
                throw 'Could not stop the project worker.'
            }
        }
    }
    Remove-Item -LiteralPath $pidPath
    Write-Output 'Project coding worker stopped.'
}
finally {
    $controlLock.Dispose()
}
