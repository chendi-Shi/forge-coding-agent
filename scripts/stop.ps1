$ErrorActionPreference = 'Stop'
& "$PSScriptRoot\stop_forge_worker.ps1"
& "$PSScriptRoot\stop_coding_worker.ps1"
& "$PSScriptRoot\compose.ps1" stop
if ($LASTEXITCODE -ne 0) { throw 'Some Compose services failed to stop' }
