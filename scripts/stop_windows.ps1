$ErrorActionPreference = "Stop"

$ContainerName = "finally"

$running = docker ps -q -f "name=$ContainerName"
if ($running) {
    Write-Host "Stopping FinAlly..."
    docker stop $ContainerName | Out-Null
    docker rm $ContainerName | Out-Null
    Write-Host "FinAlly stopped."
} else {
    $stopped = docker ps -aq -f "name=$ContainerName"
    if ($stopped) {
        Write-Host "Removing stopped container..."
        docker rm $ContainerName | Out-Null
        Write-Host "Done."
    } else {
        Write-Host "FinAlly is not running."
    }
}
