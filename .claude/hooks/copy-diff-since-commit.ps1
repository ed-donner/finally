# Stop hook: copy every code change since the last commit into a review input file.
# Output: planning/review/since-last-commit.diff (overwritten on each Stop).
# Includes tracked changes (git diff HEAD) and the contents of untracked files.
# Does not stage, commit, or otherwise modify the git index.

# Native git stderr (line-ending warnings) must not abort the script.
$ErrorActionPreference = 'Continue'

$root = $env:CLAUDE_PROJECT_DIR
if (-not $root) { $root = (git rev-parse --show-toplevel).Trim() }
Set-Location -LiteralPath $root

$outDir = Join-Path $root 'planning/review'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$out = Join-Path $outDir 'since-last-commit.diff'

$head    = (git rev-parse --short HEAD).Trim()
$subject = (git log -1 --format=%s).Trim()

# Untracked files, excluding the review output directory itself.
$untracked = @(git ls-files --others --exclude-standard | Where-Object { $_ -and $_ -notlike 'planning/review/*' })

$header = New-Object System.Collections.Generic.List[string]
$header.Add("# Changes since last commit $head ($subject)")
$header.Add("# Generated $(Get-Date -Format o)")
if ($untracked.Count -gt 0) {
    $header.Add('# Untracked files (shown as new-file diffs below):')
    foreach ($f in $untracked) { $header.Add("#   $f") }
}
$header.Add('')

$body = (git diff HEAD --no-color | Out-String)
foreach ($f in $untracked) {
    # --no-index exits 1 when files differ; that is expected, so ignore the exit code.
    $body += (git diff --no-index --no-color -- /dev/null $f | Out-String)
}

$text = ($header -join "`n") + "`n" + $body
[System.IO.File]::WriteAllText($out, $text, (New-Object System.Text.UTF8Encoding($false)))
