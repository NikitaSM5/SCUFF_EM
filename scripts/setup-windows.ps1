param([int]$Jobs = 4)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$toolsDir = Join-Path $root '.tools'
$msysDir = Join-Path $toolsDir 'msys64'
$bash = Join-Path $msysDir 'usr\bin\bash.exe'
New-Item -ItemType Directory -Force $toolsDir, (Join-Path $root 'build/logs') | Out-Null
Start-Transcript -Path (Join-Path $root 'build/logs/setup-windows-bootstrap.log') -Append | Out-Null
if (-not (Test-Path -LiteralPath $bash)) {
    $release = Invoke-RestMethod 'https://api.github.com/repos/msys2/msys2-installer/releases/latest'
    $asset = $release.assets | Where-Object { $_.name -eq 'msys2-base-x86_64-latest.tar.xz' }
    if (-not $asset -or -not $asset.digest.StartsWith('sha256:')) { throw 'Missing MSYS2 archive digest' }
    $archive = Join-Path $toolsDir $asset.name
    & curl.exe --fail --location --retry 3 --output $archive $asset.browser_download_url
    if ($LASTEXITCODE -ne 0) { throw 'MSYS2 download failed' }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $asset.digest.Substring(7)) {
        throw 'MSYS2 archive SHA256 mismatch'
    }
    $asset | ConvertTo-Json | Set-Content -Encoding UTF8 'build/logs/msys2-download.json'
    & tar.exe -xf $archive -C $toolsDir
    if ($LASTEXITCODE -ne 0) { throw 'MSYS2 extraction failed' }
}
$env:MSYSTEM = 'UCRT64'
$env:CHERE_INVOKING = '1'
$env:JOBS = "$Jobs"
if (-not (Test-Path (Join-Path $toolsDir 'windows-dependencies-ready'))) {
    & $bash -lc 'pacman --noconfirm -Syu'
    if ($LASTEXITCODE -ne 0) { throw 'MSYS2 core update completed or failed; rerun setup-windows.ps1 in a fresh process' }
    & $bash -lc 'pacman --noconfirm -Su'
    if ($LASTEXITCODE -ne 0) { throw 'MSYS2 package update failed' }
    & $bash -lc 'pacman --noconfirm -S --needed make autoconf automake libtool git mingw-w64-ucrt-x86_64-gcc mingw-w64-ucrt-x86_64-gcc-fortran mingw-w64-ucrt-x86_64-openblas mingw-w64-ucrt-x86_64-pkgconf'
    if ($LASTEXITCODE -ne 0) { throw 'MSYS2 dependency installation failed' }
    New-Item -ItemType File -Path (Join-Path $toolsDir 'windows-dependencies-ready') -Force | Out-Null
}
& $bash -lc 'bash scripts/setup-windows.sh'
if ($LASTEXITCODE -ne 0) { throw 'SCUFF Windows build failed; see build/logs/setup-windows.log' }
& py -3.11 -c 'import numpy, gmsh; from PyQt5 import QtWidgets, QtWebEngineWidgets'
if ($LASTEXITCODE -ne 0) {
    & py -3.11 -m pip install -r scripts/requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Install Windows Python 3.11 and the requirements' }
}
& py -3.11 scripts/bundle_windows.py
if ($LASTEXITCODE -ne 0) { throw 'Native runtime DLL verification failed' }
Write-Output 'Pixel antenna: py -3.11 test/run_planar.py'
Write-Output 'Original dipole test: py -3.11 test/run_test.py --frequency-ghz 3.2'
Stop-Transcript | Out-Null
