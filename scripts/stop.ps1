$taskRoot=Split-Path -Parent $PSScriptRoot
[IO.File]::WriteAllText((Join-Path $taskRoot 'state/stop.request'),'operator stop')
Write-Output 'Stop requested for this project supervisor; paid holds retained'
