# AIMurahV3 launcher (PowerShell)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = "$here;$env:PYTHONPATH"
$venvPython = Join-Path $here ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPython) {
    $pythonCmd = $venvPython
} else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
    if ($python) { $pythonCmd = $python.Source }
}
if (-not $pythonCmd) {
    Write-Error "python interpreter not found"
    exit 1
}
& $pythonCmd -m aimurah @args
exit $LASTEXITCODE
