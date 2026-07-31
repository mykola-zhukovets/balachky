<#
.SYNOPSIS
Runs the install/upgrade matrix only in a disposable Windows environment.

.DESCRIPTION
The automated path expects two real Inno Setup packages: N and N+1. It performs
silent install, same-version reinstall, upgrade, and silent uninstall while
checking the installed files, the per-user uninstall key, and a data marker.

Interactive keep/delete-data scenarios never launch the uninstaller. A person
makes the choice in the uninstaller UI, then runs the matching verify scenario.
#>

[CmdletBinding()]
param(
    [switch]$IAmInSandbox,
    [switch]$IAmInDisposableVm,
    [ValidateSet(
        "all-automated",
        "fresh-install",
        "same-version-reinstall",
        "upgrade",
        "silent-uninstall-keep-data",
        "verify-interactive-keep-data",
        "verify-interactive-delete-data"
    )]
    [string[]]$Scenario = @("all-automated"),
    [string]$OldInstaller,
    [string]$NewInstaller,
    [string]$ExpectedMarkerHash = ""
)

$isWindowsSandbox = (
    $IAmInSandbox.IsPresent -and
    $env:USERNAME -eq "WDAGUtilityAccount"
)
$isApprovedDisposableVm = (
    $IAmInDisposableVm.IsPresent -and
    $env:BALACHKY_INSTALL_TEST_VM -eq "YES"
)
if (-not ($isWindowsSandbox -or $isApprovedDisposableVm)) {
    [Console]::Error.WriteLine(
        "Відмова: цей скрипт встановлює й видаляє програму. " +
        "Запустіть його у Windows Sandbox з -IAmInSandbox або в одноразовій " +
        "VM з -IAmInDisposableVm і BALACHKY_INSTALL_TEST_VM=YES."
    )
    exit 64
}
# SAFETY_GATE_PASSED

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\Balachky"
$DataDir = Join-Path $env:LOCALAPPDATA "Balachky"
$AppExe = Join-Path $InstallDir "Balachky.exe"
$Uninstaller = Join-Path $InstallDir "unins000.exe"
$MarkerPath = Join-Path $DataDir "install-matrix.marker"
$UninstallRegistryPath = (
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\" +
    "{2C5BBCE3-5047-47A6-96B0-C78B12E059F9}_is1"
)

# SCENARIO_IDS_BEGIN
$ScenarioIds = @(
    "fresh-install",
    "same-version-reinstall",
    "upgrade",
    "silent-uninstall-keep-data",
    "verify-interactive-keep-data",
    "verify-interactive-delete-data"
)
# SCENARIO_IDS_END

$AutomatedScenarioIds = $ScenarioIds[0..3]
$Results = New-Object "System.Collections.Generic.List[object]"

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Assert-ProgramAbsent {
    Assert-True (-not (Test-Path -LiteralPath $InstallDir)) (
        "Папка програми досі існує: $InstallDir"
    )
    Assert-True (-not (Test-Path -LiteralPath $UninstallRegistryPath)) (
        "Uninstall-ключ досі існує: $UninstallRegistryPath"
    )
}

function Assert-FreshState {
    Assert-ProgramAbsent
    Assert-True (-not (Test-Path -LiteralPath $DataDir)) (
        "Для чистого встановлення папки даних не повинно бути: $DataDir"
    )
}

function Assert-Installed {
    Assert-True (Test-Path -LiteralPath $InstallDir -PathType Container) (
        "Немає папки програми: $InstallDir"
    )
    Assert-True (Test-Path -LiteralPath $AppExe -PathType Leaf) (
        "Немає виконуваного файла: $AppExe"
    )
    Assert-True (Test-Path -LiteralPath $Uninstaller -PathType Leaf) (
        "Немає деінсталятора: $Uninstaller"
    )
    Assert-True (Test-Path -LiteralPath $UninstallRegistryPath) (
        "Немає per-user uninstall-ключа: $UninstallRegistryPath"
    )
}

function Resolve-Installer {
    param(
        [string]$Path,
        [string]$Role
    )

    Assert-True (-not [string]::IsNullOrWhiteSpace($Path)) (
        "Не задано $Role інсталятор."
    )
    Assert-True (Test-Path -LiteralPath $Path -PathType Leaf) (
        "Не знайдено $Role інсталятор: $Path"
    )
    return (Resolve-Path -LiteralPath $Path).Path
}

function Invoke-InnoProcess {
    param(
        [string]$FilePath,
        [string]$Operation
    )

    $safeName = $Operation -replace "[^a-zA-Z0-9-]", "-"
    $logPath = Join-Path $env:TEMP (
        "balachky-post86-{0}-{1}.log" -f $safeName, [guid]::NewGuid()
    )
    $arguments = @(
        "/SP-",
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/LOG=`"$logPath`""
    )

    Write-Host "  Очікуваний код виходу: 0"
    Write-Host "  Журнал Inno: $logPath"
    $process = Start-Process -FilePath $FilePath -ArgumentList $arguments `
        -Wait -PassThru
    Write-Host "  Фактичний код виходу: $($process.ExitCode)"
    Assert-True ($process.ExitCode -eq 0) (
        "$Operation завершився з кодом $($process.ExitCode), очікувався 0."
    )
}

function Get-InstalledDisplayVersion {
    $values = Get-ItemProperty -LiteralPath $UninstallRegistryPath
    Assert-True (-not [string]::IsNullOrWhiteSpace($values.DisplayVersion)) (
        "У uninstall-ключі немає DisplayVersion."
    )
    return [string]$values.DisplayVersion
}

function New-DataMarker {
    Assert-True (-not (Test-Path -LiteralPath $MarkerPath)) (
        "Контрольний файл уже існує: $MarkerPath"
    )
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
    $content = "install-matrix|{0}|{1:o}" -f [guid]::NewGuid(), [DateTime]::UtcNow
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($MarkerPath, $content, $encoding)
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $MarkerPath).Hash
    Write-Host "  SHA-256 контрольного файла: $hash"
    return $hash
}

function Get-MarkerHash {
    Assert-True (Test-Path -LiteralPath $MarkerPath -PathType Leaf) (
        "Немає контрольного файла даних: $MarkerPath"
    )
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $MarkerPath).Hash
}

function Assert-MarkerHash {
    param([string]$Before)

    $after = Get-MarkerHash
    Assert-True ($after -eq $Before) (
        "Контрольний файл даних змінився: було $Before, стало $after."
    )
}

function Invoke-Scenario {
    param(
        [string]$Name,
        [scriptblock]$Body
    )

    Write-Host ""
    Write-Host "=== $Name ==="
    try {
        & $Body
        $Results.Add([pscustomobject]@{
            Scenario = $Name
            Result = "PASS"
            Detail = ""
        }) | Out-Null
        Write-Host "PASS $Name"
        return $true
    }
    catch {
        $Results.Add([pscustomobject]@{
            Scenario = $Name
            Result = "FAIL"
            Detail = $_.Exception.Message
        }) | Out-Null
        Write-Host "FAIL $Name — $($_.Exception.Message)"
        return $false
    }
}

$OldInstallerPath = $null
$NewInstallerPath = $null
if (
    $Scenario -contains "all-automated" -or
    $Scenario -contains "fresh-install" -or
    $Scenario -contains "same-version-reinstall"
) {
    $OldInstallerPath = Resolve-Installer $OldInstaller "N"
}
if (
    $Scenario -contains "all-automated" -or
    $Scenario -contains "upgrade"
) {
    $NewInstallerPath = Resolve-Installer $NewInstaller "N+1"
}

if ($Scenario -contains "all-automated") {
    Assert-True ($Scenario.Count -eq 1) (
        "all-automated не можна поєднувати з окремими сценаріями."
    )
    $SelectedScenarioIds = $AutomatedScenarioIds
}
else {
    $SelectedScenarioIds = $Scenario
}

$Bodies = @{
    "fresh-install" = {
        Assert-FreshState
        Invoke-InnoProcess $OldInstallerPath "fresh-install"
        Assert-Installed
        $null = Get-InstalledDisplayVersion
        $null = New-DataMarker
    }
    "same-version-reinstall" = {
        Assert-Installed
        $beforeHash = Get-MarkerHash
        $beforeVersion = Get-InstalledDisplayVersion
        Invoke-InnoProcess $OldInstallerPath "same-version-reinstall"
        Assert-Installed
        Assert-MarkerHash $beforeHash
        Assert-True (
            (Get-InstalledDisplayVersion) -eq $beforeVersion
        ) "Повторне встановлення змінило DisplayVersion."
    }
    "upgrade" = {
        Assert-Installed
        $beforeHash = Get-MarkerHash
        $beforeVersion = Get-InstalledDisplayVersion
        Invoke-InnoProcess $NewInstallerPath "upgrade"
        Assert-Installed
        Assert-MarkerHash $beforeHash
        $afterVersion = Get-InstalledDisplayVersion
        Assert-True ($afterVersion -ne $beforeVersion) (
            "Після N→N+1 DisplayVersion не змінився: $afterVersion"
        )
    }
    "silent-uninstall-keep-data" = {
        Assert-Installed
        $beforeHash = Get-MarkerHash
        Invoke-InnoProcess $Uninstaller "silent-uninstall-keep-data"
        Assert-ProgramAbsent
        Assert-MarkerHash $beforeHash
    }
    "verify-interactive-keep-data" = {
        Assert-ProgramAbsent
        Assert-True (-not [string]::IsNullOrWhiteSpace($ExpectedMarkerHash)) (
            "Для перевірки збережених даних задайте -ExpectedMarkerHash " +
            "зі значенням, надрукованим під час fresh-install."
        )
        $actualHash = Get-MarkerHash
        Assert-True ($actualHash -eq $ExpectedMarkerHash) (
            "Контрольний файл змінився: очікувався $ExpectedMarkerHash, " +
            "отримано $actualHash."
        )
    }
    "verify-interactive-delete-data" = {
        Assert-ProgramAbsent
        Assert-True (-not (Test-Path -LiteralPath $DataDir)) (
            "Після вибору видалення даних папка досі існує: $DataDir"
        )
    }
}

foreach ($scenarioId in $SelectedScenarioIds) {
    $passed = Invoke-Scenario $scenarioId $Bodies[$scenarioId]
    if (-not $passed) {
        break
    }
}

Write-Host ""
Write-Host "=== ПІДСУМОК ==="
foreach ($result in $Results) {
    if ($result.Detail) {
        Write-Host "$($result.Result) $($result.Scenario): $($result.Detail)"
    }
    else {
        Write-Host "$($result.Result) $($result.Scenario)"
    }
}

$failed = @($Results | Where-Object { $_.Result -eq "FAIL" }).Count
if ($failed -gt 0) {
    Write-Host "FAIL: $failed сценарій(ї) не пройдено."
    exit 1
}

Write-Host "PASS: $($Results.Count) сценарій(ї) пройдено."
exit 0
