# FinAlly stop script for Windows PowerShell
$ContainerName = "finally"

docker stop $ContainerName 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "FinAlly stopped." -ForegroundColor Green
} else {
    Write-Host "FinAlly was not running." -ForegroundColor Yellow
}
docker rm $ContainerName 2>$null | Out-Null
