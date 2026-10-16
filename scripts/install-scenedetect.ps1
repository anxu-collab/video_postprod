$ErrorActionPreference = 'Stop'
$postprodRoot = Split-Path -Parent $PSScriptRoot
$narratoPython = Join-Path $postprodRoot 'envs/narrato/Scripts/python.exe'
$uvExecutable = 'C:/Users/anxu/AppData/Roaming/Python/Python312/Scripts/uv.exe'
$sceneLock = Join-Path $postprodRoot 'locks/scenedetect.lock'
if (!(Test-Path -LiteralPath (Join-Path $postprodRoot 'state/deployment/scenedetect-baseline.json'))) {
    throw 'Installation baseline is missing. Do not overwrite an existing environment blindly.'
}
$dryRunOutput = & $uvExecutable pip install --python $narratoPython --dry-run --require-hashes -r $sceneLock 2>&1
$dryRunExit = $LASTEXITCODE
$dryRunOutput | Tee-Object -FilePath (Join-Path $postprodRoot 'logs/scenedetect-dryrun.log')
if ($dryRunExit -ne 0) { exit $dryRunExit }
if ($dryRunOutput | Where-Object { "$_" -match '^\s*[-~]\s+[A-Za-z0-9]' }) {
    throw 'Installation would replace or remove an existing dependency. Inspect compatibility first.'
}
& $uvExecutable pip install --link-mode copy --python $narratoPython --require-hashes -r $sceneLock
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $narratoPython (Join-Path $PSScriptRoot 'verify-scenedetect.py')
exit $LASTEXITCODE
