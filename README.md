# 강의 녹음 → 텍스트 변환·분류 파이프라인

`run.bat` 실행(더블클릭) → `소리 녹음` 폴더에서 **아직 처리 안 된 파일만** 찾아 처리합니다.

| 단계 | 도구 | 결과 |
|---|---|---|
| 1. 텍스트 변환 | faster-whisper (로컬 GPU, `models\`) | 원본.txt, 원본_타임스탬프.txt |
| 2. 강의 분류 | ① Google 캘린더(iCal) 일정 ② config 시간표 ③ Claude 내용 분석 순 | 강의명 |
| 3. 오탈자 교정 | Claude (Claude Code CLI, 3000자 청크) | 수정본.txt |
| 4. 정리 | — | 강의별 폴더 + 강의목록.csv / .md |

## 설치 (Windows, NVIDIA GPU)
```
python -m pip install uv
python -m uv venv .venv --python 3.12
python -m uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```
- Claude 교정·분류는 **Claude Code CLI**(로그인된 claude.ai 계정)를 호출합니다. VSCode Claude Code 확장이 있으면 자동으로 찾고,
  없으면 `config.json`의 `claude.exe_path`에 claude 실행 파일 경로를 지정하세요.
- Whisper 모델은 첫 실행 때 `models\`에 자동 다운로드됩니다 (large-v3-turbo 약 1.6GB).
- `config.json`의 `source_dir`, `output_dir`, 시간표를 자신의 환경에 맞게 수정하세요.

## 결과 구조
```
강의정리\
  강의목록.csv / 강의목록.md        ← 강의명·날짜·요일·녹음파일·원본·수정본 일람
  <강의명>\<날짜(요일)_시각_파일명>\
      녹음 (3).m4a  원본.txt  원본_타임스탬프.txt  수정본.txt  정보.json
  미분류\...                          ← 강의 아님/판단 불가/신뢰도 낮음
  _state.json, _work\                 ← 처리 기록·중간 결과 (지우면 전부 재처리)
```

## 옵션
- `run.bat --dry-run` : 처리 대상만 확인
- `run.bat --redo` : 모델·용어 사전을 바꾼 뒤 기존 녹음을 변환부터 다시 처리 (`--only "녹음 (3).m4a"`로 한 파일만)
- `run.bat --reclassify` : 강의목록/시간표를 고친 뒤 기존 파일 분류만 다시 (폴더 이동)

## config.json 주요 항목
- `calendar.ics_urls` : **Notion 캘린더에 연결된 Google 캘린더의 비공개 iCal 주소**.
  Notion 캘린더 자체는 외부 API가 없으므로 원본인 Google 캘린더를 읽습니다.
  Google 캘린더(웹) → 설정 → 해당 캘린더 → "캘린더 통합" → **iCal 형식의 비공개 주소** 복사.
  ⚠ 이 주소를 아는 사람은 누구나 일정을 볼 수 있으니 config.json을 공유하지 마세요 (유출 시 같은 화면에서 재설정).
  `known_lectures`가 있으면 그 이름이 제목에 포함된 일정만 강의로 인정합니다(약속 등 제외).
- `timetable` : 캘린더 대신/보조로 쓰는 고정 시간표. 예
  `[{"강의명": "자료구조", "요일": "월", "시작": "09:00", "종료": "11:50"}]`
- `timetable_period` : 시간표를 적용할 학기 기간. 범위 밖 녹음(지난 학기 등)은 Claude 내용 분석으로 분류.
  **학기가 바뀌면 timetable과 함께 반드시 갱신**하세요.
- `known_lectures` : 강의명 후보 목록. Claude가 이 이름 중에서 고르도록 해 표기 흔들림 방지
- `whisper.hotwords_by_lecture` : **강의별 용어 사전** (인식률에 가장 효과 큼). 녹음 시각으로 강의가 정해지면 그 강의 용어를 모든 구간에 적용.
  실제 강의에서 자주 틀리는 용어를 추가하세요. 용어 목록을 그대로 읊은 환각 구간은 자동 제거 후 `경고`에 기록.
- `whisper.model` : `large-v3-turbo`(기본). 10분 샘플 비교에서 `large-v3`는 2배 느리고, hotwords 사용 시
  용어 목록을 출력하며 본문을 누락하는 환각이 있어 기본값으로 쓰지 않음.
- `claude.model` : 비우면 Claude Code 기본 모델
- `copy_audio` : 녹음파일을 강의 폴더에 복사할지 (false면 용량 절약, 목록에 파일명만 기록)

## 한계
- 녹음 시각은 파일 메타데이터 → 없으면 `수정시각 - 녹음길이`로 **추정**. OneDrive 동기화로 수정시각이 바뀌면 날짜가 틀릴 수 있음 (정보.json의 `시각출처` 확인).
- 교정 결과 길이가 원문 대비 80~125%를 벗어나면(요약·누락 의심) 해당 청크는 원문을 유지하고 `경고`에 기록.
- Claude 호출은 Claude Code 로그인 계정의 사용량에서 차감됩니다.
