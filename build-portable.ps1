$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot '.cache\pyinstaller'
$env:PIP_CACHE_DIR = Join-Path $PSScriptRoot '.cache\pip'
$env:PYTHONDONTWRITEBYTECODE = '1'
$buildPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project virtual environment.' }
}
& $buildPython -m pip install -r requirements.txt pyinstaller==6.22.3
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $buildPython tools\bundle_licenses.py
if ($LASTEXITCODE -ne 0) { throw 'License collection failed.' }
# Other tools on PATH can supply incompatible DLLs with Windows library names.
# Let PyInstaller resolve dependencies from this environment and Windows only.
$buildOriginalPath = $env:PATH
$buildAssets = (Join-Path $PSScriptRoot 'app\assets') + ':app/assets'
$buildLicenses = (Join-Path $PSScriptRoot 'build\licenses') + ':licenses'
try {
    $env:PATH = @((Split-Path -Parent $buildPython), (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
    & $buildPython -m PyInstaller --noconfirm --clean --onefile --windowed --name Uncurser --distpath build\single-exe-release --workpath build\single-exe --specpath build --add-data $buildAssets --add-data $buildLicenses --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick --exclude-module PySide6.QtWebEngineCore --exclude-module tkinter Uncurser.py
    if ($LASTEXITCODE -ne 0) { throw 'Portable build failed.' }
} finally {
    $env:PATH = $buildOriginalPath
}
$buildApp = Join-Path $PSScriptRoot 'build\single-exe-release'
$buildCheckDir = Join-Path $PSScriptRoot 'build\ui-check'
$buildCheckJson = Join-Path $buildCheckDir 'self-check.json'
$buildCheckImage = Join-Path $buildCheckDir 'self-check.png'
foreach ($checkFile in @($buildCheckJson, $buildCheckImage)) {
    if (Test-Path -LiteralPath $checkFile) { Remove-Item -LiteralPath $checkFile }
}
$previousCheckDir = $env:UNCURSER_CHECK_DIR
try {
    $env:UNCURSER_CHECK_DIR = $buildCheckDir
    $buildProcess = Start-Process -FilePath (Join-Path $buildApp 'Uncurser.exe') -ArgumentList '--self-check' -WindowStyle Hidden -PassThru
} finally {
    $env:UNCURSER_CHECK_DIR = $previousCheckDir
}
if (-not $buildProcess.WaitForExit(45000)) {
    Stop-Process -Id $buildProcess.Id -ErrorAction SilentlyContinue
    throw 'The packaged app did not complete its startup check. Installed app not updated.'
}
if ($buildProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $buildCheckJson)) {
    throw 'The packaged app failed its startup check. Installed app not updated.'
}
$buildCheck = Get-Content -LiteralPath $buildCheckJson -Raw | ConvertFrom-Json
if (-not $buildCheck.bundled -or $buildCheck.ui -ne 'ready') { throw 'Invalid packaged startup check.' }
Copy-Item -LiteralPath $buildCheckImage -Destination (Join-Path $buildCheckDir 'packaged.png')
Copy-Item -LiteralPath $buildCheckJson -Destination (Join-Path $buildCheckDir 'packaged.json')
Remove-Item -LiteralPath $buildCheckImage, $buildCheckJson
$installedApp = Join-Path $PSScriptRoot 'dist\Uncurser'
$installedExe = Join-Path $installedApp 'Uncurser.exe'
if (Get-Process -Name Uncurser -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $installedExe }) {
    throw 'Close Uncurser before replacing it. The checked build is in build\single-exe-release\Uncurser.exe.'
}
New-Item -ItemType Directory -Path $installedApp -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $buildApp 'Uncurser.exe') -Destination $installedExe -Force
if ((Get-FileHash -LiteralPath $installedExe).Hash -ne (Get-FileHash -LiteralPath (Join-Path $buildApp 'Uncurser.exe')).Hash) {
    throw 'Installed executable verification failed.'
}
Write-Output 'Portable app: dist\Uncurser\Uncurser.exe'
