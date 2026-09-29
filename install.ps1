<#
  Helios one-line installer (Windows).

    irm https://raw.githubusercontent.com/NotTimPunt/jarvis/cua-driver/install.ps1 | iex

  Installs Python (if missing), downloads Helios, builds the venv + dependencies, and adds the
  `helios` command to your PATH. Then run:  helios onboard

  Optional overrides (set BEFORE piping to iex):
    $env:HELIOS_DIR    = "D:\apps\Helios"   # install location (default: %LOCALAPPDATA%\Helios)
    $env:HELIOS_BRANCH = "cua-driver"       # branch to fetch
#>

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # makes Invoke-WebRequest fast

function Info($m) { Write-Host "  $m" -ForegroundColor Cyan }
function Good($m) { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!] $m" -ForegroundColor Yellow }

Write-Host "`n=== Helios installer ===`n" -ForegroundColor Cyan

$repo   = "NotTimPunt/jarvis"
$branch = if ($env:HELIOS_BRANCH) { $env:HELIOS_BRANCH } else { "cua-driver" }
$dir    = if ($env:HELIOS_DIR)    { $env:HELIOS_DIR }    else { Join-Path $env:LOCALAPPDATA "Helios" }

# ---------- 1. Python 3.10+ ----------
function Find-Python {
  foreach ($c in @("python", "python3")) {
    $g = Get-Command $c -ErrorAction SilentlyContinue
    if ($g) {
      try {
        $v = & $g.Source -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($v -and [version]$v -ge [version]"3.10") { return $g.Source }
      } catch {}
    }
  }
  $cand = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
          Sort-Object FullName -Descending | Select-Object -First 1
  if ($cand) { return $cand.FullName }
  return $null
}

$py = Find-Python
if (-not $py) {
  Warn "Python 3.10+ not found."
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Info "Installing Python 3.12 via winget (this can take a minute)..."
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements --silent | Out-Null
    $py = Find-Python
  }
  if (-not $py) {
    Warn "Couldn't install Python automatically."
    Warn "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then re-run this installer."
    return
  }
}
Good ("Python: $py  (" + (& $py --version) + ")")

# ---------- 2. Download Helios ----------
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$zip = Join-Path $env:TEMP "jarvis-$branch.zip"
$ext = Join-Path $env:TEMP ("helios-extract-" + [guid]::NewGuid().ToString("N"))
Info "Downloading Helios ($branch)..."
Invoke-WebRequest "https://github.com/$repo/archive/refs/heads/$branch.zip" -OutFile $zip
Info "Extracting..."
Expand-Archive -Path $zip -DestinationPath $ext -Force
$src = Join-Path $ext "jarvis-$branch"
# On a re-run, back up the user's onboarded settings.toml first. robocopy will land the fresh
# committed default (so new keys/comments/defaults from this update arrive); after deps install
# we merge the user's saved values back on top so their config is preserved (see step 3b).
# secrets.toml / mcp.json / claude_settings.json are gitignored (not in the zip) so robocopy
# leaves them untouched already.
$userSettings   = Join-Path $dir "config\settings.toml"
$settingsBackup = $null
if (Test-Path $userSettings) {
  $settingsBackup = Join-Path $env:TEMP ("helios-settings-" + [guid]::NewGuid().ToString("N") + ".toml")
  Copy-Item -LiteralPath $userSettings -Destination $settingsBackup -Force
}
# Copy code in. /E = all subdirs; preserve an existing .venv + data on re-run (update in place).
robocopy $src $dir /E /XD ".venv" "data" /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
Remove-Item $zip, $ext -Recurse -Force -ErrorAction SilentlyContinue
Good "Code in $dir"

# ---------- 3. venv + dependencies ----------
$venvPy = Join-Path $dir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  Info "Creating virtual environment..."
  & $py -m venv (Join-Path $dir ".venv")
}
Info "Installing dependencies (a few minutes)..."
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $dir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Warn "Dependency install hit an error above - you can re-run the installer." }
else { Good "Dependencies installed." }

# ---------- 3b. preserve user config across re-runs ----------
# The fresh default settings.toml is now on disk; overlay the user's saved values back onto it
# (their choices win, brand-new keys keep the shipped default). On any merge error, restore the
# user's previous file verbatim so an update can never lose or corrupt an onboarded config.
if ($settingsBackup -and (Test-Path $settingsBackup)) {
  $merged = $false
  if (Test-Path $venvPy) {
    Info "Preserving your existing settings..."
    & $venvPy (Join-Path $dir "tools\merge_settings.py") --base $userSettings --user $settingsBackup --out $userSettings
    if ($LASTEXITCODE -eq 0) { $merged = $true }
  }
  if ($merged) {
    Good "Settings preserved."
  } else {
    # No venv to merge with, or the merge failed: restore the user's previous settings.toml
    # verbatim so the just-copied default never replaces their onboarded config.
    Warn "Couldn't merge new defaults - keeping your previous settings.toml unchanged."
    Copy-Item -LiteralPath $settingsBackup -Destination $userSettings -Force
  }
  Remove-Item -LiteralPath $settingsBackup -Force -ErrorAction SilentlyContinue
}

# ---------- 4. install the `helios` command ----------
$bin = Join-Path $dir "bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$shim = '@echo off' + "`r`n" + '"' + $venvPy + '" "' + $dir + '\helios_cli.py" %*' + "`r`n"
Set-Content -LiteralPath (Join-Path $bin "helios.cmd") -Value $shim -Encoding ASCII -NoNewline
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ';') -notcontains $bin) {
  [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(';') + ";" + $bin), "User")
  Good "Added 'helios' to your PATH."
}
if (($env:Path -split ';') -notcontains $bin) { $env:Path = $env:Path.TrimEnd(';') + ";" + $bin }

# ---------- done ----------
Write-Host "`n=== Installed ===" -ForegroundColor Green
Write-Host "Next, run " -NoNewline
Write-Host "helios onboard" -ForegroundColor Cyan -NoNewline
Write-Host " to connect a brain and set things up."
Write-Host "(If 'helios' isn't recognized, open a NEW terminal first.)`n"
