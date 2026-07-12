# Whisper-WebUI — AI 에이전트용 사용 안내

> 이 문서는 외부 Whisper-WebUI 연동을 위한 상위 프로젝트 참고 자료입니다.
> 이 저장소의 에이전트는 별도 백엔드를 기동하지 않고
> `subflow.py whisper-doctor`, `transcribe`, `transcribe-cues`를 우선 사용합니다.

최초 접속 시 이 문서를 한 번 읽고, 이후 전사/자막 작업은 아래 규칙만 따르면 됩니다.  
Gradio Web UI는 사용하지 마세요. **CLI를 우선**하고, 필요하면 REST로 대체하세요.

동기화 위치:
- 문서 원본: `backend/cli/AGENT.md`
- CLI: `python -m backend.cli --agent-help`
- API: `GET /subtitle/agent-instructions`

---

## 목표

오디오/비디오 로컬 경로를 입력으로 받아, 사람 개입 없이 **자막 파일**을 확보한다.

---

## 사전 조건

백엔드가 실행 중이어야 합니다.

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

기본 Base URL: `http://127.0.0.1:8000`  
환경변수 `WHISPER_BACKEND_URL`로 변경 가능합니다.

Swagger: `http://127.0.0.1:8000/docs`

---

## 권장 호출: CLI

```bash
python -m backend.cli <INPUT> [options]
```

### 성공 계약 (중요)

| 항목 | 규칙 |
|------|------|
| 종료 코드 | 성공 `0`, 실패 비-0 |
| stderr | 진행/상태 로그 |
| stdout (`-o` 사용 시) | **자막 파일의 절대 경로만** |
| stdout (`--stdout`) | 자막 본문 (UTF-8) |

에이전트는 산출물 경로를 **stdout에서만** 읽으세요. stderr를 파싱하지 마세요.

### 예시

```bash
python -m backend.cli /path/to/audio.mp3
python -m backend.cli /path/to/audio.mp3 -f srt -o /tmp/out.srt
python -m backend.cli /path/to/audio.mp3 --stdout --lang ko
python -m backend.cli /path/to/audio.mp3 --base-url http://127.0.0.1:8000
python -m backend.cli --agent-help
```

---

## 파라미터 처리 규칙

1. **명시 요청이 없으면 옵션 플래그를 넣지 마세요.** 기본값은 백엔드/`WhisperParams`에 있습니다.
2. `<INPUT>`은 **존재하는 로컬 파일 경로**만 허용합니다. URL은 CLI에서 불가합니다.
3. `--format`: `srt` | `vtt` | `webvtt` | `txt` | `lrc` | `tsv` | `json`  
   기본값 `srt`. 요청이 없으면 `srt`를 쓰세요.
4. 언어를 알면 `--lang`에 ISO-639-1 코드 (`ko`, `en`, `ja` 등). 모르면 생략(자동 감지).
5. `--translate`는 **음성을 영어로 번역(Whisper translate)** 할 때만. NLLB/DeepL 자막 번역이 아닙니다.
6. `--model-size`는 요청 시에만 (`tiny`, `base`, `small`, `medium`, `large-v2`, `large-v3` 등).  
   작을수록 빠르고 VRAM↓, 클수록 품질↑ 경향.
7. `--compute-type`: GPU면 `float16`, CPU면 `float32`를 필요할 때만 지정.
8. 잡음/긴 무음이 많으면 `--vad-filter`.
9. 화자 구분이 필요할 때만 `--diarize` (필요 시 `--hf-token`).
10. BGM 제거가 필요할 때만 `--separate-bgm` (느리고 VRAM 증가).
11. 긴 미디어는 `--timeout`을 늘리세요 (기본 600초).
12. CLI에 없는 플래그를 만들지 마세요. 세밀 제어는 REST 쿼리 파라미터를 사용하세요.

### CLI 플래그 ↔ API 쿼리

| Flag | API query | 의미 |
|------|-----------|------|
| `-f` / `--format` | `file_format` | 자막 형식 (기본 srt) |
| `-o` / `--output` | (로컬) | 출력 경로. 기본 `<input>.<format>` |
| `--stdout` | (로컬) | 자막 본문을 stdout |
| `--base-url` | (로컬) | 백엔드 origin |
| `--poll-interval` | (로컬) | 폴링 간격 초 (기본 3) |
| `--timeout` | (로컬) | 최대 대기 초 (기본 600) |
| `--model-size` | `model_size` | Whisper 모델 크기 |
| `--lang` | `lang` | 원본 언어 코드 |
| `--translate` | `is_translate=true` | 음성→영어 번역 |
| `--compute-type` | `compute_type` | float16 / float32 / int8 … |
| `--vad-filter` | `vad_filter=true` | Silero VAD |
| `--diarize` | `is_diarize=true` | 화자 분리 |
| `--hf-token` | `hf_token` | 화자분리용 HF 토큰 |
| `--separate-bgm` | `is_separate_bgm=true` | UVR BGM 분리 |

---

## REST 흐름 (CLI 미사용 시)

Base: `http://127.0.0.1:8000`

1. **작업 등록**  
   `POST /subtitle/`  
   - multipart: `file=<audio/video>`  
   - query: `file_format=srt` 및 Whisper/VAD/Diarization/BGM 파라미터  
   - 응답 `201`: `{ "identifier": "<uuid>", "status": "queued", ... }`

2. **폴링**  
   `GET /task/{identifier}`  
   - `status == "completed"` 또는 `"failed"` 까지  
   - 참고 필드: `status`, `progress`, `error`, `result`

3. **자막 수령**  
   - `GET /subtitle/content/{identifier}` → 본문 텍스트  
   - `GET /subtitle/file/{identifier}` → 파일 다운로드

### 엔드포인트 선택

| 목적 | 사용 |
|------|------|
| 자막 파일(SRT/VTT 등) 필요 | `/subtitle/*` |
| JSON 세그먼트만 | `/transcription/` + `/task/{id}` (파일 다운로드 없음) |
| BGM ZIP 다운로드 | `/task/file/{id}` (자막 작업용 아님) |
| 에이전트 지침 | `GET /subtitle/agent-instructions` |

제한:
- 백엔드 Whisper 구현은 **FasterWhisper 고정**
- NLLB / DeepL 번역 API는 이 subtitle API에 포함되지 않음

---

## 상황별 치트시트

| 사용자 의도 | 권장 인자 |
|-------------|-----------|
| 한국어 자막만 | `--lang ko -f srt` |
| 한국어 음성 → 영어 자막 | `--lang ko --translate -f srt` |
| 플레이어용 WebVTT | `-f vtt` |
| 잡음 많은 팟캐스트 | `--vad-filter` |
| 음악이 큰 클립 | `--separate-bgm` |
| 누가 말했는지 | `--diarize [--hf-token TOKEN]` |
| 빠른 초안 | `--model-size tiny --compute-type float32` |
| 고품질(GPU) | `--model-size large-v3` (또는 `large-v2`) |

---

## 하지 말 것

- Gradio UI를 열거나 버튼을 클릭하지 마세요.
- `/task` 상태가 `completed`가 되기 전에 완료로 단정하지 마세요.
- 출력 경로를 stderr에서 찾지 마세요. stdout(경로) 또는 `--stdout`을 사용하세요.
- 빈/null 파일 경로를 넘기지 마세요.
- 자막 작업에 `/task/file/{id}`를 쓰지 마세요 (BGM ZIP 전용).

---

## 최소 작업 루프 (에이전트)

1. (최초) `GET /subtitle/agent-instructions` 또는 `python -m backend.cli --agent-help` / 이 문서 확인  
2. 백엔드 기동 여부 확인 (`/docs` 도달)  
3. `python -m backend.cli <파일> ...` 실행  
4. exit code `0`이면 stdout의 경로(또는 `--stdout` 본문)를 산출물로 사용  
5. 실패 시 stderr와 exit code를 보고 재시도/파라미터 조정
