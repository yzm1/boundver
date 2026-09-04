#Requires -Version 5.1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$releaseArguments = @($args)
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$pythonScript = Join-Path $PSScriptRoot "publish_release.py"

if (-not (Test-Path -LiteralPath $pythonScript -PathType Leaf)) {
    throw "The adjacent release launcher is missing: $pythonScript"
}

function Test-RepositoryLocalPath {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Candidate,
        [Parameter(Mandatory = $true)]
        [string] $Repository
    )

    $comparison = [StringComparison]::Ordinal
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        $comparison = [StringComparison]::OrdinalIgnoreCase
    }
    $separator = [IO.Path]::DirectorySeparatorChar
    $root = [IO.Path]::GetFullPath($Repository).TrimEnd(
        [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    )
    $path = [IO.Path]::GetFullPath($Candidate)
    return $path.Equals($root, $comparison) -or
        $path.StartsWith("$root$separator", $comparison)
}

function Resolve-TrustedPython {
    foreach ($name in @("py", "python3", "python")) {
        $command = Get-Command -Name $name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -eq $command) {
            continue
        }

        $rawPath = [IO.Path]::GetFullPath($command.Source)
        if (Test-RepositoryLocalPath -Candidate $rawPath -Repository $repoRoot) {
            throw "Refusing a Python launcher inside the repository: $rawPath"
        }
        $resolvedPath = (Resolve-Path -LiteralPath $rawPath).Path
        if (Test-RepositoryLocalPath -Candidate $resolvedPath -Repository $repoRoot) {
            throw "Refusing a Python launcher that resolves inside the repository: $resolvedPath"
        }

        $prefix = @()
        if ($name -eq "py") {
            $prefix = @("-3")
        }
        return [PSCustomObject]@{
            Executable = $resolvedPath
            Prefix = $prefix
        }
    }
    throw "Python 3 is required to run the release launcher."
}

$python = Resolve-TrustedPython
$tokenName = "BOUNDVER_RELEASE_REVIEW_TOKEN"
$previousToken = [Environment]::GetEnvironmentVariable($tokenName, "Process")
$injectedToken = $false
$secureToken = $null
$tokenPointer = [IntPtr]::Zero
$plainToken = $null
$exitCode = 1
$helpOnly = $releaseArguments -contains "--help" -or $releaseArguments -contains "-h"
$needsReviewToken = $releaseArguments.Count -gt 0 -and
    $releaseArguments[0] -in @("check", "start")

try {
    if ($needsReviewToken -and -not $helpOnly -and [string]::IsNullOrEmpty($previousToken)) {
        $secureToken = Read-Host "Read-only release-review token" -AsSecureString
        if ($secureToken.Length -eq 0) {
            throw "The release-review token cannot be empty."
        }
        $tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
        $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
        if ([string]::IsNullOrWhiteSpace($plainToken) -or $plainToken -match "\s") {
            throw "The release-review token cannot be empty or contain whitespace."
        }
        [Environment]::SetEnvironmentVariable($tokenName, $plainToken, "Process")
        $injectedToken = $true
    }

    Push-Location -LiteralPath $repoRoot
    try {
        & $python.Executable @($python.Prefix) "-I" $pythonScript @releaseArguments
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($injectedToken) {
        [Environment]::SetEnvironmentVariable($tokenName, $previousToken, "Process")
    }
    $plainToken = $null
    if ($tokenPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
    }
    if ($null -ne $secureToken) {
        $secureToken.Dispose()
    }
}

exit $exitCode
