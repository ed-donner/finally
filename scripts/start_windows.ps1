# FinAlly start script for Windows PowerShell
param(
    [switch]$Build
)

$ErrorActionPreference = "Stop"

$ContainerName = "finally"
$ImageName = "finally"
$Port = 8000
$Volume = "finally-data"

Set-Location (Split-Path $PSScriptRoot -Parent)

# Create .env if it doesn't exist
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "Created .env from .env.example — please edit it and add your OPENROUTER_API_KEY" -ForegroundColor Yellow
    }
}

# Check if already running
$running = docker ps --format "{{.Names}}" | Where-Object { $_ -eq $ContainerName }
if ($running) {
    Write-Host "FinAlly is already running at http://localhost:$Port" -ForegroundColor Green
    exit 0
}

# Build if needed
$imageExists = docker image inspect $ImageName 2>$null
if ($Build -or -not $imageExists) {
    Write-Host "Building FinAlly Docker image..." -ForegroundColor Cyan
    docker build -t $ImageName .
}

# Remove stopped container
docker rm -f $ContainerName 2>$null | Out-Null

# Run
Write-Host "Starting FinAlly..." -ForegroundColor Cyan
docker run -d `
    --name $ContainerName `
    -p "${Port}:8000" `
    -v "${Volume}:/app/db" `
    --env-file .env `
    --restart unless-stopped `
    $ImageName

Write-Host ""
Write-Host "✓ FinAlly is running at http://localhost:$Port" -ForegroundColor Green
Write-Host "  Logs: docker logs -f $ContainerName"
Write-Host "  Stop: .\scripts\stop_windows.ps1"

Start-Sleep -Seconds 1
Start-Process "http://localhost:$Port"
