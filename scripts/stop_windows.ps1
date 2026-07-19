# FinAlly - stop script (Windows PowerShell).
# Stops and removes the running container. Does NOT remove the data volume,
# so the SQLite database (portfolio, trades, chat history) persists.
#
# Usage:
#   .\scripts\stop_windows.ps1
$ErrorActionPreference = 'Stop'

$Container = 'finally'

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error 'docker is not installed or not on PATH.'
    exit 1
}

$existing = docker ps -a --format '{{.Names}}' | Select-String -SimpleMatch -Pattern $Container
if ($existing) {
    Write-Host "Stopping and removing container $Container ..."
    docker rm -f $Container *> $null
    Write-Host "Stopped. Data volume 'finally-data' preserved."
} else {
    Write-Host "Container $Container is not running; nothing to do."
}
