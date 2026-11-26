# Windows PowerShell 5.1 wraps native stderr in ErrorRecord objects.
function Invoke-CchouseNative {
  param([string]$FilePath, [string[]]$ArgumentList, [switch]$ShowStderr)

  $output = [System.Collections.Generic.List[string]]::new()
  $stderr = [System.Collections.Generic.List[string]]::new()
  $ErrorActionPreference = "Continue"
  $PSNativeCommandUseErrorActionPreference = $false
  & $FilePath @ArgumentList 2>&1 | ForEach-Object {
    if ($_ -is [System.Management.Automation.ErrorRecord]) {
      if ($_.FullyQualifiedErrorId -notin @("NativeCommandError", "NativeCommandErrorMessage")) {
        throw $_
      }
      $line = $_.ToString()
      $stderr.Add($line)
      if ($ShowStderr) { [Console]::Error.WriteLine($line) }
    } else {
      $output.Add([string]$_)
    }
  }
  $code = $LASTEXITCODE
  return [pscustomobject]@{ ExitCode = $code; Output = $output.ToArray(); Stderr = $stderr -join "`n" }
}

function Invoke-CchouseNativeChecked {
  param([string]$FilePath, [string[]]$ArgumentList, [string]$FailureMessage)

  $result = Invoke-CchouseNative -FilePath $FilePath -ArgumentList $ArgumentList -ShowStderr
  $result.Output
  if ($result.ExitCode -ne 0) {
    throw "$FailureMessage (rc=$($result.ExitCode))"
  }
}
