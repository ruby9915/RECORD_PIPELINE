"""
녹음 파일 → 텍스트 변환 → 강의 분류 → 오탈자 교정 파이프라인

실행: .venv\\Scripts\\python.exe pipeline.py            (새 파일만 처리)
      .venv\\Scripts\\python.exe pipeline.py --dry-run  (처리 대상만 출력)
      .venv\\Scripts\\python.exe pipeline.py --reclassify  (전체 강의 분류만 다시)

단계(파일별, 중단 시 다음 실행에서 이어서 진행):
  1. 변환(transcribed)  : faster-whisper 로 원본 텍스트 생성
  2. 분류(classified)   : 시간표 매칭 → 실패 시 Claude 가 내용 보고 강의명 추정
  3. 교정(corrected)    : Claude 가 청크 단위로 오탈자 교정
  4. 배치(done)         : 강의명/날짜 폴더에 녹음파일·원본·수정본·정보 저장, 목록 갱신
"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
UNCLASSIFIED = "미분류"

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ───────────────────────── 설정 / 상태 ─────────────────────────

def load_config():
    with open(BASE / "config.json", encoding="utf-8") as f:
        return json.load(f)


class State:
    """output_dir/_state.json : 파일 해시 → 처리 기록"""

    def __init__(self, path: Path):
        self.path = path
        self.data = {}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def file_key(path: Path) -> str:
    """파일 이름이 바뀌어도 같은 녹음으로 인식하도록 크기 + 앞 4MB 내용으로 해시"""
    h = hashlib.sha1(str(path.stat().st_size).encode())
    with open(path, "rb") as f:
        h.update(f.read(4 * 1024 * 1024))
    return h.hexdigest()[:16]


# ───────────────────────── 녹음 메타데이터 ─────────────────────────

def audio_info(path: Path):
    """(녹음 시작 시각[로컬], 길이 초, 시각 출처)"""
    import av

    duration, created = 0.0, None
    with av.open(str(path)) as c:
        if c.duration:
            duration = c.duration / 1_000_000
        ct = c.metadata.get("creation_time") or ""
        if ct:
            try:
                created = dt.datetime.fromisoformat(ct.replace("Z", "+00:00"))
                if created.tzinfo:
                    created = created.astimezone().replace(tzinfo=None)
                if created.year < 2000:  # 0 값(1904/1970) 무시
                    created = None
            except ValueError:
                created = None
    if created:
        return created, duration, "파일 메타데이터(creation_time)"
    # Windows 녹음기: 수정 시각 ≈ 녹음 종료 시각 → 길이만큼 빼서 시작 시각 추정
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime)
    return mtime - dt.timedelta(seconds=duration), duration, "파일 수정시각 - 녹음길이 (추정)"


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# ───────────────────────── 1. 음성 → 텍스트 ─────────────────────────

_whisper_model = None


def _add_cuda_dll_dirs():
    site = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for d in glob.glob(str(site / "*" / "bin")):
        os.add_dll_directory(d)
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def get_whisper(cfg):
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    _add_cuda_dll_dirs()
    from faster_whisper import WhisperModel

    w = cfg["whisper"]
    root = str(BASE / "models")
    tries = [("cuda", "float16"), ("cpu", "int8")]
    if w.get("device") == "cpu":
        tries = [("cpu", "int8")]
    elif w.get("device") == "cuda":
        tries = [("cuda", "float16")]
    last = None
    for device, ctype in tries:
        try:
            log(f"Whisper 모델 로드: {w['model']} ({device}/{ctype}) — 첫 실행 시 다운로드")
            _whisper_model = WhisperModel(w["model"], device=device, compute_type=ctype, download_root=root)
            return _whisper_model
        except Exception as e:  # CUDA DLL 누락 등
            log(f"  {device} 로드 실패: {e}")
            last = e
    raise RuntimeError(f"Whisper 모델을 로드하지 못했습니다: {last}")


def decode_audio(path: Path, sr: int = 16000):
    """16kHz 모노 float32 파형. (faster-whisper 내장 디코더는 PyAV 15+ 와 호환되지 않아 직접 처리)"""
    import av
    import numpy as np

    chunks = []
    with av.open(str(path)) as c:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sr)
        for frame in c.decode(audio=0):
            for f in resampler.resample(frame):
                chunks.append(f.to_ndarray().reshape(-1))
        for f in resampler.resample(None):  # 남은 샘플 flush
            chunks.append(f.to_ndarray().reshape(-1))
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def hotwords_for(cfg, lecture):
    """강의별 용어 사전 → 모든 구간에 적용되는 hotwords 문자열"""
    terms = cfg["whisper"].get("hotwords_by_lecture", {}).get(lecture or "")
    return f"{lecture} 강의입니다. {terms}" if terms else None


def _is_hotword_leak(text: str, hotwords: str) -> bool:
    """용어 목록을 그대로 읊은 환각 구간 판별: 단어 4개 이상이 전부 용어 사전에 있는 단어"""
    vocab = set(re.findall(r"[\w]+", hotwords))
    words = re.findall(r"[\w]+", text)
    return len(words) >= 4 and all(wd in vocab for wd in words)


def transcribe(cfg, audio: Path, duration: float, work: Path, hotwords=None):
    model = get_whisper(cfg)
    w = cfg["whisper"]
    segments, _ = model.transcribe(
        decode_audio(audio),
        language=w.get("language") or None,
        beam_size=w.get("beam_size", 5),
        vad_filter=True,                  # 무음 구간 제거 → 환각 반복 감소
        condition_on_previous_text=False, # 긴 녹음에서 같은 문장 무한 반복 방지
        initial_prompt=w.get("initial_prompt") or None,
        hotwords=hotwords,                # 강의 용어 (initial_prompt 와 달리 모든 구간에 적용)
    )
    segs, leaks, last_pct = [], [], -10
    t0 = time.time()
    for s in segments:
        text = s.text.strip()
        if text and hotwords and _is_hotword_leak(text, hotwords):
            leaks.append(f"[{fmt_ts(s.start)}] 용어 목록 출력 환각 구간 제거: {text[:60]}")
            continue
        if text:
            segs.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text})
        pct = int(s.end / duration * 100) if duration else 0
        if pct >= last_pct + 10:
            last_pct = pct
            log(f"  변환 {pct}% ({fmt_ts(s.end)} / {fmt_ts(duration)}, 경과 {int(time.time() - t0)}초)")
    (work / "segments.json").write_text(json.dumps(segs, ensure_ascii=False, indent=1), encoding="utf-8")
    raw = "\n".join(s["text"] for s in segs)
    (work / "원본.txt").write_text(raw, encoding="utf-8")
    stamped = "\n".join(f"[{fmt_ts(s['start'])}] {s['text']}" for s in segs)
    (work / "원본_타임스탬프.txt").write_text(stamped, encoding="utf-8")
    for lk in leaks:
        log("  ⚠ " + lk)
    return raw, leaks


# ───────────────────────── Claude 호출 ─────────────────────────

def find_claude(cfg) -> str:
    cands = [cfg["claude"].get("exe_path"), os.environ.get("CLAUDE_CODE_EXECPATH"), shutil.which("claude")]
    for c in cands:
        if c and Path(c).exists():
            return c
    # VSCode 확장 버전이 바뀌어도 찾도록 최신 버전 폴더 탐색
    found = glob.glob(str(Path.home() / ".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude.exe"))

    def ver(p):
        m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", p)
        return tuple(map(int, m.groups())) if m else (0,)

    if found:
        return max(found, key=ver)
    raise RuntimeError("claude 실행 파일을 찾지 못했습니다. config.json 의 claude.exe_path 를 지정하세요.")


def ask_claude(cfg, system: str, prompt: str) -> str:
    exe = find_claude(cfg)
    cmd = [exe, "-p", "--tools", "", "--system-prompt", system,
           "--no-session-persistence", "--setting-sources", "", "--output-format", "json"]
    if cfg["claude"].get("model"):
        cmd += ["--model", cfg["claude"]["model"]]
    last_err = None
    for attempt in range(3):
        p = None
        try:
            # 프로젝트 CLAUDE.md 등이 섞이지 않게 빈 임시 폴더에서 실행
            with tempfile.TemporaryDirectory() as tmp:
                p = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True,
                                   cwd=tmp, timeout=cfg["claude"].get("timeout_sec", 600))
            out = p.stdout.decode("utf-8", errors="replace")
            data = json.loads(out)
            if data.get("is_error") or data.get("subtype") not in (None, "success"):
                raise RuntimeError(f"Claude 오류: {str(data.get('result'))[:300]}")
            return data["result"]
        except Exception as e:
            last_err = e
            detail = p.stderr.decode("utf-8", "replace")[:300] if p is not None and p.stderr else ""
            log(f"  Claude 호출 실패({attempt + 1}/3): {e} {detail}")
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Claude 호출 3회 실패: {last_err}")


def extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"JSON 응답 아님: {text[:200]}")
    return json.loads(m.group(0))


# ───────────────────────── 2. 강의 분류 ─────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def _calendar_files(cfg, out_dir: Path) -> list[Path]:
    """Google 캘린더(= Notion 캘린더의 원본) 비공개 iCal 주소를 받아 캐시. 실패 시 이전 캐시 사용"""
    import urllib.request

    files = []
    refresh = cfg.get("calendar", {}).get("refresh_hours", 6) * 3600
    for i, url in enumerate(cfg.get("calendar", {}).get("ics_urls", [])):
        f = out_dir / f"_calendar_{i}.ics"
        if not f.exists() or time.time() - f.stat().st_mtime > refresh:
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    f.write_bytes(r.read())
                log(f"캘린더 {i + 1} 갱신")
            except Exception as e:
                log(f"캘린더 {i + 1} 다운로드 실패: {e}" + (" (이전 캐시 사용)" if f.exists() else ""))
        if f.exists():
            files.append(f)
    return files


def match_calendar(cfg, out_dir: Path, start: dt.datetime, duration: float):
    """녹음 시간과 가장 많이 겹치는 캘린더 일정 → (강의명, 일정 제목)"""
    files = _calendar_files(cfg, out_dir)
    if not files:
        return None, None
    import icalendar
    import recurring_ical_events

    known = cfg.get("known_lectures", [])

    def lecture_of(title):
        # 일정 제목에 강의명 목록의 이름이 들어 있으면 그 이름으로 통일.
        # 목록이 있는데 어느 것과도 안 맞으면 수업이 아닌 일정(약속 등)으로 보고 제외
        for name in known:
            if _norm(name) in _norm(title):
                return name
        return None if known else title

    end = start + dt.timedelta(seconds=duration)
    q_start, q_end = start.astimezone(), end.astimezone()
    best, best_title, best_overlap = None, None, 0
    for f in files:
        cal = icalendar.Calendar.from_ical(f.read_bytes())
        for ev in recurring_ical_events.of(cal).between(q_start - dt.timedelta(hours=1), q_end + dt.timedelta(hours=1)):
            s, e = ev.get("DTSTART").dt, ev.get("DTEND").dt if ev.get("DTEND") else None
            if not isinstance(s, dt.datetime) or not isinstance(e, dt.datetime):
                continue  # 종일 일정 제외
            s = s.astimezone().replace(tzinfo=None) if s.tzinfo else s
            e = e.astimezone().replace(tzinfo=None) if e.tzinfo else e
            title = str(ev.get("SUMMARY", "")).strip()
            name = lecture_of(title)
            overlap = (min(end, e) - max(start, s)).total_seconds()
            if name and overlap > best_overlap:
                best, best_title, best_overlap = name, title, overlap
    if not best or best_overlap < min(duration * 0.3, 60):
        return None, None
    return best, best_title


def time_based_lecture(cfg, out_dir: Path, start: dt.datetime, duration: float):
    """녹음 시각만으로 정한 강의 → (강의명, 분류방법, 분류결과) 또는 None"""
    cal_name, cal_title = match_calendar(cfg, out_dir, start, duration)
    if cal_name:
        return sanitize(cal_name), "캘린더", {"신뢰도": "높음", "근거": f"캘린더 일정 '{cal_title}'"}
    name = match_timetable(cfg, start, duration)
    if name:
        return sanitize(name), "시간표", {"신뢰도": "높음"}
    return None


def match_timetable(cfg, start: dt.datetime, duration: float):
    """시간표 항목과 녹음 시간이 가장 많이 겹치는 강의"""
    period = cfg.get("timetable_period")
    if period and not (period[0] <= f"{start:%Y-%m-%d}" <= period[1]):
        return None  # 다른 학기 녹음 → 시간표 적용 안 함
    end = start + dt.timedelta(seconds=duration)
    wd = WEEKDAYS[start.weekday()]
    best, best_overlap = None, 0
    for t in cfg.get("timetable", []):
        if t.get("요일") != wd:
            continue
        s = dt.datetime.combine(start.date(), dt.time.fromisoformat(t["시작"]))
        e = dt.datetime.combine(start.date(), dt.time.fromisoformat(t["종료"]))
        overlap = (min(end, e) - max(start, s)).total_seconds()
        if overlap > best_overlap:
            best, best_overlap = t, overlap
    # 녹음 길이의 30% 이상 겹쳐야 인정 (짧은 녹음은 60초 이상)
    if best and best_overlap >= min(duration * 0.3, 60):
        return best["강의명"]
    return None


CLASSIFY_SYSTEM = (
    "너는 대학/학원 강의 녹취록을 보고 어떤 강의인지 판별하는 분류기다. "
    "반드시 JSON 객체 하나만 출력한다. 다른 설명은 쓰지 않는다."
)


def classify(cfg, raw: str, start: dt.datetime, existing_names: list[str]):
    names = sorted(set(cfg.get("known_lectures", [])) | set(existing_names))
    n = len(raw)
    excerpt = raw[:3000]
    if n > 6000:
        excerpt += "\n...(중략)...\n" + raw[n // 2: n // 2 + 1500] + "\n...(중략)...\n" + raw[-1500:]
    prompt = f"""다음은 음성인식으로 받아 적은 녹음 내용 발췌다(오인식 포함 가능).
녹음 일시: {start:%Y-%m-%d} ({WEEKDAYS[start.weekday()]}) {start:%H:%M}

기존 강의명 목록(해당하면 이 중 정확히 같은 이름을 사용): {json.dumps(names, ensure_ascii=False) if names else "없음"}

규칙:
- 목록에 맞는 강의가 있으면 그 이름을 그대로 쓴다.
- 없으면 내용에서 드러나는 과목/주제를 짧은 강의명(예: "자료구조", "회계원리")으로 정한다. 교수·강사가 과목명을 말하면 그것을 우선한다.
- 강의가 아니거나(잡담, 테스트 녹음 등) 판단 근거가 부족하면 강의명을 "{UNCLASSIFIED}"로 한다.
- 신뢰도는 "높음" / "중간" / "낮음" 중 하나.

출력 형식: {{"강의명": "...", "신뢰도": "...", "근거": "한두 문장", "주제": "이번 녹음에서 다룬 핵심 주제 한 줄"}}

<녹음내용>
{excerpt}
</녹음내용>"""
    res = extract_json(ask_claude(cfg, CLASSIFY_SYSTEM, prompt))
    name = sanitize(res.get("강의명") or UNCLASSIFIED)
    if res.get("신뢰도") == "낮음":
        name = UNCLASSIFIED
    return name, res


def sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or UNCLASSIFIED


# ───────────────────────── 3. 오탈자 교정 ─────────────────────────

CORRECT_SYSTEM = (
    "너는 한국어 강의 음성인식(STT) 녹취록 교정자다. 입력 텍스트의 오탈자만 고친 결과만 출력한다. "
    "머리말, 설명, 코드블록 표시 없이 교정된 본문만 출력한다."
)


def split_chunks(raw: str, size: int) -> list[str]:
    lines, chunks, cur = raw.split("\n"), [], ""
    for ln in lines:
        if cur and len(cur) + len(ln) + 1 > size:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        chunks.append(cur)
    return chunks


def correct_chunk(cfg, lecture: str, chunk: str, idx: int, total: int):
    prompt = f"""강의명: {lecture if lecture != UNCLASSIFIED else "알 수 없음"}
아래는 강의 녹취록의 {idx + 1}/{total} 번째 부분이다.

교정 규칙:
- 음성인식 오류로 잘못 적힌 단어(특히 전문용어, 고유명사), 맞춤법, 띄어쓰기, 문장부호를 고친다.
- 내용을 요약·삭제·추가·재배열하지 않는다. 말투(구어체)와 반복 표현도 그대로 둔다.
- 확신이 없는 부분은 원문을 유지한다.
- 줄 구성은 원문과 최대한 같게 유지한다.

<원문>
{chunk}
</원문>"""
    for attempt in range(2):
        out = ask_claude(cfg, CORRECT_SYSTEM, prompt).strip()
        out = re.sub(r"^</?원문>|</?원문>$", "", out).strip()
        ratio = len(out) / max(len(chunk), 1)
        if 0.8 <= ratio <= 1.25:
            return out, None
        log(f"  청크 {idx + 1}: 길이 비율 {ratio:.2f} 이상 → 재시도")
    # 요약/누락 의심 → 내용 손실을 막기 위해 원문 유지
    return chunk, f"청크 {idx + 1}: 교정 결과 길이 비정상(비율 {ratio:.2f}) → 원문 유지"


def correct(cfg, lecture: str, raw: str, work: Path):
    """청크별 결과를 work/corrected/ 에 저장 → 중단돼도 끝난 청크는 재사용"""
    cdir = work / "corrected"
    cdir.mkdir(exist_ok=True)
    chunks = split_chunks(raw, cfg["claude"].get("chunk_chars", 3000))
    warnings = []

    def job(i):
        f = cdir / f"{i:04d}.txt"
        if f.exists():
            return i, f.read_text(encoding="utf-8"), None
        out, warn = correct_chunk(cfg, lecture, chunks[i], i, len(chunks))
        if warn is None:  # 원문 유지된 청크는 저장하지 않아 다음 실행에서 재시도
            f.write_text(out, encoding="utf-8")
        return i, out, warn

    results = [None] * len(chunks)
    done = 0
    with cf.ThreadPoolExecutor(max_workers=cfg["claude"].get("parallel", 3)) as ex:
        for fut in cf.as_completed([ex.submit(job, i) for i in range(len(chunks))]):
            i, out, warn = fut.result()
            results[i] = out
            done += 1
            if warn:
                warnings.append(warn)
            log(f"  교정 {done}/{len(chunks)}")
    text = "\n".join(results)
    (work / "수정본.txt").write_text(text, encoding="utf-8")
    return text, sorted(warnings)


# ───────────────────────── 4. 배치 / 목록 ─────────────────────────

def place(cfg, rec: dict, audio: Path, work: Path, out_dir: Path):
    start = dt.datetime.fromisoformat(rec["녹음시작"])
    folder = out_dir / rec["강의명"] / f"{start:%Y-%m-%d}({rec['요일']})_{start:%H%M}_{audio.stem}"
    old = rec.get("폴더")
    if old and Path(old).exists() and Path(old) != folder:  # 재분류로 강의명이 바뀐 경우 이동
        folder.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(old, folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("원본.txt", "원본_타임스탬프.txt", "수정본.txt"):
        if (work / name).exists():
            shutil.copy2(work / name, folder / name)
    if cfg.get("copy_audio", True) and not (folder / audio.name).exists():
        shutil.copy2(audio, folder / audio.name)
    rec["폴더"] = str(folder)
    info = {k: v for k, v in rec.items() if k != "폴더"}
    (folder / "정보.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")


def write_index(state: State, out_dir: Path):
    rows = sorted((r for r in state.data.values() if r.get("단계") == "done"),
                  key=lambda r: (r["강의명"], r["녹음시작"]))
    cols = ["강의명", "날짜", "요일", "녹음시각", "길이", "녹음파일", "원본", "수정본", "분류방법", "신뢰도", "주제", "경고"]
    table = []
    for r in rows:
        folder = Path(r["폴더"])
        rel = lambda p: os.path.relpath(p, out_dir)
        table.append({
            "강의명": r["강의명"], "날짜": r["날짜"], "요일": r["요일"],
            "녹음시각": r["녹음시작"][11:16], "길이": r["길이"],
            "녹음파일": r["원본파일"],
            "원본": rel(folder / "원본.txt"), "수정본": rel(folder / "수정본.txt"),
            "분류방법": r.get("분류방법", ""), "신뢰도": r.get("분류결과", {}).get("신뢰도", ""),
            "주제": r.get("분류결과", {}).get("주제", ""), "경고": " / ".join(r.get("경고", [])),
        })
    with open(out_dir / "강의목록.csv", "w", encoding="utf-8-sig", newline="") as f:  # 엑셀 호환
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(table)

    md = ["# 강의 녹음 목록", "", f"갱신: {dt.datetime.now():%Y-%m-%d %H:%M}", ""]
    for lecture in sorted({t["강의명"] for t in table}):
        md += [f"## {lecture}", "", "| 날짜 | 요일 | 시각 | 길이 | 녹음파일 | 원본 | 수정본 | 주제 |", "|---|---|---|---|---|---|---|---|"]
        for t in (t for t in table if t["강의명"] == lecture):
            o, c = t["원본"].replace("\\", "/"), t["수정본"].replace("\\", "/")
            md.append(f"| {t['날짜']} | {t['요일']} | {t['녹음시각']} | {t['길이']} | {t['녹음파일']} "
                      f"| [원본](<{o}>) | [수정본](<{c}>) | {t['주제']}{' ⚠' if t['경고'] else ''} |")
        md.append("")
    (out_dir / "강의목록.md").write_text("\n".join(md), encoding="utf-8")


# ───────────────────────── 메인 ─────────────────────────

def scan(cfg):
    src = Path(cfg["source_dir"])
    exts = {e.lower() for e in cfg["audio_extensions"]}
    now = time.time()
    files = []
    for p in sorted(src.iterdir()):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        if now - p.stat().st_mtime < cfg.get("skip_if_modified_within_sec", 120):
            log(f"건너뜀(녹음/동기화 중일 수 있음): {p.name}")
            continue
        files.append(p)
    return files


def process(cfg, state: State, audio: Path, key: str, out_dir: Path, reclassify=False):
    rec = state.data.setdefault(key, {"단계": "new"})
    rec["원본파일"] = audio.name
    rec["원본경로"] = str(audio)
    work = out_dir / "_work" / key
    work.mkdir(parents=True, exist_ok=True)

    if "녹음시작" not in rec:
        start, dur, src = audio_info(audio)
        rec.update({"녹음시작": start.isoformat(timespec="seconds"), "날짜": f"{start:%Y-%m-%d}",
                    "요일": WEEKDAYS[start.weekday()], "길이": fmt_ts(dur), "길이초": round(dur, 1),
                    "시각출처": src})
    start = dt.datetime.fromisoformat(rec["녹음시작"])

    if rec["단계"] == "new":
        # 녹음 시각으로 강의를 미리 알 수 있으면 그 강의 용어를 음성 인식에 반영
        tb = time_based_lecture(cfg, out_dir, start, rec["길이초"])
        hw = hotwords_for(cfg, tb[0] if tb else None)
        log(f"[1/4] 텍스트 변환: {audio.name} (길이 {rec['길이']})" + (f" — 용어 사전: {tb[0]}" if hw else ""))
        _, leaks = transcribe(cfg, audio, rec["길이초"], work, hotwords=hw)
        rec["변환경고"] = leaks
        rec["단계"] = "transcribed"
        state.save()
    raw = (work / "원본.txt").read_text(encoding="utf-8")
    if not raw.strip():
        rec.update({"강의명": UNCLASSIFIED, "분류방법": "인식된 음성 없음", "경고": ["인식된 텍스트 없음"]})
        (work / "수정본.txt").write_text("", encoding="utf-8")
        rec["단계"] = "corrected"

    if rec["단계"] == "transcribed" or (reclassify and raw.strip()):
        existing = [r["강의명"] for k, r in state.data.items() if k != key and r.get("강의명") not in (None, UNCLASSIFIED)]
        tb = time_based_lecture(cfg, out_dir, start, rec["길이초"])
        if tb:
            rec.update({"강의명": tb[0], "분류방법": tb[1], "분류결과": tb[2]})
        else:
            log(f"[2/4] 강의 분류(Claude): {audio.name}")
            name, res = classify(cfg, raw, start, existing)
            rec.update({"강의명": name, "분류방법": "Claude 내용 분석", "분류결과": res})
        log(f"  → 강의명: {rec['강의명']} ({rec['분류방법']})")
        if rec["단계"] == "transcribed":
            rec["단계"] = "classified"
        state.save()

    if rec["단계"] == "classified":
        log(f"[3/4] 오탈자 교정(Claude): {audio.name}")
        _, warnings = correct(cfg, rec["강의명"], raw, work)
        rec["경고"] = rec.get("변환경고", []) + warnings
        rec["단계"] = "corrected"
        state.save()

    if rec["단계"] in ("corrected", "done"):
        log(f"[4/4] 정리: {rec['강의명']} / {rec['날짜']}({rec['요일']})")
        place(cfg, rec, audio, work, out_dir)
        rec["단계"] = "done"
        state.save()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="처리 대상만 출력")
    ap.add_argument("--reclassify", action="store_true", help="이미 처리된 파일도 강의 분류 다시 수행")
    ap.add_argument("--only", help="이 이름의 파일만 처리 (시험용)")
    ap.add_argument("--redo", action="store_true", help="이미 처리된 파일도 변환부터 다시 (모델·용어 사전 변경 후)")
    args = ap.parse_args()

    cfg = load_config()
    out_dir = Path(cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    state = State(out_dir / "_state.json")

    todo = []
    for audio in scan(cfg):
        if args.only and audio.name != args.only:
            continue
        key = file_key(audio)
        rec = state.data.get(key)
        if rec and args.redo:
            rec["단계"] = "new"  # 녹음시각·폴더 기록은 유지 → 결과 파일 덮어쓰기
            shutil.rmtree(out_dir / "_work" / key / "corrected", ignore_errors=True)
        elif rec and rec.get("단계") == "done" and not args.reclassify:
            continue
        todo.append((audio, key))

    log(f"처리 대상 {len(todo)}개" + (": " + ", ".join(a.name for a, _ in todo) if todo else " (새 파일 없음)"))
    if args.dry_run:
        return

    failed = []
    for audio, key in todo:
        try:
            process(cfg, state, audio, key, out_dir, reclassify=args.reclassify)
        except Exception as e:
            log(f"실패: {audio.name} — {e} (다음 실행 때 이어서 진행)")
            failed.append(audio.name)
            state.save()
    write_index(state, out_dir)
    log(f"완료. 목록: {out_dir / '강의목록.md'}" + (f" / 실패 {len(failed)}개: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
