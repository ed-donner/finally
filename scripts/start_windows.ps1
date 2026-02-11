$ErrorActionPreference = "Stop"
$ContainerName = "finally"
$ImageName = "finally"

# Stop existing container if running
docker rm -f $ContainerName 2>$null

# Build if --build flag passed or image doesn't exist
if ($args -contains "--build" -or -not (docker image inspect $ImageName 2>$null)) {
    Write-Host "Building FinAlly Docker image..."
    docker build -t $ImageName .
}

# Run container
docker run -d `
    --name $ContainerName `
    -p 8000:8000 `
    -v finally-data:/app/db `
    --env-file .env `
    $ImageName

Write-Host ""
Write-Host "FinAlly is running at http://localhost:8000"
Write-Host "Stop with: .\scripts\stop_windows.ps1"
