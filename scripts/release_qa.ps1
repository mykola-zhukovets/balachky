<#
    Strict, portable frozen-release QA runner.  It is intentionally separate
    from dev\qa_gate.ps1 so a detached public checkout can invoke it directly.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RepoRoot,
    [Parameter(Mandatory)][string]$PythonPath,
    [Parameter(Mandatory)][switch]$Frozen,
    [Parameter(Mandatory)][switch]$StrictFrozen,
    [Parameter(Mandatory)][ValidatePattern("^[0-9a-fA-F]{40}$")][string]$ExpectedCommit
)

$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    Write-Host "STRICT FROZEN QA FAIL: $Message" -ForegroundColor Red
    exit 1
}

if (-not $Frozen -or -not $StrictFrozen) {
    Fail "-Frozen and -StrictFrozen are both required"
}
if (-not [System.IO.Path]::IsPathRooted($RepoRoot) -or -not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    Fail "RepoRoot must be an existing absolute directory: $RepoRoot"
}
if (-not [System.IO.Path]::IsPathRooted($PythonPath) -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    Fail "PythonPath must be an existing absolute file: $PythonPath"
}

$root = (Resolve-Path -LiteralPath $RepoRoot).Path
$python = (Resolve-Path -LiteralPath $PythonPath).Path
$head = (& git -C $root rev-parse HEAD 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or $head -notmatch "^[0-9a-fA-F]{40}$") {
    Fail "cannot resolve Git HEAD for RepoRoot"
}
if ($head -ine $ExpectedCommit) {
    Fail "Git HEAD $head does not equal ExpectedCommit $ExpectedCommit"
}

& $python -c "import sys"
if ($LASTEXITCODE -ne 0) { Fail "PythonPath cannot start Python" }
$pyright = Get-Command pyright -ErrorAction SilentlyContinue
if ($null -eq $pyright) { Fail "required tool pyright is not available" }
$pyrightOutput = & $pyright.Source --version 2>&1
if ($LASTEXITCODE -ne 0) { Fail "required tool pyright cannot run" }
if (($pyrightOutput -join "`n") -match "(?i)warning") { Fail "required tool pyright emitted a warning" }

$exe = Join-Path $root "dist\Balachky\Balachky.exe"
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    Fail "required frozen executable is missing: $exe"
}
$localAppData = $env:LOCALAPPDATA
if ([string]::IsNullOrWhiteSpace($localAppData)) { Fail "LOCALAPPDATA is required for frozen log input" }
$log = Join-Path $localAppData "Balachky\logs\balachky.log"
if (-not (Test-Path -LiteralPath $log -PathType Leaf)) {
    Fail "required frozen log input is missing: $log"
}
$auditPath = Join-Path $root "qa-reports\frozen-audit.json"
if (-not (Test-Path -LiteralPath $auditPath -PathType Leaf)) {
    Fail "required frozen audit input is missing: $auditPath"
}
try { $audit = Get-Content -LiteralPath $auditPath -Raw | ConvertFrom-Json }
catch { Fail "frozen audit input is not valid JSON: $auditPath" }
foreach ($property in @("commit", "status", "isolation", "warnings")) {
    if ($audit.PSObject.Properties.Name -notcontains $property) { Fail "frozen audit input is missing $property" }
}
if ($audit.commit -ine $ExpectedCommit) { Fail "frozen audit commit does not match ExpectedCommit" }
if ($audit.status -ine "passed") { Fail "frozen audit is not passed: $($audit.status)" }
if ($audit.isolation -ine "verified") { Fail "frozen audit isolation is unproven: $($audit.isolation)" }
if (@($audit.warnings).Count -ne 0) { Fail "frozen audit contains warning entries" }

$existing = @(Get-Process -Name "Balachky" -ErrorAction SilentlyContinue)
if ($existing.Count -ne 0) { Fail "second instance is already running; isolation cannot be proven" }

$proc = $null
try {
    $proc = Start-Process -FilePath $exe -PassThru
    Start-Sleep -Milliseconds 800
    if ($proc.HasExited) { Fail "frozen application exited early with code $($proc.ExitCode)" }
    $other = @(Get-Process -Name "Balachky" -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $proc.Id })
    if ($other.Count -ne 0) { Fail "second instance appeared during frozen run; isolation cannot be proven" }
    $newLog = Get-Content -LiteralPath $log -Raw
    if ($newLog -match "Traceback|ERROR|CRITICAL|WARNING") {
        Fail "frozen log contains warning or error markers"
    }
    Write-Host "STRICT FROZEN QA PASS" -ForegroundColor Green
}
finally {
    if ($null -ne $proc -and -not $proc.HasExited) {
        $proc.CloseMainWindow() | Out-Null
        if (-not $proc.WaitForExit(3000)) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    }
}
