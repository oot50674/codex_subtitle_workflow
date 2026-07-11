# Subtitle Workflow

영상과 초벌 SRT를 근거로 오탈자·싱크를 검토하고 한국어 번역 자막을
생성하는 agent-in-the-loop CLI입니다. 미디어 처리와 검증은 CLI가 맡고,
의미 교정·번역·타이밍 판단은 에이전트가 증거를 보고 결정합니다.

## 구성

- `subflow.py`: 메인 CLI
- `AGENTS.md`: 에이전트가 먼저 읽는 진입 지침
- `AGENT_PROTOCOL.md`: 전체 검토·번역·검증 프로토콜
- `doc/`: 빈 인덱스, 작업 기록 템플릿, 향후 축적할 지식 문서 구조

배포본에는 이전 영상, 자막, 작업 폴더, 발행 결과와 과거 작업 문서가
포함되지 않습니다.

## 요구 사항

- Windows 10 이상
- Python 3.10 이상
- PowerShell Core (`pwsh`)
- FFmpeg와 FFprobe

FFmpeg는 고정 경로를 가정하지 않습니다. 다음 순서로 찾습니다.

1. 명시한 `--ffmpeg-root`
2. `SUBFLOW_FFMPEG_ROOT` 또는 `FFMPEG_ROOT`
3. 프로세스 `PATH`의 `ffmpeg`와 `ffprobe`
4. 이 도구가 설치한 사용자 로컬 FFmpeg

```powershell
python -X utf8 .\subflow.py doctor
```

찾지 못했을 때만 다음 명령으로 Gyan의 Windows Essentials ZIP을 내려받아
SHA-256을 검증하고 `%LOCALAPPDATA%\SubtitleWorkflow\ffmpeg`에 설치합니다.
설치된 `bin` 폴더는 현재 프로세스와 사용자 `PATH`에 추가됩니다.

```powershell
python -X utf8 .\subflow.py doctor --install-ffmpeg
```

## 기본 작업 흐름

```powershell
$Python = 'python'
$Tool = Join-Path $PWD 'subflow.py'
$Video = 'D:\media\video.mp4'
$Draft = 'D:\media\draft.srt'
$Work = 'D:\subtitle-jobs\video-01'
$OutputRoot = 'D:\subtitle-output'

& $Python -X utf8 $Tool prepare $Video $Draft `
  --workdir $Work `
  --source-language en `
  --target-language ko

& $Python -X utf8 $Tool sync `
  --manifest "$Work\manifest.json" `
  --output "$Work\sync_analysis.json"

& $Python -X utf8 $Tool evidence `
  --manifest "$Work\manifest.json" `
  --cues '3,8-12,42' `
  --output "$Work\evidence"
```

에이전트는 `AGENT_PROTOCOL.md`에 따라 증거를 검토하고 번역 맵과 확정
교정 파일을 작성합니다. 이후 다음과 같이 병합·적용·검증·발행합니다.

```powershell
& $Python -X utf8 $Tool merge `
  --manifest "$Work\manifest.json" `
  --translation-map "$Work\translations.json" `
  --overrides "$Work\overrides.json" `
  --source-preserving `
  --output "$Work\decisions.json"

& $Python -X utf8 $Tool apply `
  --manifest "$Work\manifest.json" `
  --decisions "$Work\decisions.json" `
  --output "$Work\applied"

& $Python -X utf8 $Tool verify `
  --manifest "$Work\manifest.json" `
  --output "$Work\applied"

& $Python -X utf8 $Tool publish `
  --manifest "$Work\manifest.json" `
  --decisions "$Work\decisions.json" `
  --source-output "$Work\applied" `
  --output-root $OutputRoot
```

발행 결과는 `output\YYYY-MM-DD\HHmmss` 구조로 정리됩니다. 한국어 자막은
가능하면 한 cue당 1~2줄로 압축하고, 실제 음성·화면 근거가 있을 때만
타이밍을 변경합니다.
