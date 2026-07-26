# Балачки — launch (run WITHOUT admin rights)
# A normal process can paste text into any window.
Set-Location $PSScriptRoot
$env:PYTHONPATH = $PSScriptRoot

# Optional: point to your own model cache dir.
# If unset, the standard HuggingFace cache (~/.cache/huggingface) is used.
# $env:WHISPER_TYPER_MODELS = "D:\models\whisper"

# venv якщо є, інакше системний python
$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
& $py -m fronts.desktop
Read-Host "Press Enter to exit"
