param([switch]$OpenUI)
$ErrorActionPreference='Stop';$taskRoot=Split-Path -Parent $PSScriptRoot
& (Join-Path $PSScriptRoot 'init-operator.ps1')
$python=Join-Path $taskRoot 'envs/scheduler/Scripts/python.exe';$script=Join-Path $taskRoot 'scheduler/supervisor.py'
$env:PYTHONUTF8='1';$env:PYTHONNOUSERSITE='1'
$running=$false;$record=Join-Path $taskRoot 'state/processes/supervisor.json'
if(Test-Path -LiteralPath $record){
 $previous=Get-Content -LiteralPath $record -Raw | ConvertFrom-Json
 $owner=Get-CimInstance Win32_Process -Filter "ProcessId=$($previous.pid)" -ErrorAction SilentlyContinue
 if($owner -and $owner.CommandLine.Replace('/','\').Contains($script.Replace('/','\'))){$running=$true}
}
if(-not $running){Start-Process -FilePath $python -ArgumentList @($script) -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskRoot 'logs/supervisor.stdout.log') -RedirectStandardError (Join-Path $taskRoot 'logs/supervisor.stderr.log') | Out-Null}
if($OpenUI){
 Add-Type -AssemblyName System.Security
 $raw=[Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes((Join-Path $taskRoot 'state/secrets/operator-token.dpapi')),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
 $token=[Text.Encoding]::UTF8.GetString($raw)
 Start-Process -FilePath "http://127.0.0.1:8760/#$token" | Out-Null
}
Write-Output 'Local automatic postproduction startup requested'
