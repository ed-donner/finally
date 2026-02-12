param(
  [switch]$Build,
  [switch]$Open
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ComposeFile = Join-Path $RootDir "docker-compose.yml"

if ($Build) {
  docker compose -f $ComposeFile up -d --build
} else {
  docker compose -f $ComposeFile up -d
}

Write-Host "FinAlly is starting on http://localhost:8003"
docker compose -f $ComposeFile ps

if ($Open) {
  Start-Process "http://localhost:8003"
}
