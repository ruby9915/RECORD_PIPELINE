"""과목(강의) 분류: 캘린더/시간표(시각 기반) → Claude 내용 분석. 자료·필기 분류 포함"""
import datetime as dt
import json
import time
from pathlib import Path

from .claude_cli import ask_claude, extract_json
from .common import UNCLASSIFIED, WEEKDAYS, log, norm, sanitize
from .extract import outline_text


# ───────────────────────── 시각 기반 ─────────────────────────

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
            if norm(name) in norm(title):
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


def time_based_lecture(cfg, out_dir: Path, start: dt.datetime, duration: float):
    """녹음 시각만으로 정한 강의 → (강의명, 분류방법, 분류결과) 또는 None"""
    cal_name, cal_title = match_calendar(cfg, out_dir, start, duration)
    if cal_name:
        return sanitize(cal_name), "캘린더", {"신뢰도": "높음", "근거": f"캘린더 일정 '{cal_title}'"}
    name = match_timetable(cfg, start, duration)
    if name:
        return sanitize(name), "시간표", {"신뢰도": "높음"}
    return None


# ───────────────────────── 내용 기반 (Claude) ─────────────────────────

def lecture_names(cfg, items: dict) -> list[str]:
    """설정의 강의명 + 이미 분류된 항목의 강의명"""
    names = set(cfg.get("known_lectures", []))
    for it in items.values():
        if it.get("강의명") and it["강의명"] != UNCLASSIFIED:
            names.add(it["강의명"])
    return sorted(names)


def _names_block(cfg, names):
    aliases = cfg.get("lecture_aliases", {})
    s = f"과목 목록(해당하면 이 중 정확히 같은 이름을 사용): {json.dumps(names, ensure_ascii=False) if names else '없음'}"
    if aliases:
        s += "\n줄임말·별칭: " + ", ".join(f"{', '.join(v)} = {k}" for k, v in aliases.items())
    return s


def _finalize(res: dict):
    name = sanitize(res.get("강의명") or UNCLASSIFIED)
    if res.get("신뢰도") == "낮음":
        name = UNCLASSIFIED
    return name, res


RULES_COMMON = """- 목록에 맞는 과목이 있으면 그 이름을 그대로 쓴다 (별칭이면 정식 이름으로).
- 없으면 내용에서 드러나는 과목을 짧은 이름(예: "자료구조")으로 정한다. 교수가 과목명을 말하면 그것을 우선한다.
- 판단 근거가 부족하면 강의명을 "미분류"로 한다.
- 신뢰도는 "높음" / "중간" / "낮음" 중 하나."""

CLASSIFY_SYSTEM = (
    "너는 대학 강의의 녹취록·자료·필기를 보고 어느 과목의 것인지 판별하는 분류기다. "
    "반드시 JSON 객체 하나만 출력한다. 다른 설명은 쓰지 않는다."
)


def classify_recording(cfg, raw: str, start: dt.datetime, names: list[str], filename: str = ""):
    n = len(raw)
    excerpt = raw[:3000]
    if n > 6000:
        excerpt += "\n...(중략)...\n" + raw[n // 2: n // 2 + 1500] + "\n...(중략)...\n" + raw[-1500:]
    prompt = f"""다음은 음성인식으로 받아 적은 강의 녹음 발췌다(오인식 포함 가능).
파일명: {filename}
녹음 일시: {start:%Y-%m-%d} ({WEEKDAYS[start.weekday()]}) {start:%H:%M}

{_names_block(cfg, names)}

규칙:
{RULES_COMMON}
- 강의가 아니면(잡담, 테스트 녹음 등) 강의명을 "미분류"로 한다.

출력 형식: {{"강의명": "...", "신뢰도": "...", "근거": "한두 문장", "주제": "이번 녹음에서 다룬 핵심 주제 한 줄", "주차": 정수 또는 null}}

<녹음내용>
{excerpt}
</녹음내용>"""
    return _finalize(extract_json(ask_claude(cfg, CLASSIFY_SYSTEM, prompt)))


def classify_material(cfg, doc: dict, filename: str, names: list[str]):
    """강의자료 → 과목 + 개요(제목, 종류, 주차, 주제별 쪽 범위, 핵심용어)"""
    prompt = f"""다음은 강의자료에서 추출한 텍스트다.
파일명: {filename}
형식: {doc['형식']}, {len(doc['쪽'])}{doc['쪽단위']}

{_names_block(cfg, names)}

규칙:
{RULES_COMMON}
- 파일명에 과목·주차 단서가 있으면 활용한다.
- "주제목록"은 자료의 내용 구성을 쪽 범위와 함께 5~15개로 정리한다 (쪽 번호는 '=== p.N ===' 마커 기준).
- "핵심용어"는 이 자료에 나오는 전문용어·고유명사 10~30개 (음성인식 보정용이므로 수업에서 말로 나올 법한 용어 위주, 한국어 표기).

출력 형식: {{"강의명": "...", "신뢰도": "...", "근거": "...", "제목": "자료 제목", "종류": "강의슬라이드|교재|과제|공지|시험|기타",
 "주차": 정수 또는 null, "주제목록": [{{"주제": "...", "쪽": "3-10"}}], "핵심용어": ["..."]}}

<자료내용>
{outline_text(doc)}
</자료내용>"""
    return _finalize(extract_json(ask_claude(cfg, CLASSIFY_SYSTEM, prompt)))


def classify_note(cfg, text: str, filename: str, file_date: str, names: list[str]):
    """필기(텍스트 파일) → 과목 + 날짜(내용에 명시된 경우) + 주제"""
    excerpt = text[:6000] + ("\n...(이하 생략)" if len(text) > 6000 else "")
    prompt = f"""다음은 학생이 수업 중 작성한 필기(텍스트 파일)다.
파일명: {filename}
파일 수정 날짜: {file_date}

{_names_block(cfg, names)}

규칙:
{RULES_COMMON}
- "날짜"는 필기 내용이나 파일명에 수업 날짜가 명시된 경우에만 YYYY-MM-DD 로 적고, 아니면 null.

출력 형식: {{"강의명": "...", "신뢰도": "...", "근거": "...", "날짜": "YYYY-MM-DD" 또는 null, "주제": "한 줄", "주차": 정수 또는 null}}

<필기>
{excerpt}
</필기>"""
    return _finalize(extract_json(ask_claude(cfg, CLASSIFY_SYSTEM, prompt)))
