param(
  [switch]$Build,
  [switch]$RealLlm
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$TestComposeFile = Join-Path $RootDir "test/docker-compose.test.yml"

if ($RealLlm) {
  $env:TEST_LLM_MOCK = "false"
  $env:REAL_LLM_E2E = "true"
  $env:PLAYWRIGHT_TEST_ARGS = "--grep @real-llm --workers=1 --retries=0"
}
else {
  $env:TEST_LLM_MOCK = "true"
  $env:REAL_LLM_E2E = "false"
  $env:PLAYWRIGHT_TEST_ARGS = "--grep-invert @real-llm"
}

docker compose -f $TestComposeFile build

docker compose -f $TestComposeFile run --rm backend-tests

docker compose -f $TestComposeFile up -d app

try {
  docker compose -f $TestComposeFile run --rm playwright
  Write-Host "Containerized backend + Playwright tests completed."
}
finally {
  docker compose -f $TestComposeFile down -v
}
