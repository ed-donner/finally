# FinAlly - start script (Windows PowerShell).
# Idempotent: builds the image if missing (or with -Build), (re)creates the
# container with the persistent volume, port mapping, and .env file.
#
# Usage:
#   .\scripts\start_windows.ps1            # build if needed, then run
#   .\scripts\start_windows.ps1 -Build    # force a rebuild first
param(
    [switch]$Build
)

$ErrorActionPreference = 'Stop'

$Image     = 'finally:latest'
$Container = 'finally'
$Port      = '8000'
$Volume    = 'finally-data'

# Resolve repo root (parent of this script's directory) and move there.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir   = Split-Path -Parent $ScriptDir
Set-Location $RootDir

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error 'docker is not installed or not on PATH.'
    exit 1
}

# Ensure a .env exists (create from example on first run).
if (-not (Test-Path .env)) {
    Write-Host 'No .env found; creating one from .env.example.'
    Copy-Item .env.example .env
}

# Build the image if it doesn't exist yet, or if -Build was passed.
docker image inspect $Image *> $null
$imageExists = ($LASTEXITCODE -eq 0)
if ($Build -or -not $imageExists) {
    Write-Host "Building image $Image ..."
    docker build -t $Image .
    if ($LASTEXITCODE -ne 0) { Write-Error 'docker build failed.'; exit 1 }
} else {
    Write-Host "Image $Image already exists (use -Build to rebuild)."
}

# Remove any existing container (idempotent restart).
$existing = docker ps -a --format '{{.Names}}' | Select-String -SimpleMatch -Pattern $Container
if ($existing) {
    Write-Host "Removing existing container $Container ..."
    docker rm -f $Container *> $null
}

Write-Host "Starting container $Container ..."
docker run -d `
    --name $Container `
    -p "${Port}:8000" `
    -v "${Volume}:/app/db" `
    --env-file .env `
    $Image *> $null
if ($LASTEXITCODE -ne 0) { Write-Error 'docker run failed.'; exit 1 }

Write-Host ''
Write-Host 'FinAlly is starting up.'
Write-Host "  -> http://localhost:$Port"
Write-Host ''
Write-Host "Logs:  docker logs -f $Container"
Write-Host 'Stop:  .\scripts\stop_windows.ps1'
