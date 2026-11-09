# Установка оснастки: один вызов harness\install.py --apply, код выхода сохраняется.
# План без записи: python harness\install.py
$python = if (Get-Command python -ErrorAction SilentlyContinue) { 'python' } else { 'python3' }
& $python -X utf8 -B (Join-Path $PSScriptRoot 'harness\install.py') --apply @args
exit $LASTEXITCODE
