# Run this project's Compose stack using the selected local engine.
$ErrorActionPreference = 'Stop'
$taskProject = Split-Path -Parent $PSScriptRoot
$taskRuntimeFile = Join-Path $taskProject '.runtime.json'
$taskComposeArguments = @($args)
$taskComposeCommand = if ($taskComposeArguments.Count) { [string]$taskComposeArguments[0] } else { '' }
$taskProjectName = 'agent-backend-aegra'
if (Test-Path -LiteralPath $taskRuntimeFile) {
    $taskRuntime = Get-Content -LiteralPath $taskRuntimeFile -Raw | ConvertFrom-Json
} else {
    $taskRuntime = @{ backend = 'desktop' }
}
if ($taskRuntime.backend -eq 'wsl') {
    $taskDistro = [string]$taskRuntime.distro
    if ($taskDistro -notmatch '^[A-Za-z0-9_.-]+$') { throw 'Invalid WSL distribution in .runtime.json' }
    $taskWslExecutable = (Get-Command wsl.exe -ErrorAction Stop).Source
    $taskLinuxProject = (& $taskWslExecutable -d $taskDistro -u root -- wslpath -a $taskProject).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve project directory in WSL' }
    if (-not $taskLinuxProject.StartsWith('/') -or $taskLinuxProject -match '["\r\n\x00]') {
        throw 'Invalid WSL project directory'
    }
    $taskLinuxKeeper = "$taskLinuxProject/scripts/wsl_keepalive.sh"
    if ($taskComposeCommand -in @('up', 'start', 'restart')) {
        # systemd daemons alone do not keep a WSL distribution alive. Keep one
        # foreground Linux process attached to a hidden, persistent wsl.exe.
        # The flock in the helper makes repeated launches harmless.
        $taskKeeperArguments = @('-d', $taskDistro, '-u', 'root', '--', '/bin/bash', ('"' + $taskLinuxKeeper + '"'), 'hold')
        $taskKeeperProcess = Start-Process -FilePath $taskWslExecutable -ArgumentList $taskKeeperArguments -WindowStyle Hidden -PassThru
        $taskKeeperReady = $false
        for ($taskAttempt = 0; $taskAttempt -lt 20; $taskAttempt++) {
            & $taskWslExecutable -d $taskDistro -u root -- /bin/bash $taskLinuxKeeper status
            if ($LASTEXITCODE -eq 0) { $taskKeeperReady = $true; break }
            if ($taskKeeperProcess.HasExited -and $taskKeeperProcess.ExitCode -ne 0) { break }
            Start-Sleep -Milliseconds 250
        }
        if (-not $taskKeeperReady) { throw 'Cannot establish the project WSL keepalive process' }
    }
    & $taskWslExecutable -d $taskDistro -u root -- systemctl start docker
    if ($LASTEXITCODE -ne 0) { throw 'Cannot start the selected WSL Docker Engine' }
    & $taskWslExecutable -d $taskDistro -u root -- /usr/bin/docker --host unix:///var/run/docker.sock compose --project-directory $taskLinuxProject --project-name $taskProjectName @taskComposeArguments
    $taskComposeExitCode = $LASTEXITCODE
    if ($taskComposeExitCode -eq 0 -and $taskComposeCommand -in @('stop', 'down')) {
        # A partial `stop api` must not shut down databases or other services.
        $taskStillRunning = @(& $taskWslExecutable -d $taskDistro -u root -- /usr/bin/docker --host unix:///var/run/docker.sock compose --project-directory $taskLinuxProject --project-name $taskProjectName ps --status running --quiet)
        if ($LASTEXITCODE -ne 0) { throw 'Cannot verify project shutdown; WSL keepalive was retained' }
        if (-not ($taskStillRunning | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
            & $taskWslExecutable -d $taskDistro -u root -- /bin/bash $taskLinuxKeeper stop
            if ($LASTEXITCODE -ne 0) { throw 'Services stopped, but the project WSL keepalive did not exit' }
        }
    }
} else {
    & docker compose --project-directory $taskProject --project-name $taskProjectName @taskComposeArguments
    $taskComposeExitCode = $LASTEXITCODE
}
exit $taskComposeExitCode
