<#
.SYNOPSIS
Creates an advisory Silero VAD versus subtitle-entry sync report.

.DESCRIPTION
Runs the project health checks, serializes the full-media VAD pass against the
same shared media-worker lock used by the Whisper queue, and calls
`subflow.py sync --backend silero`. The command never rewrites SRT or manifest
timings. Its JSON output is a ranked candidate list for agent review.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Manifest,

    [string]$Output,
    [string]$Python = 'python',
    [string]$Subflow,
    [string]$RuntimeRoot,
    [ValidateRange(0.01, 0.99)]
    [double]$VadThreshold = 0.50,
    [ValidateRange(0.01, 0.99)]
    [double]$VadNegThreshold = 0.35,
    [ValidateRange(0, 60000)]
    [int]$VadMinSpeechMs = 100,
    [ValidateRange(0, 60000)]
    [int]$VadMinSilenceMs = 250,
    [ValidateRange(0, 10000)]
    [int]$VadSpeechPadMs = 100,
    [ValidateRange(0.0, 60.0)]
    [double]$SearchWindowSeconds = 0.75,
    [ValidateRange(1, 60000)]
    [int]$ReviewThresholdMs = 450,
    [ValidateRange(0.0, 1.0)]
    [double]$LowOverlapRatio = 0.35,
    [ValidateRange(0.0, 1.0)]
    [double]$NoOverlapRatio = 0.10,
    [ValidateRange(0, 10000)]
    [int]$UtteranceJoinGapMs = 120,
    [ValidateRange(1, 60000)]
    [int]$OrphanSpeechMinMs = 300,
    [ValidateRange(0, 60000)]
    [int]$AllowTruncatedAudioTailMs = 0,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Subflow) {
    $Subflow = Join-Path $projectRoot 'subflow.py'
}
$manifestPath = (Resolve-Path -LiteralPath $Manifest).Path
if (-not $Output) {
    $Output = Join-Path (Split-Path -Parent $manifestPath) 'sync_analysis.json'
}
$outputPath = [System.IO.Path]::GetFullPath($Output)
if ($VadNegThreshold -ge $VadThreshold) {
    throw 'VadNegThreshold must be lower than VadThreshold.'
}
if ($NoOverlapRatio -gt $LowOverlapRatio) {
    throw 'NoOverlapRatio must be less than or equal to LowOverlapRatio.'
}
if ($outputPath.Equals($manifestPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Output must not overwrite the manifest.'
}
if ((Test-Path -LiteralPath $outputPath) -and -not $Force) {
    throw "Refusing to overwrite existing sync report: $outputPath. Use -Force only after preserving any baseline report."
}

function Format-Invariant([double]$Value) {
    return $Value.ToString([System.Globalization.CultureInfo]::InvariantCulture)
}

& $Python -X utf8 $Subflow doctor
if ($LASTEXITCODE -ne 0) {
    throw "subflow doctor failed with exit code $LASTEXITCODE."
}

$doctorArguments = @('-X', 'utf8', $Subflow, 'whisper-doctor')
if ($RuntimeRoot) {
    $doctorArguments += @('--runtime-root', [System.IO.Path]::GetFullPath($RuntimeRoot))
}
$doctorOutput = @(& $Python @doctorArguments)
$doctorExitCode = $LASTEXITCODE
$doctorOutput | Write-Output
if ($doctorExitCode -ne 0) {
    throw 'Silero VAD needs the existing project-local Whisper runtime. Installation requires separate user approval.'
}
$doctorReport = ($doctorOutput -join [Environment]::NewLine) | ConvertFrom-Json
if (-not $doctorReport.silero_vad.available) {
    throw "The Whisper runtime is installed but Silero VAD is unavailable: $($doctorReport.silero_vad.error)"
}

$runtimeDirectory = Join-Path $projectRoot '.runtime'
[System.IO.Directory]::CreateDirectory($runtimeDirectory) | Out-Null
$mediaLockPath = Join-Path $runtimeDirectory 'media-worker.lock'
$mediaLockStream = $null
try {
    try {
        $mediaLockStream = [System.IO.File]::Open(
            $mediaLockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    }
    catch [System.IO.IOException] {
        throw 'Another Whisper transcription or Silero VAD media job is running. Wait for the transcription queue to become idle.'
    }

    $arguments = @(
        '-X', 'utf8', $Subflow, 'sync',
        '--manifest', $manifestPath,
        '--output', $outputPath,
        '--backend', 'silero',
        '--vad-threshold', (Format-Invariant $VadThreshold),
        '--vad-neg-threshold', (Format-Invariant $VadNegThreshold),
        '--vad-min-speech-ms', [string]$VadMinSpeechMs,
        '--vad-min-silence-ms', [string]$VadMinSilenceMs,
        '--vad-speech-pad-ms', [string]$VadSpeechPadMs,
        '--search-window', (Format-Invariant $SearchWindowSeconds),
        '--review-threshold-ms', [string]$ReviewThresholdMs,
        '--low-overlap-ratio', (Format-Invariant $LowOverlapRatio),
        '--no-overlap-ratio', (Format-Invariant $NoOverlapRatio),
        '--utterance-join-gap-ms', [string]$UtteranceJoinGapMs,
        '--orphan-speech-min-ms', [string]$OrphanSpeechMinMs
    )
    if ($AllowTruncatedAudioTailMs -gt 0) {
        $arguments += @('--allow-truncated-audio-tail-ms', [string]$AllowTruncatedAudioTailMs)
    }
    if ($RuntimeRoot) {
        $arguments += @('--runtime-root', [System.IO.Path]::GetFullPath($RuntimeRoot))
    }
    if ($Force) {
        $arguments += '--force'
    }
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Silero sync audit failed with exit code $LASTEXITCODE."
    }
}
finally {
    if ($mediaLockStream) {
        $mediaLockStream.Dispose()
    }
}
