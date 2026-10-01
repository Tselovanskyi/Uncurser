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
# Other tools on PATH can supply incompatible DLLs with Windows library names.
# Let PyInstaller resolve dependencies from this environment and Windows only.
$buildOriginalPath = $env:PATH
try {
    $env:PATH = @((Split-Path -Parent $buildPython), (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
    & $buildPython -m PyInstaller --noconfirm --clean --onedir --windowed --name Uncurser --distpath dist --workpath build --specpath build --add-data 'app/assets:app/assets' --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick --exclude-module PySide6.QtWebEngineCore --exclude-module tkinter Uncurser.py
    if ($LASTEXITCODE -ne 0) { throw 'Portable build failed.' }
} finally {
    $env:PATH = $buildOriginalPath
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination (Join-Path $PSScriptRoot 'dist\Uncurser\README.md')
& $buildPython tools\bundle_licenses.py
if ($LASTEXITCODE -ne 0) { throw 'License collection failed.' }
$buildApp = Join-Path $PSScriptRoot 'dist\Uncurser'
$buildCheckJson = Join-Path $buildApp 'data\self-check.json'
$buildCheckImage = Join-Path $buildApp 'data\self-check.png'
foreach ($checkFile in @($buildCheckJson, $buildCheckImage)) {
    if (Test-Path -LiteralPath $checkFile) { Remove-Item -LiteralPath $checkFile }
}
$buildProcess = Start-Process -FilePath (Join-Path $buildApp 'Uncurser.exe') -ArgumentList '--self-check' -WindowStyle Hidden -PassThru
if (-not $buildProcess.WaitForExit(20000)) {
    Stop-Process -Id $buildProcess.Id -ErrorAction SilentlyContinue
    throw 'The packaged app did not complete its startup check. ZIP not updated.'
}
if ($buildProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $buildCheckJson)) {
    throw 'The packaged app failed its startup check. ZIP not updated.'
}
$buildCheck = Get-Content -LiteralPath $buildCheckJson -Raw | ConvertFrom-Json
if (-not $buildCheck.bundled -or $buildCheck.ui -ne 'ready') { throw 'Invalid packaged startup check.' }
$buildCheckDir = Join-Path $PSScriptRoot 'build\ui-check'
New-Item -ItemType Directory -Path $buildCheckDir -Force | Out-Null
Copy-Item -LiteralPath $buildCheckImage -Destination (Join-Path $buildCheckDir 'packaged.png')
Copy-Item -LiteralPath $buildCheckJson -Destination (Join-Path $buildCheckDir 'packaged.json')
Remove-Item -LiteralPath $buildCheckImage, $buildCheckJson
$buildLog = Join-Path $buildApp 'data\Uncurser.log'
if ((Test-Path -LiteralPath $buildLog) -and (Get-Item -LiteralPath $buildLog).Length -eq 0) { Remove-Item -LiteralPath $buildLog }
Compress-Archive -LiteralPath (Join-Path $PSScriptRoot 'dist\Uncurser') -DestinationPath (Join-Path $PSScriptRoot 'dist\Uncurser-portable-win64.zip') -Force
Write-Output 'Portable app: dist\Uncurser\Uncurser.exe'
