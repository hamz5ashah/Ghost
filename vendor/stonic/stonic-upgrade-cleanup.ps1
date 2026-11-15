[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $InstallDir
)

$ErrorActionPreference = 'Stop'

function Write-CleanupLog {
    param([string] $Message)
    Write-Output "[Stonic AI upgrade] $Message"
}

function Get-NormalizedRoot {
    param([string] $Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $null
    }

    try {
        return ([System.IO.Path]::GetFullPath($Path)).TrimEnd('\')
    }
    catch {
        return $null
    }
}

function Get-ProcessSnapshot {
    try {
        return @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop | ForEach-Object {
            if (-not [string]::IsNullOrWhiteSpace($_.ExecutablePath)) {
                [PSCustomObject]@{
                    Id   = [int] $_.ProcessId
                    Path = [string] $_.ExecutablePath
                }
            }
        })
    }
    catch {
        # CIM can be disabled on hardened Windows images. Process.Path is a
        # sufficient same-user fallback for this per-user installer.
        return @(Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
            try {
                if (-not [string]::IsNullOrWhiteSpace($_.Path)) {
                    [PSCustomObject]@{
                        Id   = [int] $_.Id
                        Path = [string] $_.Path
                    }
                }
            }
            catch {
                # Protected processes are unrelated and deliberately ignored.
            }
        })
    }
}

function Test-StonicInstallRoot {
    param([string] $Root)

    if ([string]::IsNullOrWhiteSpace($Root)) {
        return $false
    }

    # Never trust an arbitrary /D directory. Both markers are emitted by every
    # packaged Stonic AI installation and keep a shared parent directory from
    # becoming an accidental process-kill boundary.
    # Teen shartein, aur teeno lazmi hain.
    #
    # Ye function tay karta hai ke $Root ke andar chalte huay processes maare ja
    # sakte hain ya nahi. Gen 3 ka exe aur folder ka naam Gen 2 ke bilkul barabar
    # hai (customer ko har jagah sirf "Stonic AI" dikhna chahiye), is liye naam
    # se pehchan mumkin hi nahi — marker hi wo cheez hai jo "hamara install" aur
    # "unka install" mein farq karti hai.
    #
    # Marker ke baghair ye script Gen 2 ki chalti hui app ke processes maar sakti
    # thi.
    $appExecutable = Join-Path $Root 'Stonic AI.exe'
    $appArchive = Join-Path $Root 'resources\app.asar'
    $gen3Marker = Join-Path $Root 'resources\stonic-gen3.marker'
    return (Test-Path -LiteralPath $appExecutable -PathType Leaf) -and
        (Test-Path -LiteralPath $appArchive -PathType Leaf) -and
        (Test-Path -LiteralPath $gen3Marker -PathType Leaf)
}

function Get-StonicProcesses {
    param(
        [object[]] $Snapshot,
        [System.Collections.Generic.HashSet[string]] $Roots
    )

    return @($Snapshot | Where-Object {
        $processPath = Get-NormalizedRoot $_.Path
        if ([string]::IsNullOrWhiteSpace($processPath)) {
            return $false
        }

        foreach ($root in $Roots) {
            $rootPrefix = $root + '\'
            if ($processPath.StartsWith(
                $rootPrefix,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                return $true
            }
        }

        return $false
    })
}

try {
    $normalizedInstallDir = Get-NormalizedRoot $InstallDir
    if ([string]::IsNullOrWhiteSpace($normalizedInstallDir)) {
        throw 'The installer supplied an invalid installation directory.'
    }

    if (-not (Test-StonicInstallRoot $normalizedInstallDir)) {
        Write-CleanupLog 'No verified existing Stonic AI installation was found.'
        exit 0
    }

    # $InstallDir comes from NSIS' resolved $INSTDIR. Never match global image
    # names: only the main executable and bundled binaries physically rooted in
    # this exact per-user installation can be selected.
    $roots = New-Object 'System.Collections.Generic.HashSet[string]' (
        [System.StringComparer]::OrdinalIgnoreCase
    )
    [void] $roots.Add($normalizedInstallDir)

    $snapshot = Get-ProcessSnapshot
    $targets = @(Get-StonicProcesses -Snapshot $snapshot -Roots $roots)

    if ($targets.Count -eq 0) {
        Write-CleanupLog 'No running installed components were found.'
        exit 0
    }

    Write-CleanupLog "Closing $($targets.Count) installed component process(es)."

    # Ask window-owning processes to close first so the old application gets a
    # chance to flush settings and databases before any forced termination.
    foreach ($target in $targets) {
        try {
            $process = Get-Process -Id $target.Id -ErrorAction Stop
            if ($process.MainWindowHandle -ne [IntPtr]::Zero) {
                [void] $process.CloseMainWindow()
            }
        }
        catch {
            # It may have exited between the snapshot and this request.
        }
    }

    $graceDeadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        Start-Sleep -Milliseconds 250
        $snapshot = Get-ProcessSnapshot
        $remaining = @(Get-StonicProcesses -Snapshot $snapshot -Roots $roots)
    } while ($remaining.Count -gt 0 -and [DateTime]::UtcNow -lt $graceDeadline)

    # Force only the verified installed binaries that did not exit gracefully.
    for ($attempt = 1; $attempt -le 4 -and $remaining.Count -gt 0; $attempt++) {
        foreach ($target in $remaining) {
            try {
                Stop-Process -Id $target.Id -Force -ErrorAction Stop
            }
            catch {
                # Re-query below; a race with normal exit is expected here.
            }
        }

        Start-Sleep -Milliseconds 400
        $snapshot = Get-ProcessSnapshot
        $remaining = @(Get-StonicProcesses -Snapshot $snapshot -Roots $roots)
    }

    if ($remaining.Count -gt 0) {
        Write-CleanupLog "Unable to stop $($remaining.Count) installed component process(es)."
        exit 20
    }

    Write-CleanupLog 'Installed components are stopped; upgrade can continue safely.'
    exit 0
}
catch {
    Write-CleanupLog "Cleanup failed safely: $($_.Exception.Message)"
    exit 10
}
