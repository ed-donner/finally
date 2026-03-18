$ErrorActionPreference = "Stop"

$ContainerName = "finally"
$ImageName = "finally"
$VolumeName = "finally-data"
$Port = 8000
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$EnvFile = Join-Path $ProjectDir ".env"

# Check for .env file
$EnvFlag = @()
if (Test-Path $EnvFile) {
    $EnvFlag = @("--env-file", $EnvFile)
} else {
    Write-Host "Warning: .env file not found at $EnvFile"
    Write-Host "Copy .env.example to .env and fill in your API keys."
    Write-Host "Continuing without environment variables..."
}

# Stop existing container if running
$running = docker ps -q -f "name=$ContainerName" 2>$null
if ($running) {
    Write-Host "Stopping existing $ContainerName container..."
    docker stop $ContainerName 2>$null | Out-Null
    docker rm $ContainerName 2>$null | Out-Null
} else {
    $stopped = docker ps -aq -f "name=$ContainerName" 2>$null
    if ($stopped) {
        Write-Host "Removing stopped $ContainerName container..."
        docker rm $ContainerName 2>$null | Out-Null
    }
}

# Build image if it doesn't exist or -Build flag is passed
$needsBuild = $false
if ($args -contains "--build" -or $args -contains "-Build") {
    $needsBuild = $true
} else {
    try {
        docker image inspect $ImageName 2>$null | Out-Null
    } catch {
        $needsBuild = $true
    }
}

if ($needsBuild) {
    Write-Host "Building Docker image..."
    docker build -t $ImageName $ProjectDir
}

# Run the container
Write-Host "Starting $ContainerName..."
$dockerArgs = @(
    "run", "-d",
    "--name", $ContainerName,
    "-p", "${Port}:8000",
    "-v", "${VolumeName}:/app/db"
) + $EnvFlag + @($ImageName)

& docker @dockerArgs

Write-Host ""
Write-Host "FinAlly is running at http://localhost:$Port"
Write-Host "To stop: .\scripts\stop_windows.ps1"

# Open browser
Start-Process "http://localhost:$Port"
