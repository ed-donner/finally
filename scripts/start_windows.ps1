$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$ImageName = "finally"
$ContainerName = "finally-app"

Set-Location $ProjectDir

# Build if needed or if --build flag passed
if ($args -contains "--build" -or -not (docker image inspect $ImageName 2>$null)) {
    Write-Host "Building Docker image..."
    docker build -t $ImageName .
}

# Stop existing container
docker rm -f $ContainerName 2>$null

# Run
Write-Host "Starting FinAlly..."
docker run -d `
    --name $ContainerName `
    -p 8000:8000 `
    -v finally-data:/app/db `
    --env-file .env `
    $ImageName

Write-Host "FinAlly is running at http://localhost:8000"
Start-Process "http://localhost:8000"
