$ErrorActionPreference = "Stop"
$ContainerName = "finally"
$ImageName = "finally"

# Use docker if available, otherwise fall back to podman
if (Get-Command docker -ErrorAction SilentlyContinue) {
    $Engine = "docker"
} elseif (Get-Command podman -ErrorAction SilentlyContinue) {
    $Engine = "podman"
} else {
    Write-Error "Neither docker nor podman found on PATH."
    exit 1
}

# Stop existing container if running
& $Engine rm -f $ContainerName 2>$null

# Build if --build flag passed or image doesn't exist
& $Engine image inspect $ImageName *> $null
$imageExists = ($LASTEXITCODE -eq 0)
if ($args -contains "--build" -or -not $imageExists) {
    Write-Host "Building FinAlly image with $Engine..."
    & $Engine build -t $ImageName .
}

# Run container
& $Engine run -d `
    --name $ContainerName `
    -p 8000:8000 `
    -v finally-data:/app/db `
    --env-file .env `
    $ImageName

Write-Host ""
Write-Host "FinAlly is running at http://localhost:8000"
Write-Host "Stop with: .\scripts\stop_windows.ps1"
