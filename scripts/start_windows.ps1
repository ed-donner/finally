<#
.SYNOPSIS
    Start FinAlly in Docker (Windows PowerShell).

.DESCRIPTION
    Builds the image if needed, runs the container with the database volume and .env, waits until
    the app answers, opens http://localhost:8000 and prints instructions.
    Safe to run repeatedly: if FinAlly is already running it just prints the URL.
    The database lives in the Docker volume "finally-data" and survives stop/start.

.PARAMETER Build
    Rebuild the image, then (re)start the container.

.PARAMETER NoOpen
    Do not open the browser.

.EXAMPLE
    .\scripts\start_windows.ps1
    .\scripts\start_windows.ps1 -Build
#>
param(
    [switch]$Build,
    [switch]$NoOpen
)

# Native docker commands write progress to stderr; check $LASTEXITCODE explicitly instead of
# letting PowerShell turn that output into terminating errors.
$ErrorActionPreference = 'Continue'

$Image = 'finally'
$Container = 'finally'
$Volume = 'finally-data'
$Url = 'http://localhost:8000'

function Fail([string]$Message) {
    Write-Host "Error: $Message" -ForegroundColor Red
    exit 1
}

# Run docker quietly and report only success/failure.
function Test-Docker {
    & docker @args *> $null
    return ($LASTEXITCODE -eq 0)
}

function Test-ContainerRunning {
    $state = & docker container inspect -f '{{.State.Running}}' $Container 2>$null
    return ($LASTEXITCODE -eq 0 -and $state -eq 'true')
}

# Work from the project root no matter where the script is called from.
Set-Location (Join-Path $PSScriptRoot '..')

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail 'Docker is not installed. Get Docker Desktop from https://docs.docker.com/get-docker/'
}
if (-not (Test-Docker info)) {
    Fail 'Docker is installed but not running. Start Docker Desktop and try again.'
}

# --- 1. Build the image if it is missing (or if -Build was passed) ----------------------------
$rebuilt = $false
if ($Build -or -not (Test-Docker image inspect $Image)) {
    Write-Host 'Building the FinAlly image (the first build takes a few minutes)...'
    & docker build -t $Image .
    if ($LASTEXITCODE -ne 0) { Fail 'Image build failed (see output above).' }
    $rebuilt = $true
}

# --- 2. Start the container ----------------------------------------------------------------------
if ((Test-ContainerRunning) -and -not $rebuilt) {
    Write-Host 'FinAlly is already running.'
}
else {
    # Replace any old container (stopped, or running an older image). The volume is untouched.
    if (Test-Docker container inspect $Container) {
        & docker rm -f $Container *> $null
    }

    $runArgs = @('run', '-d', '--name', $Container, '--restart', 'unless-stopped',
        '-p', '8000:8000', '-v', "${Volume}:/app/db")
    if (Test-Path .env) {
        $runArgs += @('--env-file', '.env')
    }
    else {
        Write-Host "Warning: no .env file found in $(Get-Location)." -ForegroundColor Yellow
        Write-Host '         The app will run, but AI chat needs OPENROUTER_API_KEY. Create .env and re-run.' -ForegroundColor Yellow
    }
    $runArgs += $Image

    Write-Host 'Starting the container...'
    & docker @runArgs *> $null
    if ($LASTEXITCODE -ne 0) {
        Fail 'Could not start the container. Is port 8000 already in use by another program?'
    }
}

# --- 3. Wait until the app answers -------------------------------------------------------------
Write-Host -NoNewline 'Waiting for FinAlly to be ready'
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $null = Invoke-WebRequest -Uri "$Url/api/health" -UseBasicParsing -TimeoutSec 2
        $ready = $true
        break
    }
    catch {
        if (-not (Test-ContainerRunning)) {
            Write-Host ''
            & docker logs --tail 20 $Container
            Fail 'The container stopped during startup (log above).'
        }
        Write-Host -NoNewline '.'
        Start-Sleep -Seconds 1
    }
}
Write-Host ''
if (-not $ready) { Fail "FinAlly did not respond within 60 seconds. Check: docker logs $Container" }

# --- 4. Open the browser, print instructions ---------------------------------------------------
if (-not $NoOpen) {
    try { Start-Process $Url }
    catch { Write-Host '(Could not open a browser automatically; open the URL yourself.)' }
}

Write-Host @"

  FinAlly is running:  $Url

  View logs :  docker logs -f $Container
  Stop      :  .\scripts\stop_windows.ps1   (your data is kept in the "$Volume" volume)
  Rebuild   :  .\scripts\start_windows.ps1 -Build
  Changed .env?  Run stop_windows.ps1, then start_windows.ps1 (keys are read when the container starts).
  Reset data:    .\scripts\stop_windows.ps1; docker volume rm $Volume

"@
