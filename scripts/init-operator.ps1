$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
$target=Join-Path $taskRoot 'state/secrets/operator-token.dpapi'
if(-not(Test-Path -LiteralPath $target)){
 Add-Type -AssemblyName System.Security
 $randomBytes=New-Object byte[] 32
 $random=[Security.Cryptography.RandomNumberGenerator]::Create();$random.GetBytes($randomBytes);$random.Dispose()
 $plain=[Text.Encoding]::UTF8.GetBytes([Convert]::ToBase64String($randomBytes))
 $encrypted=[Security.Cryptography.ProtectedData]::Protect($plain,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
 [IO.File]::WriteAllBytes($target,$encrypted)
}
Write-Output 'Operator credential ready (CurrentUser DPAPI); no key printed'
