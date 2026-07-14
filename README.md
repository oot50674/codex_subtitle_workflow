# Subtitle Workflow

영상의 음성을 확인해 초벌 자막을 교정하고 한국어 번역 자막을 만드는
에이전트 기반 작업 도구입니다.

사용자는 영상이나 YouTube 주소, 그리고 가지고 있는 SRT를 제공하면 됩니다.
에이전트가 파일별로 전사, 번역, 검토, 검증을 순서대로 진행합니다. 사용자가 내부 로직이나 개별
명령을 모두 알 필요는 없습니다.

## 한눈에 보는 작업 흐름

```text
영상 또는 YouTube 주소를 전사 대기열에 등록
  ↓
파일 하나의 신뢰할 수 있는 초벌 SRT 준비
  ├─ SRT가 있으면 바로 준비(prepare)
  └─ 없으면 Whisper 전체 전사 후 준비
  ↓
에이전트가 번역, ASR 후보 검토, 증거 확인·교정 수행
  ↓
원문·번역 교정 및 필요한 싱크 조정 → 자동 검증
  ↓
검증된 한국어 SRT 발행
```

## 사용자가 준비할 것

다음 중 가능한 자료를 에이전트에게 알려주면 됩니다.

- 로컬 영상 파일 경로 또는 YouTube URL
- 직접 만들었거나 신뢰할 수 있는 원문 SRT
- 원문 언어와 번역할 언어
- 고유명사, 선호 용어, 말투처럼 반드시 지켜야 할 사항
- 결과 자막을 저장할 위치에 대한 별도 요구

신뢰할 수 있는 SRT가 없다면 에이전트가 `large-v3-turbo` 모델로 영상 전체를
전사합니다. YouTube가 제공하는 자막과 자동자막은 전사 원본으로 사용하지
않습니다.

## 처음 사용할 때

Windows 10 이상, Python 3.10 이상, PowerShell Core(`pwsh`)가 필요합니다.
영상 처리를 위해 FFmpeg와 FFprobe도 사용합니다.

에이전트는 작업을 시작하기 전에 다음 상태를 확인합니다.

1. FFmpeg를 사용할 수 있는지 확인
2. 전사가 필요하면 전용 Whisper 환경 확인
3. YouTube 영상이 필요하면 전용 다운로드 환경 확인

필요한 프로그램이나 모델을 새로 내려받아야 할 때는 먼저 사용자에게 승인을
받습니다. 설치된 전용 환경과 모델은 프로젝트의 `.runtime` 폴더에 보관되며
Git에는 포함되지 않습니다.

## 에이전트가 하는 일

### 1. 입력 자료 준비

YouTube URL을 받으면 영상과 오디오만 내려받습니다. URL에 재생목록 정보가
포함되어 있어도 기본적으로 해당 영상 하나만 받습니다. 사용자가 재생목록
전체를 요청한 경우에만 전체를 내려받습니다.

신뢰할 수 있는 SRT가 없으면 내려받은 영상이나 로컬 영상 전체를 Whisper로
전사합니다.

### 2. 작업 폴더 생성

영상마다 새로운 작업 폴더를 만들고 영상 정보, 자막 내용, 자막별 시작·종료
시각을 정리합니다. 기존 작업과 관련이 있다면 `doc/index.json`에서 이전 작업
기록과 용어를 확인합니다.

### 3. 번역과 검토

한 파일의 전체 전사와 `prepare`가 끝나면 에이전트가 모든 자막을 번역하면서
반복 문장, 이상한 기술 용어, ASR 환각 의심 구간을 메모합니다. 배치 작업에서는
현재 파일의 번역과 검토를 마친 뒤 다음 파일로 넘어갑니다.

### 4. 교정·싱크

에이전트는 모든 자막을 원문과 앞뒤 문맥에 맞춰 검토합니다. 의심 구간은 필요한
음성, 영상 프레임, 짧은 영상 클립, 앞뒤 자막, Whisper 재전사 결과를 함께
확인합니다.

다음 작업을 수행합니다.

- 잘못 전사된 원문과 오역 교정
- 영상 전체의 용어·고유명사·말투 통일
- 읽기 어려운 문장 축약과 줄바꿈 조정
- 확신이 낮은 후보의 추가 증거 확인
- 실제 근거가 있는 경우에만 자막 타이밍 조정

싱크 분석 결과는 참고 자료일 뿐 자동으로 타이밍을 바꾸는 기준은 아닙니다.
실제 음성과 화면에서 근거를 찾았을 때만 원문이나 타이밍을 수정합니다.

싱크가 의심되는 작업에서는 준비된 16 kHz 음원을 Silero VAD로 한 번 분석한
뒤, 전체 발화 구간과 모든 자막 엔트리를 비교합니다. 이 분석은 다음 항목을
점수와 개별 근거로 보고합니다.

- 발화와 거의 겹치지 않는 자막
- 자막 안의 긴 앞·뒤 무음
- 자막 사이 빈 구간에 남은 발화
- 어느 자막에도 포함되지 않은 발화
- 한 발화를 여러 자막이 공유하거나 한 자막에 여러 발화가 있는 그룹

점수는 검토 순서를 정할 뿐 타임코드를 자동으로 바꾸지 않습니다. 에이전트는
앞뒤 자막과 실제 음성·영상을 확인한 뒤에만 변경을 승인합니다.
보고서는 중간 이상 점수의 큐와 완전히 무자막인 발화를 1차 후보로 분리하고,
이미 자막이 붙은 발화의 짧은 앞뒤 조각이나 낮은 점수는 2차 참고 후보로 남겨
검토량이 불필요하게 늘지 않게 합니다.

한국어 자막은 가능하면 한 자막 구간당 한두 줄로 만듭니다. 자연스럽게 보이게 하려는
이유만으로 원문의 의미나 타이밍을 임의로 바꾸지 않습니다.

싱크 오류가 확인되어 타이밍을 고친 항목은 최종 파일을 만들 때 표시 여유를 더할
수 있습니다. 기본값은 발화 시작 전 0.3초, 발화 종료 후 0.8초이며 필요하면 각각
최대 3초까지 조정합니다. 자막 사이 간격이 부족하면 양쪽 여유를 비례해서 줄이므로
자막끼리 겹치지 않습니다. 이 처리는 `timing`이 들어 있는 항목에만 적용되고,
원본 결정 파일은 바뀌지 않습니다.

### 5. 자동 검증

검토가 끝나면 다음 문제를 검사합니다.

- 빠졌거나 비어 있는 자막
- 잘못된 자막 순서와 시간 범위
- 지나치게 길거나 빠르게 지나가는 자막
- 한국어 번역이 들어가지 않은 항목
- 짧은 동일 문장이 반복되는 등 전사 오류가 의심되는 구간

오류가 발견되면 해당 구간의 음성과 화면을 다시 확인합니다. 모든 필수 검사를
통과한 결과만 발행합니다.

### 6. 결과 발행과 기록

최종 결과는 다음 구조로 정리됩니다.

```text
output\YYYY-MM-DD\HHmmss
```

기본적으로 원본 영상 옆에도 번역 자막을 저장합니다.

```text
video.mp4
video.ko.srt
```

작업에 사용한 입력, 판단 결과, 출력 파일 경로와 해시는 `run.json`과 `doc/`
기록에 남깁니다. 따라서 나중에 어떤 영상과 자막으로 만든 결과인지 확인할 수
있습니다.

## 에이전트에게 요청하는 예시

```text
D:\media\tutorial.mp4를 한국어 자막으로 만들어줘.
원문 SRT는 없으니 영상 전체를 전사해서 진행해줘.
```

```text
이 YouTube 영상을 한국어로 번역해줘:
https://www.youtube.com/watch?v=VIDEO_ID
```

```text
D:\media\video.mp4와 D:\media\draft.en.srt를 사용해서
오역과 싱크를 검토하고 한국어 SRT를 만들어줘.
```

이후 에이전트가 파일별로 전사, 번역, 검토, 검증을 순서대로 진행합니다.
다운로드나 런타임 설치처럼 외부 파일을 받아야 하는 단계에서만 사용자 승인을
요청합니다.

## 직접 실행할 때 참고할 명령

일반적으로는 에이전트가 실행하므로 아래 명령을 외울 필요가 없습니다.

```powershell
# FFmpeg 상태 확인
python -X utf8 .\subflow.py doctor

# Whisper 환경 확인
python -X utf8 .\subflow.py whisper-doctor

# 최초 Whisper 환경 설치: 다운로드 승인 후 실행
python -X utf8 .\subflow.py whisper-doctor --install-runtime

# YouTube 환경 확인
python -X utf8 .\subflow.py youtube-doctor

# 최초 YouTube 환경 설치: 다운로드 승인 후 실행
python -X utf8 .\subflow.py youtube-doctor --install-runtime

# 여러 전사 작업 중 한 파일만 처리해 즉시 번역 단계로 넘기기
pwsh -NoLogo -NoProfile -File .\scripts\retranscription_queue.ps1 `
  -Action drain -MaxJobs 1

# 검토 결과를 decisions 파일로 병합
python -X utf8 .\subflow.py merge `
  --manifest .\work\example\manifest.json `
  --parts .\work\example\decisions.part1.json `
  --output .\work\example\decisions.json

# 준비된 작업의 Silero VAD 싱크 의심 후보 생성
pwsh -NoLogo -NoProfile -File .\scripts\silero_sync_audit.ps1 `
  -Manifest .\work\example\manifest.json

# 검토된 타이밍에 패딩을 적용하고 apply + verify까지 실행
# Output은 아직 존재하지 않는 새 폴더여야 합니다.
pwsh -NoLogo -NoProfile -File .\scripts\finalize_sync_subtitles.ps1 `
  -Manifest .\work\example\manifest.json `
  -Decisions .\work\example\decisions.sync-reviewed.json `
  -Output .\work\example\sync-final-padded `
  -StartPadMs 300 `
  -EndPadMs 800
```

세부 검토·번역 규칙은 `AGENT_PROTOCOL.md`, 에이전트 진입 지침은
`AGENTS.md`에 있습니다.
