$ContainerName = "finally-app"

Write-Host "Stopping FinAlly..."
docker rm -f $ContainerName 2>$null
Write-Host "Stopped. Data volume preserved."
