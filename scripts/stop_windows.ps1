$ErrorActionPreference = "Stop"

$ContainerName = "finally"

$running = docker ps -q -f "name=$ContainerName" 2>$null
if ($running) {
    Write-Host "Stopping $ContainerName..."
    docker stop $ContainerName 2>$null | Out-Null
    docker rm $ContainerName 2>$null | Out-Null
    Write-Host "Stopped."
} else {
    $stopped = docker ps -aq -f "name=$ContainerName" 2>$null
    if ($stopped) {
        Write-Host "Removing stopped $ContainerName container..."
        docker rm $ContainerName 2>$null | Out-Null
        Write-Host "Removed."
    } else {
        Write-Host "No $ContainerName container found."
    }
}
