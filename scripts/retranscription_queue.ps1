<#
.SYNOPSIS
Queues every Whisper transcription request behind one global worker.

.DESCRIPTION
Use this for initial full-file transcription and for partial/full
retranscription, including `-JobType transcribe-cues`. The exclusive worker lock
prevents concurrent Whisper, FFmpeg, and Silero VAD processes from overloading the
system. Atomic identity claims suppress duplicate pending, running, and completed
media/range/options requests even when output paths differ. Use `-MaxJobs 1`
while streaming a batch so each completed file can be prepared and reviewed
before the next queue drain.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('enqueue', 'drain', 'status', 'recover')]
    [string]$Action,

    [ValidateSet('transcribe', 'transcribe-cues')]
    [string]$JobType = 'transcribe',
    [string]$Media,
    [string]$Manifest,
    [string]$Cues,
    [double]$Padding = 1.25,
    [string]$Output,
    [string]$Metadata,
    [string]$Start,
    [string]$End,
    [string]$Model = 'large-v3-turbo',
    [string]$Language = 'ja',
    [ValidateSet('cuda', 'cpu', 'auto')]
    [string]$Device = 'cuda',
    [string]$ComputeType = 'float16',
    [int]$BeamSize = 5,
    [switch]$VadFilter,
    [switch]$WordTimestamps,
    [switch]$NoConditionOnPreviousText,
    [string]$InitialPrompt,
    [switch]$LocalFilesOnly,
    [string]$QueueRoot,
    [ValidateRange(1, 10080)]
    [int]$StaleAfterMinutes = 30,
    [ValidateRange(0, 2147483647)]
    [int]$MaxJobs = 0,
    [string]$Python = 'python',
    [string]$Subflow
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $QueueRoot) {
    $QueueRoot = Join-Path $projectRoot '.runtime\retranscription-queue'
}
if (-not $Subflow) {
    $Subflow = Join-Path $projectRoot 'subflow.py'
}

$queueRootPath = [System.IO.Path]::GetFullPath($QueueRoot)
$pendingDir = Join-Path $queueRootPath 'pending'
$runningDir = Join-Path $queueRootPath 'running'
$doneDir = Join-Path $queueRootPath 'done'
$failedDir = Join-Path $queueRootPath 'failed'
$identityDir = Join-Path $queueRootPath 'identities'
foreach ($directory in @($pendingDir, $runningDir, $doneDir, $failedDir, $identityDir)) {
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
}

function Get-Sha256([string]$Value) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $hash = [System.Security.Cryptography.SHA256]::HashData($bytes)
    return [System.Convert]::ToHexString($hash).ToLowerInvariant()
}

function Get-NormalizedPath([string]$Value) {
    if (-not $Value) { return $null }
    return [System.IO.Path]::GetFullPath($Value)
}

function Convert-TimeToMilliseconds([string]$Value) {
    if (-not $Value) { return $null }
    $text = $Value.Trim().Replace(',', '.')
    [double]$secondsValue = 0
    if ([double]::TryParse(
        $text,
        [System.Globalization.NumberStyles]::Float,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [ref]$secondsValue
    )) {
        return [int][Math]::Round($secondsValue * 1000)
    }
    $match = [regex]::Match($text, '^(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?$')
    if (-not $match.Success) {
        throw "Invalid time value: $Value"
    }
    $hours = if ($match.Groups[1].Success) { [int]$match.Groups[1].Value } else { 0 }
    $minutes = [int]$match.Groups[2].Value
    $seconds = [int]$match.Groups[3].Value
    if ($seconds -ge 60 -or ($match.Groups[1].Success -and $minutes -ge 60)) {
        throw "Invalid time value: $Value"
    }
    $millisText = $match.Groups[4].Value.PadRight(3, '0')
    $millis = if ($millisText) { [int]$millisText } else { 0 }
    return (((($hours * 60) + $minutes) * 60 + $seconds) * 1000 + $millis)
}

function Get-SelectedCueIndexes([string]$Selection, [object[]]$ManifestCues) {
    $valid = @{}
    foreach ($cue in $ManifestCues) {
        $valid[[int]$cue.index] = $true
    }
    $selected = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($rawToken in $Selection.Split(',')) {
        $token = $rawToken.Trim()
        if (-not $token) { continue }
        if ($token -notmatch '^\d+(?:-\d+)?$') {
            throw "Invalid cue selection token: $token"
        }
        if ($token.Contains('-')) {
            $parts = $token.Split('-', 2)
            $left = [int]$parts[0]
            $right = [int]$parts[1]
            if ($left -gt $right) {
                $temporary = $left
                $left = $right
                $right = $temporary
            }
            foreach ($index in $left..$right) { [void]$selected.Add($index) }
        }
        else {
            [void]$selected.Add([int]$token)
        }
    }
    if ($selected.Count -eq 0) {
        throw 'Cue selection is empty.'
    }
    foreach ($index in $selected) {
        if (-not $valid.ContainsKey($index)) {
            throw "Unknown cue index: $index"
        }
    }
    return @($selected | Sort-Object)
}

function Find-ExistingJob([string]$JobId) {
    foreach ($state in @('pending', 'running', 'done')) {
        $directory = Join-Path $queueRootPath $state
        $match = Get-ChildItem -LiteralPath $directory -Filter "*-$JobId.json" -File -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($match) {
            return [pscustomobject]@{ state = $state; path = $match.FullName }
        }
    }
    return $null
}

function Get-ExistingDelivery([string]$Path) {
    try {
        $payload = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        $outputValue = if ($payload.PSObject.Properties.Name -contains 'output') {
            $payload.output
        } elseif ($payload.PSObject.Properties.Name -contains 'first_output') {
            $payload.first_output
        } else { $null }
        $metadataValue = if ($payload.PSObject.Properties.Name -contains 'metadata') {
            $payload.metadata
        } elseif ($payload.PSObject.Properties.Name -contains 'first_metadata') {
            $payload.first_metadata
        } else { $null }
        return [pscustomobject]@{ output = $outputValue; metadata = $metadataValue }
    }
    catch {
        return [pscustomobject]@{ output = $null; metadata = $null }
    }
}

function Write-JsonAtomic([string]$Path, [object]$Value) {
    $temporary = "$Path.$PID.tmp"
    $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Write-JsonCreateNew([string]$Path, [object]$Value) {
    $json = $Value | ConvertTo-Json -Depth 8
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::Read
    )
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
}

if ($Action -eq 'enqueue') {
    if (-not $Output -or -not $Metadata) {
        throw 'enqueue requires -Output and -Metadata.'
    }
    $manifestPath = $null
    $manifestSha256 = $null
    $manifestId = $null
    $manifestVideoSha256 = $null
    $normalizedCues = $null
    $rangeStartMs = $null
    $rangeEndMs = $null
    if ($JobType -eq 'transcribe-cues') {
        if (-not $Manifest -or -not $Cues) {
            throw 'transcribe-cues enqueue requires -Manifest and -Cues.'
        }
        if ($Padding -lt 0) {
            throw 'Padding cannot be negative.'
        }
        $manifestPath = Get-NormalizedPath $Manifest
        $manifestPayload = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        $manifestSha256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $manifestId = if ($manifestPayload.PSObject.Properties.Name -contains 'manifest_id') {
            [string]$manifestPayload.manifest_id
        } else { $null }
        $manifestVideoSha256 = if (
            $manifestPayload.source.PSObject.Properties.Name -contains 'video_sha256'
        ) { [string]$manifestPayload.source.video_sha256 } else { $null }
        $selectedIndexes = @(Get-SelectedCueIndexes $Cues @($manifestPayload.cues))
        $selectedCues = @($manifestPayload.cues | Where-Object { $selectedIndexes -contains [int]$_.index })
        $paddingMs = [int][Math]::Round($Padding * 1000)
        $rangeStartMs = [Math]::Max(0, [int](($selectedCues.start_ms | Measure-Object -Minimum).Minimum) - $paddingMs)
        $rangeEndMs = [int](($selectedCues.end_ms | Measure-Object -Maximum).Maximum) + $paddingMs
        $normalizedCues = $selectedIndexes -join ','
        $mediaPath = Get-NormalizedPath ([string]$manifestPayload.source.video)
    }
    else {
        if (-not $Media) {
            throw 'transcribe enqueue requires -Media.'
        }
        $mediaPath = Get-NormalizedPath $Media
        $rangeStartMs = if ($Start) { Convert-TimeToMilliseconds $Start } else { 0 }
        $rangeEndMs = if ($End) { Convert-TimeToMilliseconds $End } else { $null }
    }
    if ($rangeStartMs -lt 0) {
        throw 'Transcription start cannot be negative.'
    }
    if ($null -ne $rangeEndMs -and $rangeEndMs -le $rangeStartMs) {
        throw 'Transcription end must be later than start.'
    }
    if (-not (Test-Path -LiteralPath $mediaPath -PathType Leaf)) {
        throw "Media not found: $mediaPath"
    }
    if ($manifestVideoSha256) {
        $mediaFingerprint = "sha256:$($manifestVideoSha256.ToLowerInvariant())"
    }
    else {
        $mediaItem = Get-Item -LiteralPath $mediaPath
        $mediaFingerprint = "size-mtime:$($mediaItem.Length):$($mediaItem.LastWriteTimeUtc.Ticks)"
    }

    $identity = [ordered]@{
        media = $mediaPath.ToLowerInvariant()
        media_fingerprint = $mediaFingerprint
        range_start_ms = $rangeStartMs
        range_end_ms = $rangeEndMs
        model = $Model
        language = $Language
        device = $Device
        compute_type = $ComputeType
        beam_size = $BeamSize
        vad_filter = [bool]$VadFilter
        word_timestamps = [bool]$WordTimestamps
        no_condition_on_previous_text = [bool]$NoConditionOnPreviousText
        initial_prompt = $InitialPrompt
        local_files_only = [bool]$LocalFilesOnly
    }
    $jobId = Get-Sha256 ($identity | ConvertTo-Json -Compress)
    $existing = Find-ExistingJob $jobId
    if ($existing) {
        $delivery = Get-ExistingDelivery $existing.path
        [pscustomobject]@{
            enqueued = $false
            duplicate = $true
            job_id = $jobId
            state = $existing.state
            path = $existing.path
            existing_output = $delivery.output
            existing_metadata = $delivery.metadata
        } | ConvertTo-Json
        exit 0
    }

    $job = [ordered]@{
        schema_version = 1
        job_id = $jobId
        created_at = [DateTimeOffset]::Now.ToString('o')
        identity = $identity
        job_type = $JobType
        media = $mediaPath
        manifest = $manifestPath
        manifest_sha256 = $manifestSha256
        manifest_id = $manifestId
        manifest_video_sha256 = $manifestVideoSha256
        cues = $normalizedCues
        padding = if ($JobType -eq 'transcribe-cues') { $Padding } else { $null }
        start = $Start
        end = $End
        output = Get-NormalizedPath $Output
        metadata = Get-NormalizedPath $Metadata
    }
    $stamp = [DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')
    $jobPath = Join-Path $pendingDir "$stamp-$jobId.json"
    $identityPath = Join-Path $identityDir "$jobId.json"
    try {
        Write-JsonCreateNew $identityPath ([ordered]@{
            schema_version = 1
            job_id = $jobId
            created_at = [DateTimeOffset]::Now.ToString('o')
            identity = $identity
            first_output = $job.output
            first_metadata = $job.metadata
        })
    }
    catch {
        if (-not (Test-Path -LiteralPath $identityPath -PathType Leaf)) {
            throw
        }
        $existing = Find-ExistingJob $jobId
        $existingPath = if ($existing) { $existing.path } else { $identityPath }
        $delivery = Get-ExistingDelivery $existingPath
        [pscustomobject]@{
            enqueued = $false
            duplicate = $true
            job_id = $jobId
            state = if ($existing) { $existing.state } else { 'reserved' }
            path = $existingPath
            existing_output = $delivery.output
            existing_metadata = $delivery.metadata
        } | ConvertTo-Json
        exit 0
    }
    try {
        Write-JsonAtomic $jobPath $job
    }
    catch {
        Remove-Item -LiteralPath $identityPath -ErrorAction SilentlyContinue
        throw
    }
    [pscustomobject]@{
        enqueued = $true
        duplicate = $false
        job_id = $jobId
        state = 'pending'
        path = $jobPath
        output = $job.output
        metadata = $job.metadata
    } | ConvertTo-Json
    exit 0
}

if ($Action -eq 'status') {
    $summary = [ordered]@{}
    foreach ($state in @('pending', 'running', 'done', 'failed')) {
        $files = @(Get-ChildItem -LiteralPath (Join-Path $queueRootPath $state) -Filter '*.json' -File)
        $summary[$state] = $files.Count
    }
    [pscustomobject]$summary | ConvertTo-Json
    exit 0
}

if ($Action -eq 'recover') {
    $recoveryLockPath = Join-Path $queueRootPath 'worker.lock'
    $recoveryLock = $null
    try {
        try {
            $recoveryLock = [System.IO.File]::Open(
                $recoveryLockPath,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
        }
        catch [System.IO.IOException] {
            throw 'Cannot recover while a retranscription queue worker is running.'
        }

        $cutoff = [DateTime]::UtcNow.AddMinutes(-$StaleAfterMinutes)
        $recoveredRunning = 0
        $removedReservations = 0
        foreach ($runningFile in @(Get-ChildItem -LiteralPath $runningDir -Filter '*.json' -File)) {
            if ($runningFile.LastWriteTimeUtc -ge $cutoff) { continue }
            $job = Get-Content -LiteralPath $runningFile.FullName -Raw | ConvertFrom-Json
            $job | Add-Member -NotePropertyName recovered_at -NotePropertyValue ([DateTimeOffset]::Now.ToString('o')) -Force
            $job | Add-Member -NotePropertyName recovery_reason -NotePropertyValue 'stale running job found without an active queue worker' -Force
            $job | Add-Member -NotePropertyName exit_code -NotePropertyValue 2 -Force
            Write-JsonAtomic $runningFile.FullName $job
            $failedPath = Join-Path $failedDir $runningFile.Name
            Move-Item -LiteralPath $runningFile.FullName -Destination $failedPath
            Remove-Item -LiteralPath (Join-Path $identityDir "$($job.job_id).json") -ErrorAction SilentlyContinue
            $recoveredRunning++
        }
        foreach ($identityFile in @(Get-ChildItem -LiteralPath $identityDir -Filter '*.json' -File)) {
            if ($identityFile.LastWriteTimeUtc -ge $cutoff) { continue }
            $jobId = $identityFile.BaseName
            if (-not (Find-ExistingJob $jobId)) {
                Remove-Item -LiteralPath $identityFile.FullName
                $removedReservations++
            }
        }
        [pscustomobject]@{
            recovered_running = $recoveredRunning
            removed_stale_reservations = $removedReservations
            stale_after_minutes = $StaleAfterMinutes
        } | ConvertTo-Json
    }
    finally {
        if ($recoveryLock) { $recoveryLock.Dispose() }
    }
    exit 0
}

$lockPath = Join-Path $queueRootPath 'worker.lock'
$lockStream = $null
$mediaLockStream = $null
$processedJobs = [System.Collections.Generic.List[object]]::new()
try {
    try {
        $lockStream = [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    }
    catch [System.IO.IOException] {
        throw 'Another retranscription queue worker is already running.'
    }

    $runtimeDirectory = Join-Path $projectRoot '.runtime'
    [System.IO.Directory]::CreateDirectory($runtimeDirectory) | Out-Null
    $mediaLockPath = Join-Path $runtimeDirectory 'media-worker.lock'
    try {
        $mediaLockStream = [System.IO.File]::Open(
            $mediaLockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    }
    catch [System.IO.IOException] {
        throw 'Another Whisper transcription or Silero VAD media job is already running.'
    }

    $processedCount = 0
    while ($true) {
        if ($MaxJobs -gt 0 -and $processedCount -ge $MaxJobs) { break }
        $next = Get-ChildItem -LiteralPath $pendingDir -Filter '*.json' -File |
            Sort-Object Name |
            Select-Object -First 1
        if (-not $next) { break }

        $runningPath = Join-Path $runningDir $next.Name
        try {
            Move-Item -LiteralPath $next.FullName -Destination $runningPath
        }
        catch {
            continue
        }

        $job = Get-Content -LiteralPath $runningPath -Raw | ConvertFrom-Json
        $resolvedJobType = if (
            $job.PSObject.Properties.Name -contains 'job_type' -and $job.job_type
        ) { [string]$job.job_type } else { 'transcribe' }
        if ($resolvedJobType -eq 'transcribe-cues') {
            $arguments = @(
                $Subflow, 'transcribe-cues',
                '--manifest', [string]$job.manifest,
                '--cues', [string]$job.cues,
                '--padding', ([double]$job.padding).ToString([System.Globalization.CultureInfo]::InvariantCulture)
            )
        }
        else {
            $arguments = @($Subflow, 'transcribe', [string]$job.media)
            $startValue = if ($job.PSObject.Properties.Name -contains 'start') {
                $job.start
            } elseif ($job.identity.PSObject.Properties.Name -contains 'start') {
                $job.identity.start
            } else { $null }
            $endValue = if ($job.PSObject.Properties.Name -contains 'end') {
                $job.end
            } elseif ($job.identity.PSObject.Properties.Name -contains 'end') {
                $job.identity.end
            } else { $null }
            if ($startValue) { $arguments += @('--start', [string]$startValue) }
            if ($endValue) { $arguments += @('--end', [string]$endValue) }
        }
        $arguments += @(
            '--output', [string]$job.output,
            '--metadata', [string]$job.metadata,
            '--model', [string]$job.identity.model,
            '--language', [string]$job.identity.language,
            '--device', [string]$job.identity.device,
            '--compute-type', [string]$job.identity.compute_type,
            '--beam-size', [string]$job.identity.beam_size,
            '--force'
        )
        if ($job.identity.vad_filter) { $arguments += '--vad-filter' }
        if ($job.identity.word_timestamps) { $arguments += '--word-timestamps' }
        if ($job.identity.no_condition_on_previous_text) { $arguments += '--no-condition-on-previous-text' }
        if ($job.identity.initial_prompt) { $arguments += @('--initial-prompt', [string]$job.identity.initial_prompt) }
        if ($job.identity.local_files_only) { $arguments += '--local-files-only' }

        $startedAt = [DateTimeOffset]::Now
        $job | Add-Member -NotePropertyName worker_pid -NotePropertyValue $PID -Force
        $job | Add-Member -NotePropertyName worker_host -NotePropertyValue ([Environment]::MachineName) -Force
        $job | Add-Member -NotePropertyName worker_started_at -NotePropertyValue $startedAt.ToString('o') -Force
        Write-JsonAtomic $runningPath $job
        $preflightError = $null
        if (
            $resolvedJobType -eq 'transcribe-cues' -and
            $job.PSObject.Properties.Name -contains 'manifest_sha256' -and
            $job.manifest_sha256
        ) {
            if (-not (Test-Path -LiteralPath $job.manifest -PathType Leaf)) {
                $preflightError = 'Queued transcribe-cues manifest no longer exists.'
            }
            else {
                $currentManifestHash = (Get-FileHash -LiteralPath $job.manifest -Algorithm SHA256).Hash.ToLowerInvariant()
                if ($currentManifestHash -ne [string]$job.manifest_sha256) {
                    $preflightError = 'Queued transcribe-cues manifest changed after enqueue; refusing a different range.'
                }
            }
        }
        if ($preflightError) {
            $exitCode = 2
            $job | Add-Member -NotePropertyName error -NotePropertyValue $preflightError -Force
            Write-Warning $preflightError
        }
        else {
            & $Python @arguments
            $exitCode = $LASTEXITCODE
        }
        $job | Add-Member -NotePropertyName started_at -NotePropertyValue $startedAt.ToString('o') -Force
        $job | Add-Member -NotePropertyName finished_at -NotePropertyValue ([DateTimeOffset]::Now.ToString('o')) -Force
        $job | Add-Member -NotePropertyName exit_code -NotePropertyValue $exitCode -Force

        $destinationDirectory = if ($exitCode -eq 0) { $doneDir } else { $failedDir }
        $destination = Join-Path $destinationDirectory $next.Name
        Write-JsonAtomic "$runningPath.result" $job
        Remove-Item -LiteralPath $runningPath
        Move-Item -LiteralPath "$runningPath.result" -Destination $destination

        if ($exitCode -ne 0) {
            Remove-Item -LiteralPath (Join-Path $identityDir "$($job.job_id).json") -ErrorAction SilentlyContinue
            throw "Retranscription failed for job $($job.job_id) with exit code $exitCode."
        }
        $processedJobs.Add([pscustomobject]@{
            job_id = [string]$job.job_id
            job_type = $resolvedJobType
            output = [string]$job.output
            metadata = [string]$job.metadata
        }) | Out-Null
        $processedCount++
    }
}
finally {
    if ($mediaLockStream) { $mediaLockStream.Dispose() }
    if ($lockStream) { $lockStream.Dispose() }
}

$status = (& $PSCommandPath -Action status -QueueRoot $queueRootPath) | ConvertFrom-Json
[pscustomobject]@{
    pending = [int]$status.pending
    running = [int]$status.running
    done = [int]$status.done
    failed = [int]$status.failed
    processed_jobs = @($processedJobs)
} | ConvertTo-Json -Depth 5
