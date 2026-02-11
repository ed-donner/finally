$ErrorActionPreference = "Stop"
$ContainerName = "finally"

docker stop $ContainerName 2>$null
if ($?) { Write-Host "FinAlly stopped." } else { Write-Host "FinAlly is not running." }
docker rm $ContainerName 2>$null
