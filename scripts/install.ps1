param([ValidateSet('P0')][string]$Stage='P0')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$report=Join-Path $root 'state/deployment/P0.json'
if (Test-Path -LiteralPath $report) {
    $record=Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    if ($record.environment_status -eq 'PASS') {
        & (Join-Path $PSScriptRoot 'doctor.ps1') -Json
        exit $LASTEXITCODE
    }
}
# Re-entrant installation uses only the recorded interpreters and frozen locks.
$versions=Get-Content -LiteralPath (Join-Path $root 'config/versions.json') -Raw | ConvertFrom-Json
$env:UV_LINK_MODE='copy'
foreach ($name in @('mpt','narrato')) {
    $folder=if($name -eq 'mpt'){'MoneyPrinterTurbo'}else{'NarratoAI'}
    $env:UV_PROJECT_ENVIRONMENT=Join-Path $root "envs/$name"
    Push-Location (Join-Path $root "upstream/$folder")
    try {
        & uv sync --frozen --no-dev --python $versions.interpreters.$name
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } finally { Pop-Location }
}
& uv pip install --python (Join-Path $root 'envs/scheduler/Scripts/python.exe') --require-hashes -r (Join-Path $root 'locks/scheduler.lock')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& uv pip install --python (Join-Path $root 'envs/narrato/Scripts/python.exe') --require-hashes -r (Join-Path $root 'locks/narrato-worker.lock')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& (Join-Path $PSScriptRoot 'doctor.ps1') -Json
exit $LASTEXITCODE
