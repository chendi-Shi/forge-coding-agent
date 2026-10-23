param(
    [string]$BaseUrl = 'http://127.0.0.1:2026',
    [ValidateRange(1, 100)][int]$Concurrency = 2,
    [string]$Python = '',
    [string]$ExecutorId = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$workerPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'forge_worker.py'))
$envPath = Join-Path $projectRoot '.env'
$localSettings = @{}
if (Test-Path -LiteralPath $envPath) {
    foreach ($line in [System.IO.File]::ReadLines($envPath)) {
        if ($line -match '^\s*(?:export\s+)?(HOST_PORT|CODING_WORKER_CONCURRENCY|FORGE_PROVIDER)\s*=\s*(.*?)\s*$') {
            $localSettings[$Matches[1]] = $Matches[2].Trim().Trim('"', "'")
        }
    }
}
if (-not $PSBoundParameters.ContainsKey('BaseUrl') -and $localSettings.ContainsKey('HOST_PORT')) {
    $port = 0
    if (-not [int]::TryParse($localSettings['HOST_PORT'], [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        throw 'HOST_PORT must be an integer from 1 to 65535.'
    }
    $BaseUrl = "http://127.0.0.1:$port"
}
if (-not $PSBoundParameters.ContainsKey('Concurrency') -and $localSettings.ContainsKey('CODING_WORKER_CONCURRENCY')) {
    $configuredCapacity = 0
    if (-not [int]::TryParse($localSettings['CODING_WORKER_CONCURRENCY'], [ref]$configuredCapacity) -or
        $configuredCapacity -lt 1 -or $configuredCapacity -gt 100) {
        throw 'CODING_WORKER_CONCURRENCY must be an integer from 1 to 100.'
    }
    $Concurrency = $configuredCapacity
}
$runtimePath = Join-Path $projectRoot '.forge-workspaces'
if (-not (Test-Path -LiteralPath $runtimePath)) {
    New-Item -ItemType Directory -Path $runtimePath | Out-Null
}
$pidPath = Join-Path $runtimePath 'worker.pid.json'
$stopPath = Join-Path $runtimePath 'worker.stop'
$lockPath = Join-Path $runtimePath 'worker-control.lock'
$controlLock = [System.IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')

function Test-WorkerIdentity($Record, $Process) {
    if ($null -eq $Process -or -not $Record.instance_id -or -not $Process.CommandLine) { return $false }
    return ($Record.script -eq $workerPath -and
        $Process.ExecutablePath -eq $Record.python -and
        $Process.CommandLine.Contains($workerPath) -and
        $Process.CommandLine.Contains([string]$Record.instance_id) -and
        $Process.CreationDate.ToUniversalTime().Ticks -eq ([DateTime]$Record.created_utc).ToUniversalTime().Ticks)
}

function ConvertTo-ProcessArgument([string]$Value) {
    # Start-Process joins ArgumentList into one Windows command line.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

try {
    if (Test-Path -LiteralPath $pidPath) {
        $record = Get-Content -Raw -LiteralPath $pidPath | ConvertFrom-Json
        $existing = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$record.pid)"
        if (Test-WorkerIdentity $record $existing) {
            Write-Output "Forge worker is already running (PID $($record.pid))."
            return
        }
        if ($null -ne $existing) {
            throw 'PID record does not match this project worker; no process was touched. Inspect the stale record.'
        }
    }
    if (-not $Python) {
        $projectPython = Join-Path $projectRoot '.venv/Scripts/python.exe'
        if (Test-Path -LiteralPath $projectPython) { $Python = $projectPython }
        else { $Python = (Get-Command python -CommandType Application | Select-Object -First 1).Source }
    }
    $Python = [System.IO.Path]::GetFullPath($Python)
    $providerName = if ($env:FORGE_PROVIDER) { $env:FORGE_PROVIDER } elseif ($localSettings.ContainsKey('FORGE_PROVIDER')) { $localSettings['FORGE_PROVIDER'] } else { 'codex_bridge' }
    $codexPath = if ($providerName -eq 'openai_compatible') { 'unused-http-provider' } else { (Get-Command codex -CommandType Application | Select-Object -First 1).Source }
    & $Python -c 'import httpx' 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'The selected Python needs httpx installed.' }
    if (-not $ExecutorId) { $ExecutorId = 'forge-' + [guid]::NewGuid().ToString('N').Substring(0, 12) }
    if ($ExecutorId -notmatch '^[A-Za-z0-9_.-]{1,128}$') { throw 'Invalid ExecutorId.' }
    if (Test-Path -LiteralPath $stopPath) { Remove-Item -LiteralPath $stopPath }
    $instanceId = [guid]::NewGuid().ToString('N')
    $workerArguments = @('-u', $workerPath, '--base-url', $BaseUrl, '--concurrency', [string]$Concurrency,
        '--executor-id', $ExecutorId, '--codex-executable', $codexPath, '--workspaces', $runtimePath,
        '--stop-file', $stopPath, '--instance-id', $instanceId)
    $argumentLine = ($workerArguments | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join ' '
    $process = Start-Process -FilePath $Python -ArgumentList $argumentLine -WorkingDirectory $projectRoot `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath 'worker.stdout.log') `
        -RedirectStandardError (Join-Path $runtimePath 'worker.stderr.log')
    Start-Sleep -Milliseconds 700
    $process.Refresh()
    if ($process.HasExited) { throw 'Worker stopped during startup; inspect .forge-workspaces/worker.stdout.log.' }
    $identity = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)"
    if ($null -eq $identity) { throw 'Cannot read the new worker process identity.' }
    @{
        pid = $process.Id
        script = $workerPath
        python = $Python
        created_utc = $identity.CreationDate.ToUniversalTime().ToString('o')
        instance_id = $instanceId
        executor_id = $ExecutorId
    } | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding UTF8
    Write-Output "Forge worker started (PID $($process.Id), capacity $Concurrency). Logs: $runtimePath"
}
finally {
    $controlLock.Dispose()
}
