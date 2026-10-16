param([ValidateSet('mpt','narrato','scheduler')][string]$Component,[switch]$Restart)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
$port=@{mpt=8761;narrato=8762;scheduler=8760}[$Component]
$python=Join-Path $taskRoot "envs/$Component/Scripts/python.exe"
$script=if($Component -eq 'scheduler'){Join-Path $taskRoot 'scheduler/server.py'}else{Join-Path $taskRoot "workers/$Component/server.py"}
$listener=Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if($listener){
 if(-not $Restart){Write-Output "$Component already listening";exit 0}
 $process=Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
 if($process.CommandLine -notlike "*$Component/server.py*" -and $process.CommandLine -notlike "*$Component\server.py*"){throw 'PORT_OWNER_UNVERIFIED'}
 Stop-Process -Id $listener.OwningProcess
}
$tokenFile=Join-Path $taskRoot 'state/secrets/worker-token.dpapi'
Add-Type -AssemblyName System.Security
$plain=[Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($tokenFile),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
$env:POSTPROD_WORKER_TOKEN=[Text.Encoding]::UTF8.GetString($plain)
$env:PYTHONUTF8='1';$env:PYTHONNOUSERSITE='1'
$out=Join-Path $taskRoot "logs/$Component-service.stdout.log";$err=Join-Path $taskRoot "logs/$Component-service.stderr.log"
$p=Start-Process -FilePath $python -ArgumentList @($script) -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
@{pid=$p.Id;started_utc=[DateTime]::UtcNow.ToString('o');executable=$python;script=$script}|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $taskRoot "state/processes/$Component.json")
Remove-Item Env:POSTPROD_WORKER_TOKEN
for($waitIndex=0;$waitIndex -lt 45;$waitIndex++){
 try{$health=Invoke-RestMethod -Uri "http://127.0.0.1:$port/health/live" -TimeoutSec 2;if($health.status -eq 'alive'){Write-Output "$Component ready";exit 0}}catch{}
 Start-Sleep -Seconds 1
}
throw "$Component startup timeout; inspect its existing log before retrying"
Write-Output "$Component launch recorded"
