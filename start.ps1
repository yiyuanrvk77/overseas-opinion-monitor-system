$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$requirementsPath = Join-Path $projectRoot 'backend\requirements.txt'
$requirementsMarker = Join-Path $projectRoot '.venv\.requirements.sha256'
$packageLockPath = Join-Path $projectRoot 'package-lock.json'
$nodeModulesPath = Join-Path $projectRoot 'node_modules'
$nodeMarker = Join-Path $nodeModulesPath '.package-lock.sha256'
$distIndex = Join-Path $projectRoot 'dist\index.html'

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Native command failed with exit code ${LASTEXITCODE}: $FilePath"
    }
}

Set-Location $projectRoot
if (-not (Test-Path -LiteralPath $venvPython)) {
    Invoke-NativeCommand -FilePath 'python' -Arguments @('-m', 'venv', (Join-Path $projectRoot '.venv'))
}

$requirementsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $requirementsPath).Hash
$installedHash = if (Test-Path -LiteralPath $requirementsMarker) { (Get-Content -LiteralPath $requirementsMarker -Raw).Trim() } else { '' }
& $venvPython -c "import fastapi, starlette, uvicorn" 2>$null
$pythonReady = $LASTEXITCODE -eq 0
if (-not $pythonReady -or $installedHash -ne $requirementsHash) {
    Invoke-NativeCommand -FilePath $venvPython -Arguments @('-m', 'pip', 'install', '-r', $requirementsPath)
    Set-Content -LiteralPath $requirementsMarker -Value $requirementsHash -NoNewline
}

$buildRequired = -not (Test-Path -LiteralPath $distIndex)
if (-not $buildRequired) {
    $distTime = (Get-Item -LiteralPath $distIndex).LastWriteTimeUtc
    $buildInputs = @(
        Get-ChildItem -LiteralPath (Join-Path $projectRoot 'src') -Recurse -File
        Get-Item -LiteralPath (Join-Path $projectRoot 'index.html')
        Get-Item -LiteralPath (Join-Path $projectRoot 'package.json')
        Get-Item -LiteralPath $packageLockPath
        Get-Item -LiteralPath (Join-Path $projectRoot 'vite.config.js')
    )
    $buildRequired = $null -ne ($buildInputs | Where-Object { $_.LastWriteTimeUtc -gt $distTime } | Select-Object -First 1)
}

if ($buildRequired) {
    $packageHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $packageLockPath).Hash
    $installedPackageHash = if (Test-Path -LiteralPath $nodeMarker) { (Get-Content -LiteralPath $nodeMarker -Raw).Trim() } else { '' }
    if (-not (Test-Path -LiteralPath $nodeModulesPath) -or $installedPackageHash -ne $packageHash) {
        Invoke-NativeCommand -FilePath 'npm' -Arguments @('ci')
        Set-Content -LiteralPath $nodeMarker -Value $packageHash -NoNewline
    }
    Invoke-NativeCommand -FilePath 'npm' -Arguments @('run', 'build')
}

Invoke-NativeCommand -FilePath $venvPython -Arguments @('-m', 'uvicorn', 'backend.api:app', '--host', '127.0.0.1', '--port', '8000')
