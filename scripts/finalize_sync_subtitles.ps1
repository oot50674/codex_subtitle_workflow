[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Manifest,

    [Parameter(Mandatory = $true)]
    [string]$Decisions,

    [Parameter(Mandatory = $true)]
    [string]$Output,

    [string]$Python = 'python',
    [string]$Subflow,

    [ValidateRange(0, 3000)]
    [int]$StartPadMs = 300,

    [ValidateRange(0, 3000)]
    [int]$EndPadMs = 800,

    [ValidateRange(0, 3000)]
    [int]$MaxPadMs = 3000,

    [switch]$AllowNoTimingChanges
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $Subflow) {
    $Subflow = Join-Path $projectRoot 'subflow.py'
}

if ($StartPadMs -gt $MaxPadMs -or $EndPadMs -gt $MaxPadMs) {
    throw 'StartPadMs and EndPadMs must not exceed MaxPadMs.'
}

$manifestFull = [IO.Path]::GetFullPath($Manifest)
$decisionsFull = [IO.Path]::GetFullPath($Decisions)
$subflowFull = [IO.Path]::GetFullPath($Subflow)
$paddingScript = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'apply_timing_padding.py'))
$outputFull = [IO.Path]::GetFullPath($Output)
$outputParent = Split-Path -Parent $outputFull
$outputLeaf = Split-Path -Leaf $outputFull

foreach ($required in @($manifestFull, $decisionsFull, $subflowFull, $paddingScript)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required file is missing: $required"
    }
}
if (Test-Path -LiteralPath $outputFull) {
    throw "Refusing to replace an existing final output: $outputFull"
}

New-Item -ItemType Directory -Path $outputParent -Force | Out-Null
$resolvedParent = (Resolve-Path -LiteralPath $outputParent).Path
$staging = Join-Path $resolvedParent ('.tmp-' + $outputLeaf + '-' + [guid]::NewGuid().ToString('N'))
$stagingFull = [IO.Path]::GetFullPath($staging)
$parentFull = [IO.Path]::GetFullPath($resolvedParent)
if (-not [StringComparer]::OrdinalIgnoreCase.Equals(
    [IO.Path]::GetDirectoryName($stagingFull),
    $parentFull
)) {
    throw "Unsafe staging path: $stagingFull"
}

function Invoke-PythonChecked {
    param([string[]]$Arguments)
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE"
    }
}

$moved = $false
try {
    New-Item -ItemType Directory -Path $stagingFull | Out-Null
    Invoke-PythonChecked @('-X', 'utf8', $subflowFull, 'doctor')

    $paddedDecisions = Join-Path $stagingFull 'decisions.padded.json'
    $paddingReport = Join-Path $stagingFull 'timing-padding.json'
    $paddingArguments = @(
        '-X', 'utf8', $paddingScript,
        '--manifest', $manifestFull,
        '--decisions', $decisionsFull,
        '--output-decisions', $paddedDecisions,
        '--report', $paddingReport,
        '--start-pad-ms', [string]$StartPadMs,
        '--end-pad-ms', [string]$EndPadMs,
        '--max-pad-ms', [string]$MaxPadMs
    )
    if ($AllowNoTimingChanges) {
        $paddingArguments += '--allow-no-targets'
    }
    Invoke-PythonChecked $paddingArguments

    Invoke-PythonChecked @(
        '-X', 'utf8', $subflowFull, 'apply',
        '--manifest', $manifestFull,
        '--decisions', $paddedDecisions,
        '--output', $stagingFull
    )
    Invoke-PythonChecked @(
        '-X', 'utf8', $subflowFull, 'verify',
        '--manifest', $manifestFull,
        '--output', $stagingFull
    )

    $snapshotDirectory = Join-Path $stagingFull 'input-snapshot'
    New-Item -ItemType Directory -Path $snapshotDirectory -Force | Out-Null
    Copy-Item -LiteralPath $manifestFull -Destination (Join-Path $snapshotDirectory 'manifest.json')
    Copy-Item -LiteralPath $decisionsFull -Destination (Join-Path $snapshotDirectory 'reviewed-decisions.json')

    $finalization = [ordered]@{
        schema_version = 1
        status = 'verified'
        created_at = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssK')
        manifest = $manifestFull
        reviewed_decisions = $decisionsFull
        padding = [ordered]@{
            start_ms = $StartPadMs
            end_ms = $EndPadMs
            maximum_ms = $MaxPadMs
            policy = 'reviewed overrides only; collision-free proportional gap allocation'
        }
        output = $outputFull
    }
    $finalization | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $stagingFull 'finalization.json') -Encoding utf8

    if (Test-Path -LiteralPath $outputFull) {
        throw "Final output appeared while verification was running: $outputFull"
    }
    [IO.Directory]::Move($stagingFull, $outputFull)
    $moved = $true
    Write-Host "Verified padded subtitle output: $outputFull"
}
finally {
    if (-not $moved -and (Test-Path -LiteralPath $stagingFull -PathType Container)) {
        $currentParent = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($stagingFull))
        if ([StringComparer]::OrdinalIgnoreCase.Equals($currentParent, $parentFull) -and
            ([IO.Path]::GetFileName($stagingFull)).StartsWith('.tmp-' + $outputLeaf + '-')) {
            Remove-Item -LiteralPath $stagingFull -Recurse -Force
        }
    }
}
