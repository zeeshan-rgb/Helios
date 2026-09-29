# Run Helios's test suite from the repo root, scoped to tests/ only.
# Running pytest from the wrong directory makes it recursively collect the whole tree (including
# the .venv, where a pywin32 COM test segfaults on import) — so always point it at tests/.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot      # ...\helios
$py = Join-Path $root ".venv\Scripts\python.exe"
& $py -m pytest (Join-Path $root "tests") -q -p no:cacheprovider
exit $LASTEXITCODE
