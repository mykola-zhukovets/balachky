<#
    build.ps1 — реліз-складання «Балачки»: PyInstaller → обов'язковий аудит
    дистрибутива → Inno Setup → хеш у назві.

    Навіщо цей скрипт. Ранок 2026-07-30: два кроки виглядали виконаними, не
    бувши такими.
      1. Аудит дистрибутива (tests/test_distribution_audit.py,
         tests/test_license_distribution_contract.py) мовчки пропускається
         (pytest.skip), якщо не задано BALACHKY_AUDIT_DIST — набір лишався
         зеленим, хоча нічого не перевірив.
      2. Ланцюжок команд узяв код завершення з ОСТАННЬОЇ команди (перевірка
         наявності файлу), а не з PyInstaller, який або впав, або не
         запускався — інсталятор зібрався зі старої, девʼятигодинної дистри.

    Як цей скрипт унеможливлює обидва:
      - Крок PyInstaller: $LASTEXITCODE читається В ОДИН РЯДОК одразу після
        виклику, до будь-якої іншої команди — і перевіряється явно. Далі
        окремою перевіркою: головний .exe мусить бути НОВІШИЙ за момент
        старту цього прогону (Get-Date до виклику PyInstaller) — інакше
        стоп, навіть якщо exit code 0.
      - Крок аудиту: BALACHKY_AUDIT_DIST виставляється на щойно зібрану
        теку, pytest пише --junitxml, і скрипт падає не лише на червоному
        (exit code), а й коли junit каже skipped > 0 — пропущена перевірка
        тут прирівняна до провалу.
      - Жоден вивід не проходить через фільтр рядків (Select-String,
        Where-Object тощо) між командою і журналом: лише Tee-Object, який
        дублює все як є.

    Виклик (складання НЕ вмикати, доки власник не дав добро):
      pwsh -File installer\build.ps1
      pwsh -File installer\build.ps1 -Profile full
#>
[CmdletBinding()]
param(
    [ValidateSet("no-tts", "full")]
    [string]$Profile = "no-tts",

    [string]$BuildVenv = "D:\Projects\tts-build-venv",

    [string]$LogDir,

    [string]$InnoSetupExe
)

$ErrorActionPreference = "Stop"

# installer\build.ps1 → корінь репо на рівень вище.
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $LogDir) { $LogDir = Join-Path $Root "build-logs" }

function Fail([string]$Message) {
    Write-Host ""
    Write-Host "СТОП: $Message" -ForegroundColor Red
    exit 1
}

function Step([string]$Message) {
    Write-Host ""
    Write-Host "── $Message ──" -ForegroundColor Cyan
}

# ── 0. Преліт: усі шляхи мусять існувати ДО того, як щось запуститься ──────

Step "Преліт: перевірка шляхів"

$buildPython = Join-Path $BuildVenv "Scripts\python.exe"
$pyinstallerExe = Join-Path $BuildVenv "Scripts\pyinstaller.exe"
$specPath = Join-Path $Root "balachky.spec"
$issPath = Join-Path $Root "installer\balachky.iss"
$buildInfoPath = Join-Path $Root "whisper_core\_buildinfo.py"
$finalizeScript = Join-Path $Root "scripts\finalize_installer.ps1"
$auditDistPath = Join-Path $Root "dist\Balachky"
$exePath = Join-Path $auditDistPath "Balachky.exe"

if (-not $InnoSetupExe) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    )
    $InnoSetupExe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}

$requiredPaths = @(
    @{ Name = "Складальний python.exe"; Path = $buildPython },
    @{ Name = "pyinstaller.exe у складальному venv"; Path = $pyinstallerExe },
    @{ Name = "balachky.spec"; Path = $specPath },
    @{ Name = "installer\balachky.iss"; Path = $issPath },
    @{ Name = "scripts\finalize_installer.ps1"; Path = $finalizeScript },
    @{ Name = "ISCC.exe (Inno Setup 6)"; Path = $InnoSetupExe }
)
foreach ($item in $requiredPaths) {
    if (-not $item.Path -or -not (Test-Path $item.Path)) {
        Fail "Немає: $($item.Name) — очікувалось у '$($item.Path)'"
    }
    Write-Host "  ОК  $($item.Name): $($item.Path)"
}

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# ── 1. Робоче дерево чисте, зафіксувати коміт ───────────────────────────────

Step "Перевірка робочого дерева"

$gitStatus = git status --porcelain
if ($LASTEXITCODE -ne 0) { Fail "git status повернув помилку" }
if ($gitStatus) {
    Fail "Робоче дерево не чисте — закомітьте або відкладіть зміни перед складанням:`n$gitStatus"
}

$commit = (git rev-parse --short HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $commit) { Fail "Не вдалося визначити поточний коміт" }
Write-Host "  Коміт: $commit"

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$buildLog = Join-Path $LogDir "build-$Profile-$stamp-$commit.log"
"START $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') HEAD=$commit PROFILE=$Profile" |
    Out-File $buildLog -Encoding utf8

# Момент, ВІД якого рахуємо свіжість зібраного .exe (крок 3).
$buildStart = Get-Date

# ── 2-3. PyInstaller + перевірка свіжості ────────────────────────────────────
# Обгорнуто в try/finally: balachky.spec перезаписує _buildinfo.py ще ПІД ЧАС
# аналізу, тобто до можливого падіння. Без finally невдалий прогін лишав дерево
# брудним, і наступний запуск падав на кроці 1 — оператору доводилося чистити
# руками (знахідка судді 31.07).

try {
    Step "PyInstaller ($Profile)"

    $env:BALACHKY_BUILD_PROFILE = $Profile
    # python -m PyInstaller, НЕ pyinstaller.exe: exe-лаунчер venv несе вшитий
    # абсолютний шлях до python і тихо падає з кодом 1 після переїзду venv
    # (C:→D: 30.07; впіймано на білді 31.07 — порожній журнал, нуль виводу).
    & $buildPython -m PyInstaller $specPath --noconfirm *>&1 | Tee-Object -FilePath $buildLog -Append
    $pyinstallerExit = $LASTEXITCODE
    Remove-Item Env:\BALACHKY_BUILD_PROFILE -ErrorAction SilentlyContinue

    if ($pyinstallerExit -ne 0) {
        Fail "PyInstaller завершився з кодом $pyinstallerExit. Журнал: $buildLog"
    }

    if (-not (Select-String -Path $buildLog -Pattern "=== BALACHKY BUILD PROFILE: $Profile ===" -Quiet)) {
        Fail "У виводі PyInstaller немає підтвердження профілю '$Profile' — перевірте журнал: $buildLog"
    }

    Step "Перевірка свіжості зібраного .exe"

    if (-not (Test-Path $exePath)) {
        Fail "Немає $exePath після PyInstaller — збірка не відбулася. Журнал: $buildLog"
    }

    $exeTime = (Get-Item $exePath).LastWriteTime
    if ($exeTime -lt $buildStart) {
        Fail ("dist\Balachky\Balachky.exe старіший за початок цього запуску " +
              "($($exeTime.ToString('yyyy-MM-dd HH:mm:ss')) < $($buildStart.ToString('yyyy-MM-dd HH:mm:ss'))) " +
              "— PyInstaller вивів 'успіх', але файл не перезаписав. Журнал: $buildLog")
    }
    Write-Host "  ОК  $exePath оновлено о $($exeTime.ToString('yyyy-MM-dd HH:mm:ss'))"
}
finally {
    # Виконується і при успіху, і при Fail усередині try — дерево не лишається
    # брудним після невдалого складання.
    git checkout -- $buildInfoPath 2>$null
}

# ── 4. Переконатися, що _buildinfo.py справді в dev-стані ───────────────────

Step "Відкат whisper_core\_buildinfo.py"

$postCheckoutStatus = git status --porcelain -- $buildInfoPath
if ($postCheckoutStatus) {
    Fail "whisper_core\_buildinfo.py лишився зміненим після відкату — робоче дерево забрудниться"
}

# ── 5. ОБОВ'ЯЗКОВИЙ аудит дистрибутива ───────────────────────────────────────

Step "Аудит дистрибутива (BALACHKY_AUDIT_DIST)"

$env:BALACHKY_AUDIT_DIST = (Resolve-Path $auditDistPath).Path
$auditJunit = Join-Path $LogDir "audit-$stamp-$commit.xml"

& $buildPython -m pytest `
    tests/test_distribution_audit.py `
    tests/test_license_distribution_contract.py `
    -v --junitxml=$auditJunit *>&1 | Tee-Object -FilePath $buildLog -Append
$auditExit = $LASTEXITCODE
Remove-Item Env:\BALACHKY_AUDIT_DIST -ErrorAction SilentlyContinue

if ($auditExit -ne 0) {
    Fail "Аудит дистрибутива ЧЕРВОНИЙ (exit $auditExit). Журнал: $buildLog"
}

if (-not (Test-Path $auditJunit)) {
    Fail "pytest не створив $auditJunit — неможливо перевірити, чи щось було пропущено"
}

[xml]$auditResults = Get-Content $auditJunit
$suites = @($auditResults.testsuites.testsuite)
if (-not $suites -or $suites.Count -eq 0) { $suites = @($auditResults.testsuite) }
if (-not $suites -or $suites.Count -eq 0) { Fail "Не вдалося розібрати $auditJunit" }

$totalTests = 0
$totalSkipped = 0
foreach ($suite in $suites) {
    $totalTests += [int]$suite.tests
    $totalSkipped += [int]$suite.skipped
}

if ($totalTests -eq 0) {
    Fail "Аудит знайшов 0 тестів — селектори файлів застаріли"
}
if ($totalSkipped -gt 0) {
    Fail ("Аудит ПРОПУСТИВ $totalSkipped із $totalTests перевірок — " +
          "BALACHKY_AUDIT_DIST не подіяв, це прирівнюється до провалу аудиту. Журнал: $buildLog")
}
Write-Host "  ОК  $totalTests/$totalTests перевірок пройдено, 0 пропущено"

# ── 6. Inno Setup + хеш у назві ──────────────────────────────────────────────

Step "Компіляція інсталятора (Inno Setup)"

& $InnoSetupExe $issPath *>&1 | Tee-Object -FilePath $buildLog -Append
$isccExit = $LASTEXITCODE
if ($isccExit -ne 0) { Fail "ISCC.exe завершився з кодом $isccExit. Журнал: $buildLog" }

Step "Фіналізація інсталятора (SHA-256 у назві)"

$finalizeOutput = & pwsh -File $finalizeScript *>&1
$finalizeExit = $LASTEXITCODE
$finalizeOutput | Tee-Object -FilePath $buildLog -Append | Out-Null
if ($finalizeExit -ne 0) { Fail "finalize_installer.ps1 завершився з кодом $finalizeExit. Журнал: $buildLog" }

$installer = Get-ChildItem -Path (Join-Path $Root "installer\Output") -Filter "BalachkySetup-*.exe" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $installer) { Fail "Не знайдено готового інсталятора в installer\Output після фіналізації" }

"END $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') EXIT=0" | Add-Content $buildLog

# ── 7. Підсумок ───────────────────────────────────────────────────────────

$sizeMb = [math]::Round($installer.Length / 1MB, 1)
Write-Host ""
Write-Host "════════ ГОТОВО ════════" -ForegroundColor Green
Write-Host "Коміт:      $commit"
Write-Host "Профіль:    $Profile"
Write-Host "Інсталятор: $($installer.FullName) ($sizeMb МБ)"
Write-Host "Журнал:     $buildLog"
Write-Host "Аудит:      $auditJunit"
