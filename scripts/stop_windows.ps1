$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ComposeFile = Join-Path $RootDir "docker-compose.yml"

docker compose -f $ComposeFile down
Write-Host "FinAlly container stopped. Persistent volume data is retained."
