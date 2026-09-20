<#
.SYNOPSIS
    Stop FinAlly (Windows PowerShell).

.DESCRIPTION
    Stops and removes the container. The database volume "finally-data" is NOT touched, so your
    portfolio, trades and chat history are still there next time you start.
    Safe to run repeatedly: if nothing is running it says so and exits successfully.
#>

# Check $LASTEXITCODE explicitly instead of letting docker's stderr output become errors.
$ErrorActionPreference = 'Continue'

$Container = 'finally'
$Volume = 'finally-data'

function Test-Docker {
    & docker @args *> $null
    return ($LASTEXITCODE -eq 0)
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host 'Error: Docker is not installed.' -ForegroundColor Red
    exit 1
}
if (-not (Test-Docker info)) {
    Write-Host 'Error: Docker is not running, so there is nothing to stop.' -ForegroundColor Red
    exit 1
}

if (Test-Docker container inspect $Container) {
    Write-Host 'Stopping FinAlly...'
    & docker stop $Container *> $null   # graceful: lets the app finish writing to the database
    & docker rm $Container *> $null
    Write-Host "Stopped. Your data is kept in the `"$Volume`" volume."
}
else {
    Write-Host 'FinAlly is not running. Nothing to do.'
}

Write-Host 'Start again with .\scripts\start_windows.ps1'
