# Завершення збірки інсталятора: контрольна сума в назві файла + файл перевірки.
#
# Навіщо. Inno Setup дає просто BalachkySetup-<версія>.exe, а публікуємо ми
# BalachkySetup-<версія>-<перші 8 символів контрольної суми>.exe — щоб файл було
# видно, який саме, і щоб людина могла звірити його не читаючи опису релізу.
# Досі це робилося руками й ніде не було записано кроком: суд перед публікацією
# 25.07 знайшов, що документи обіцяють назву з контрольною сумою, а жоден скрипт
# її не робить. Ручний крок перед публікацією — це крок, який колись забудуть.
#
# Що робить:
#   1. бере свіжий інсталятор із installer\Output;
#   2. рахує SHA-256;
#   3. перейменовує у канонічну назву з першими 8 символами суми;
#   4. кладе поруч <назва>.sha256 у форматі, який розуміє certutil і sha256sum;
#   5. друкує готові рядки для опису релізу й для дорожньої карти.
#
# Виклик:  powershell -File scripts\finalize_installer.ps1
#          powershell -File scripts\finalize_installer.ps1 -Suffix "beta"

param(
    [string]$Suffix = "beta",
    [string]$OutputDir = "installer\Output"
)

$ErrorActionPreference = "Stop"

$candidates = Get-ChildItem -Path $OutputDir -Filter "BalachkySetup-*.exe" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending

if (-not $candidates) {
    Write-Error "У $OutputDir немає жодного BalachkySetup-*.exe. Спершу зберіть інсталятор через ISCC."
}

$exe = $candidates[0]

# Уже перейменований (у назві є суфікс і сума) — не чіпаємо, лише звітуємо.
if ($exe.BaseName -match '-[0-9A-F]{8}$') {
    Write-Host "Інсталятор уже має контрольну суму в назві: $($exe.Name)"
    $hash = (Get-FileHash -Algorithm SHA256 -Path $exe.FullName).Hash
    Write-Host "SHA-256: $hash"
    return
}

$hash = (Get-FileHash -Algorithm SHA256 -Path $exe.FullName).Hash
$short = $hash.Substring(0, 8)

# BalachkySetup-1.2.3.exe → BalachkySetup-1.2.3-beta-999594BC.exe
$version = $exe.BaseName -replace '^BalachkySetup-', ''
$parts = @("BalachkySetup", $version)
if ($Suffix) { $parts += $Suffix }
$parts += $short
$newName = ($parts -join '-') + '.exe'
$newPath = Join-Path $exe.DirectoryName $newName

if (Test-Path $newPath) {
    Write-Error "Файл $newName уже існує. Приберіть його або перевірте, чи це не той самий збір."
}

Rename-Item -Path $exe.FullName -NewName $newName

# Файл перевірки поруч: той самий формат, що розуміють certutil і sha256sum.
$sumFile = "$newPath.sha256"
"$($hash.ToLower())  $newName" | Set-Content -Path $sumFile -Encoding ASCII

$sizeMb = [math]::Round((Get-Item $newPath).Length / 1MB, 1)

Write-Host ""
Write-Host "Готово. Інсталятор: $newName"
Write-Host "Розмір: $sizeMb МБ"
Write-Host "SHA-256: $hash"
Write-Host ""
Write-Host "--- рядок для опису релізу й дорожньої карти ---"
Write-Host "``$newName`` ($sizeMb МБ), SHA-256: ``$hash``"
Write-Host ""
Write-Host "--- як користувач перевіряє файл ---"
Write-Host "certutil -hashfile $newName SHA256"
