$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$entry = Join-Path $projectRoot 'switchboard_exe.py'
$icon = Join-Path $projectRoot 'assets\switchboard-modern.ico'
$versionInfo = Join-Path $projectRoot 'windows-version-info.txt'
$qtRuntime = Join-Path $projectRoot '.venv\Lib\site-packages\PySide6'
$windowsIcu = Join-Path $env:SystemRoot 'System32\icuuc.dll'
$release = Join-Path $projectRoot 'release'
$work = Join-Path $projectRoot 'build\pyinstaller'
$spec = Join-Path $projectRoot 'build'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing build environment: $python"
}

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name 'Codex-Switchboard' `
    --icon $icon `
    --version-file $versionInfo `
    --add-binary "$(Join-Path $qtRuntime 'concrt140.dll');PySide6" `
    --add-binary "$(Join-Path $qtRuntime 'msvcp140_codecvt_ids.dll');PySide6" `
    --add-binary "$windowsIcu;PySide6" `
    --distpath $release `
    --workpath $work `
    --specpath $spec `
    $entry

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$exe = Join-Path $release 'Codex-Switchboard.exe'
$item = Get-Item -LiteralPath $exe
$hash = Get-FileHash -LiteralPath $exe -Algorithm SHA256
[pscustomobject]@{
    Path = $item.FullName
    SizeMB = [math]::Round($item.Length / 1MB, 2)
    SHA256 = $hash.Hash
}
