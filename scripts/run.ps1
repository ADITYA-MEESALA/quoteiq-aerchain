$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$quotePython = Join-Path $PWD '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $quotePython)) {
    throw 'Create .venv with Python 3.12 and install requirements-lock.txt first. See README.md.'
}
& $quotePython -m streamlit run streamlit_app.py --server.address=127.0.0.1
