# Start FinAlly in Docker (Windows PowerShell). Idempotent.
#
# Usage: .\scripts\start_windows.ps1 [-Build] [-NoOpen]
#   -Build   force a rebuild of the Docker image
#   -NoOpen  do not open the browser
param(
    [switch]$Build,
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"

$Image = "finally"
$Container = "finally"
$Volume = "finally-data"
$Port = if ($env:FINALLY_PORT) { $env:FINALLY_PORT } else { "8000" }
$Url = "http://localhost:$Port"

$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "docker is not installed or not on PATH."
    exit 1
}
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Cannot talk to the Docker daemon. Is Docker Desktop running?"
    exit 1
}

# --- .env ---
$EnvArgs = @()
if (Test-Path ".env") {
    $EnvArgs = @("--env-file", ".env")
} elseif (Test-Path ".env.example") {
    Write-Warning ".env not found; creating it from .env.example. Set OPENROUTER_API_KEY in .env to enable AI chat."
    Copy-Item ".env.example" ".env"
    $EnvArgs = @("--env-file", ".env")
} else {
    Write-Warning "No .env file; starting without environment file (AI chat will not work)."
}

# --- Image ---
docker image inspect $Image *> $null
if ($Build -or $LASTEXITCODE -ne 0) {
    Write-Host "Building Docker image '$Image'..."
    docker build -t $Image .
    if ($LASTEXITCODE -ne 0) { Write-Error "docker build failed."; exit 1 }
}

# --- Container ---
docker container inspect $Container *> $null
if ($LASTEXITCODE -eq 0) {
    $running = (docker container inspect -f "{{.State.Running}}" $Container).Trim()
    if ($running -eq "true" -and -not $Build) {
        Write-Host "Container '$Container' is already running."
    } else {
        Write-Host "Removing existing container '$Container'..."
        docker rm -f $Container *> $null
    }
}

docker container inspect $Container *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Starting container '$Container'..."
    docker run -d --name $Container -v "${Volume}:/app/db" -p "${Port}:8000" @EnvArgs $Image | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Error "docker run failed."; exit 1 }
}

# --- Wait for health ---
Write-Host -NoNewline "Waiting for FinAlly to become healthy"
$healthy = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "$Url/api/health" -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
    $state = (docker container inspect -f "{{.State.Running}}" $Container 2>$null)
    if ($state -ne "true") {
        Write-Host ""
        Write-Error "Container exited. Recent logs:`n$(docker logs --tail 50 $Container 2>&1 | Out-String)"
        exit 1
    }
    Write-Host -NoNewline "."
    Start-Sleep -Seconds 1
}
Write-Host ""

if ($healthy) {
    Write-Host "FinAlly is running at $Url"
    if (-not $NoOpen) { Start-Process $Url }
} else {
    Write-Warning "Health check did not pass within 60s. Check: docker logs $Container"
}
