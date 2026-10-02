param([Parameter(Mandatory=$true)][string]$Python)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskSource = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'register-market-auth.ps1'), [Text.Encoding]::UTF8)
$taskFixture = Join-Path $taskRoot 'tests/fixtures/registration_pipe_probe.py'
$taskFileAssignment = '    $start.FileName = ''C:\WINDOWS\System32\OpenSSH\ssh.exe'''
$taskArgumentAssignments = @($taskSource -split '\r?\n' | Where-Object { $_.Trim().StartsWith('$start.Arguments = ') })
if (-not $taskSource.Contains($taskFileAssignment) -or $taskArgumentAssignments.Count -ne 1) {
    throw 'Registration entry point changed; recheck the local transport test.'
}

function Invoke-InputCase {
    param([string]$Mode, [bool]$Reject, [bool]$UseDefault = $false)
    $taskInputs = [Collections.Generic.Queue[Security.SecureString]]::new()
    if ($Mode -eq 'CLIENT_CREDENTIALS') {
        $taskInputs.Enqueue((ConvertTo-SecureString ('SYNTHETIC-ID-' + [char]0xD14C + [char]0xC2A4 + [char]0xD2B8) -AsPlainText -Force))
        $taskInputs.Enqueue((ConvertTo-SecureString ('SYNTHETIC-SECRET-' + [char]0xE9) -AsPlainText -Force))
    }
    else { $taskInputs.Enqueue((ConvertTo-SecureString 'SYNTHETIC-TOKEN' -AsPlainText -Force)) }
    function Read-Host {
        param([string]$Prompt, [switch]$AsSecureString)
        if (-not $AsSecureString) { throw 'Credential prompt must use secure input.' }
        return $taskInputs.Dequeue()
    }
    # Replace only the child executable and fixed arguments in memory. The actual
    # registration script, JSON serialization, pipe, error handling and cleanup run.
    # The fake child is local Python, so neither SSH nor Oracle can be contacted.
    $taskArguments = '"' + $taskFixture + '" --mode ' + $Mode
    if ($Reject) { $taskArguments += ' --reject' }
    $taskCode = $taskSource.Replace($taskFileAssignment, ('    $start.FileName = ''' + $Python.Replace("'", "''") + ''''))
    $taskCode = $taskCode.Replace($taskArgumentAssignments[0], ('    $start.Arguments = ''' + $taskArguments.Replace("'", "''") + ''''))
    $taskBlock = [ScriptBlock]::Create($taskCode)
    $taskCaught = $null
    $taskResponse = $null
    try {
        $taskResponse = if ($UseDefault) { & $taskBlock } else { & $taskBlock -Mode $Mode }
    }
    catch { $taskCaught = $_.Exception.Message }
    if ($Reject) {
        if (-not $taskCaught -or -not $taskCaught.EndsWith('SHARED_OPERATING_CLIENT_FORBIDDEN')) {
            throw 'Expected rejection code was not preserved.'
        }
        if ($taskCaught.Contains('SYNTHETIC-PRIVATE')) { throw 'Private stderr leaked into failure output.' }
    }
    else {
        if ($taskCaught) { throw ('Local registration transport failed: ' + $taskCaught) }
        $taskText = $taskResponse -join "`n"
        if ($taskText.Contains('SYNTHETIC-')) { throw 'Credentials or private stderr leaked into output.' }
        $taskResult = $taskText | ConvertFrom-Json
        if ($taskResult.status -ne 'LOCAL_PIPE_TEST_PASSED' -or $taskResult.credential_values_returned -ne $false) {
            throw 'Unexpected local registration response.'
        }
    }
    if ($taskInputs.Count) { throw 'Not all secure credential prompts were consumed.' }
}

Invoke-InputCase -Mode CLIENT_CREDENTIALS -Reject $false -UseDefault $true
Invoke-InputCase -Mode CLIENT_CREDENTIALS -Reject $false
Invoke-InputCase -Mode ACCESS_TOKEN -Reject $false
Invoke-InputCase -Mode CLIENT_CREDENTIALS -Reject $true
Write-Output ('Auth input transport passed: PowerShell ' + $PSVersionTable.PSVersion + ', 4 cases, UTF-8 without BOM, no SSH/provider calls.')
