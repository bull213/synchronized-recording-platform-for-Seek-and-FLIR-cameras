$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

& (Join-Path $PSScriptRoot 'Start_Sync_Recorder.cmd') @args
exit $LASTEXITCODE
