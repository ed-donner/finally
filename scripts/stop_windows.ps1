$ErrorActionPreference = "Stop"
$ContainerName = "finally"

# Use docker if available, otherwise fall back to podman
if (Get-Command docker -ErrorAction SilentlyContinue) {
    $Engine = "docker"
} elseif (Get-Command podman -ErrorAction SilentlyContinue) {
    $Engine = "podman"
} else {
    Write-Error "Neither docker nor podman found on PATH."
    exit 1
}

& $Engine stop $ContainerName 2>$null
if ($LASTEXITCODE -eq 0) { Write-Host "FinAlly stopped." } else { Write-Host "FinAlly is not running." }
& $Engine rm $ContainerName 2>$null
