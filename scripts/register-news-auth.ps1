# Run personally in PowerShell. Neither secret is printed, put in arguments, or saved locally.
$ErrorActionPreference = 'Stop'
$clientSecure = Read-Host 'NAVER API Hub 앱의 Client ID (화면에 표시되지 않습니다)' -AsSecureString
$secretSecure = Read-Host 'NAVER API Hub 앱의 Client Secret' -AsSecureString
$clientPointer = [IntPtr]::Zero
$secretPointer = [IntPtr]::Zero
try {
    $clientPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($clientSecure)
    $clientValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($clientPointer)
    $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secretSecure)
    $secretValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    $payload = @{ client_id = $clientValue; client_secret = $secretValue } | ConvertTo-Json -Compress
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = 'C:\WINDOWS\System32\OpenSSH\ssh.exe'
    $start.Arguments = '-T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 144.24.92.159 "sudo -n python3 /srv/stock-assistant/current/deploy/register_oracle_news_auth.py"'
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($start)
    # .NET Framework (Windows PowerShell 5.1) has no StandardInputEncoding.
    # Own a UTF-8 writer over the pipe so its encoding is independent of the console.
    $inputWriter = [IO.StreamWriter]::new($process.StandardInput.BaseStream, [Text.UTF8Encoding]::new($false))
    try { $inputWriter.WriteLine($payload) }
    finally { $inputWriter.Dispose() }
    $safeResponse = $process.StandardOutput.ReadToEnd()
    $discardedError = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        $code = 'REGISTRATION_UNAVAILABLE'
        try { $reply = $safeResponse | ConvertFrom-Json; if ($reply.error) { $code = $reply.error } } catch {}
        throw "등록 실패: $code"
    }
    $safeResponse
}
finally {
    if ($clientPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($clientPointer) }
    if ($secretPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer) }
    $clientValue = $null; $secretValue = $null; $payload = $null
    if ($clientSecure) { $clientSecure.Dispose() }
    if ($secretSecure) { $secretSecure.Dispose() }
}
