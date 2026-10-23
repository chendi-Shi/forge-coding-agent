$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
python scripts/init_env.py
if ($LASTEXITCODE -ne 0) { throw 'Cannot initialize environment' }
& "$PSScriptRoot\compose.ps1" config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Invalid Compose configuration' }
# compose.ps1 also establishes the hidden project keepalive on WSL, so the
# detached services remain available after this launcher exits.
& "$PSScriptRoot\compose.ps1" build sandbox-runtime
if ($LASTEXITCODE -ne 0) { throw 'Sandbox runtime image build failed' }
& "$PSScriptRoot\compose.ps1" up --build --detach --wait --wait-timeout 240
if ($LASTEXITCODE -ne 0) { throw 'Services failed to become ready; inspect scripts/compose.ps1 logs --tail 100' }
& "$PSScriptRoot\start_forge_worker.ps1"
Write-Output 'Coding Agent workspace is ready at http://127.0.0.1:2026/studio (or HOST_PORT from .env).'
Write-Output 'Stop this project with scripts/compose.ps1 stop or down; this also releases its WSL keepalive.'
