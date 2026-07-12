# Subtitle Workflow

영상과 초벌 SRT를 근거로 오탈자·싱크를 검토하고 한국어 번역 자막을
생성하는 agent-in-the-loop CLI입니다. 미디어 처리와 검증은 CLI가 맡고,
의미 교정·번역·타이밍 판단은 에이전트가 증거를 보고 결정합니다.

## 구성

- `subflow.py`: 메인 CLI
- `whisper_runtime/`: 격리된 faster-whisper worker와 고정 의존성
- `AGENTS.md`: 에이전트가 먼저 읽는 진입 지침
- `AGENT_PROTOCOL.md`: 전체 검토·번역·검증 프로토콜
- `doc/`: 빈 인덱스, 작업 기록 템플릿, 향후 축적할 지식 문서 구조

## 요구 사항

- Windows 10 이상
- Python 3.10 이상
- PowerShell Core (`pwsh`)
- FFmpeg와 FFprobe
- NVIDIA GPU 사용 시 호환되는 CUDA/cuDNN 런타임

FFmpeg는 다음 순서로 찾습니다.

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

## Whisper 전사

Whisper 전사는 프로젝트 내부의 전용 Python 환경과 `faster-whisper`
worker를 사용합니다.

- 가상환경: `.runtime\whisper\venv`
- 모델 캐시: `.runtime\whisper\models`
- 두 경로 모두 Git에서 제외

최초 한 번, 사용자의 다운로드 승인을 받은 뒤 전용 환경을 설치합니다.

```powershell
python -X utf8 .\subflow.py whisper-doctor --install-runtime
```

설치 이후 상태와 캐시된 모델을 확인할 때는 다운로드 없이 실행합니다.

```powershell
python -X utf8 .\subflow.py whisper-doctor
```

전체 영상을 영어로 전사하는 예시입니다. 모델이 캐시에 없으면 최초 실행
중 웹에서 `.runtime\whisper\models\<모델명>`으로 자동 다운로드합니다.
`large-v3-turbo`의 주 모델 파일은
`.runtime\whisper\models\large-v3-turbo\model.bin`에 저장됩니다.

```powershell
python -X utf8 .\subflow.py transcribe "D:\media\video.mp4" `
  --model large-v3-turbo `
  --language en `
  --output ".\work\video-01\draft.en.srt"
```

특정 시간 범위만 재전사하면 결과 SRT의 타임코드는 원본 영상 기준으로
복원됩니다. `--keep-audio`를 지정하면 실제로 Whisper에 전달한 16 kHz
모노 WAV도 증거로 보존합니다.

```powershell
python -X utf8 .\subflow.py transcribe "D:\media\video.mp4" `
  --start 00:12:30.000 `
  --end 00:13:10.000 `
  --model large-v3-turbo `
  --language en `
  --keep-audio `
  --output ".\work\video-01\check-1230.en.srt"
```

이미 manifest가 있으면 인접 cue 범위를 패딩과 함께 바로 재전사할 수
있습니다. 멀리 떨어진 cue는 한 번에 묶지 말고 별도 호출합니다.

```powershell
python -X utf8 .\subflow.py transcribe-cues `
  --manifest ".\work\video-01\manifest.json" `
  --cues "8-12" `
  --padding 1.25 `
  --model large-v3-turbo `
  --language en `
  --output ".\work\video-01\evidence\cues-0008-0012.en.srt"
```

모든 전사는 SRT 옆에 `*.transcription.json`을 생성합니다. 이 파일에는
소스 해시, 원본 기준 구간, 모델·장치·언어, 실행 옵션, 절대 타임코드와
세그먼트 신뢰도 정보가 기록됩니다. 모델 다운로드를 금지하고 캐시만
검사하려면 `--local-files-only`를 사용합니다.

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

발행 결과는 `output\YYYY-MM-DD\HHmmss` 구조로 정리됩니다. 동시에 번역
자막을 원본 영상 폴더에 `<영상 이름>.<대상 언어>.srt`로 저장합니다. 예를
들어 대상 언어가 한국어면 `video.ko.srt`가 생성되며, 경로와 SHA-256은
`run.json`에 기록됩니다. 원본 옆 저장을 원하지 않을 때만 publish에
`--no-source-sidecar`를 지정합니다.

한국어 자막은 가능하면 한 cue당 1~2줄로 압축하고, 실제 음성·화면 근거가
있을 때만 타이밍을 변경합니다.
