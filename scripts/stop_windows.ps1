# Stop FinAlly (Windows PowerShell). Idempotent. The data volume is kept.
$ErrorActionPreference = "Stop"

$Container = "finally"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "docker is not installed or not on PATH."
    exit 1
}

docker container inspect $Container *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Stopping and removing container '$Container'..."
    docker rm -f $Container *> $null
    Write-Host "Stopped. Data volume 'finally-data' was kept."
} else {
    Write-Host "Container '$Container' is not running."
}
