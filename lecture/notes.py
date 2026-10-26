"""회차(과목+날짜) 구성, 관련 자료 선택, 강의노트 생성·검증"""
import datetime as dt
import json
import re
from pathlib import Path

from .claude_cli import ask_claude, extract_json
from .common import (UNCLASSIFIED, WEEKDAYS, date_label, fmt_ts, log, read_json, read_text_any,
                     session_folder, session_id, sha1_text, write_json)
from .extract import pages_text, parse_page_range

PROMPT_VERSION = "note-v1"  # 프롬프트를 바꾸면 올려서 기존 노트가 다시 생성되게 함
MATERIAL_CHARS_CAP = 60000   # 노트 생성에 넣는 자료 본문 상한


# ───────────────────────── 회차 구성 ─────────────────────────

def build_sessions(items: dict, sessions: dict):
    """done 상태의 녹음/영상/필기 → 회차(과목|날짜) 집합 갱신. 노트·노션 기록은 유지"""
    groups = {}
    for key, it in items.items():
        if it.get("단계") != "done" or it.get("종류") not in ("녹음", "영상", "필기"):
            continue
        lec, date = it.get("강의명"), it.get("날짜")
        if not lec or lec == UNCLASSIFIED or not date:
            continue
        sid = session_id(lec, date)
        g = groups.setdefault(sid, {"강의명": lec, "날짜": date, "요일": WEEKDAYS[dt.date.fromisoformat(date).weekday()],
                                    "녹음": [], "영상": [], "필기": []})
        g[it["종류"]].append(key)
    for sid, g in groups.items():
        for k in ("녹음", "영상", "필기"):
            g[k].sort(key=lambda key: items[key].get("녹음시작") or items[key].get("원본파일", ""))
        sessions.setdefault(sid, {}).update(g)
    # 항목이 모두 사라진(재분류 등) 회차 표시
    for sid, s in sessions.items():
        if sid not in groups:
            s["항목없음"] = True
        else:
            s.pop("항목없음", None)
    return groups


def subject_materials(items: dict, lecture: str) -> list[tuple[str, dict]]:
    return sorted(((k, it) for k, it in items.items()
                   if it.get("종류") == "자료" and it.get("단계") == "done" and it.get("강의명") == lecture),
                  key=lambda kv: kv[1].get("원본파일", ""))


def _recording_text(it: dict, stamped=True) -> str:
    folder = Path(it["폴더"])
    f = folder / ("수정본_타임스탬프.txt" if stamped else "수정본.txt")
    return f.read_text(encoding="utf-8") if f.exists() else ""


def _recording_duration(it: dict) -> float:
    return float(it.get("길이초") or 0)


def session_excerpt(items: dict, s: dict) -> str:
    """자료 선택용 짧은 발췌: 주제 줄 + 녹취 앞/중간/끝"""
    parts = []
    for key in s.get("녹음", []) + s.get("영상", []):
        it = items[key]
        topic = it.get("분류결과", {}).get("주제")
        if topic:
            parts.append(f"주제: {topic}")
        raw = _recording_text(it, stamped=False)
        n = len(raw)
        if n:
            ex = raw[:2000]
            if n > 4000:
                ex += "\n...\n" + raw[n // 2: n // 2 + 1000] + "\n...\n" + raw[-800:]
            parts.append(ex)
    for key in s.get("필기", []):
        it = items[key]
        parts.append("[필기] " + read_text_any(Path(it["폴더"]) / it["원본파일"])[:1500])
    return "\n".join(parts)


# ───────────────────────── 자료 선택 (Claude) ─────────────────────────

SELECT_SYSTEM = "너는 특정 수업 회차에서 다룬 내용과 관련된 강의자료를 고르는 도우미다. JSON 객체 하나만 출력한다."


def select_materials(cfg, items: dict, s: dict) -> list[dict]:
    mats = subject_materials(items, s["강의명"])
    if not mats:
        return []
    listing = []
    for i, (key, it) in enumerate(mats, 1):
        r = it.get("분류결과", {})
        topics = "; ".join(f"{t.get('주제')}({t.get('쪽')})" for t in r.get("주제목록", []) if t.get("주제"))
        listing.append(f"{i}. {r.get('제목') or it['원본파일']} ({it['원본파일']}) — 종류: {r.get('종류', '?')}, "
                       f"주차: {r.get('주차') or '?'}, {it.get('쪽수', '?')}{it.get('쪽단위', '쪽')}\n   주제: {topics}")
    prompt = f"""과목: {s['강의명']}
날짜: {s['날짜']} ({s['요일']})

<이 회차 녹취 발췌>
{session_excerpt(items, s)}
</이 회차 녹취 발췌>

<자료 목록>
{chr(10).join(listing)}
</자료 목록>

규칙:
- 녹취에서 실제로 다룬 내용과 겹치는 자료만 고른다.
- 슬라이드·교재는 겹치는 쪽 범위를 "3-15" 처럼 적고, 대부분 해당하면 "전체".
- 과제·공지·시험 자료는 녹취에 언급되거나 이 날짜와 가까우면 포함.
- 관련 자료가 없으면 빈 목록.

출력 형식: {{"선택": [{{"번호": 1, "쪽": "3-15", "이유": "한 문장"}}]}}"""
    res = extract_json(ask_claude(cfg, SELECT_SYSTEM, prompt))
    chosen = []
    for c in res.get("선택", []):
        try:
            key, it = mats[int(c["번호"]) - 1]
        except (ValueError, IndexError, KeyError, TypeError):
            continue
        chosen.append({"키": key, "파일": it["원본파일"], "제목": it.get("분류결과", {}).get("제목") or it["원본파일"],
                       "쪽": str(c.get("쪽") or "전체"), "이유": c.get("이유", "")})
    return chosen


# ───────────────────────── 강의노트 생성 (Claude) ─────────────────────────

NOTE_SYSTEM = (
    "너는 대학 강의의 녹취록·강의자료·학생 필기를 바탕으로 복습용 강의노트를 작성하는 조교다. "
    "원칙: (1) 녹취·자료·필기에 실제로 있는 내용만 쓴다. 네 배경지식으로 내용을 보충하거나 빈 곳을 메우지 않는다. "
    "(2) 모든 항목에 근거 위치를 단다. (3) 음성인식 오류가 많은 녹취이므로 용어는 자료와 문맥으로 바로잡되, "
    "확신이 없으면 '불확실한 부분'에 둔다. 출력은 지정된 형식의 마크다운만 쓴다."
)


def _material_block(items: dict, selection: list[dict]) -> tuple[str, list[dict], list[str]]:
    """선택된 자료 본문 → 프롬프트용 텍스트. (텍스트, 자료 메타[{번호,키,파일,쪽수}], 경고)"""
    blocks, meta, warnings, used = [], [], [], 0
    for i, sel in enumerate(selection, 1):
        it = items.get(sel["키"])
        if not it:
            continue
        doc = read_json(Path(it["폴더"]) / (it["원본파일"] + ".추출.json"))
        if not doc:
            warnings.append(f"자료 추출본 없음: {sel['파일']}")
            continue
        pages = parse_page_range(sel.get("쪽"), len(doc["쪽"]))
        text = pages_text(doc, pages)
        if used + len(text) > MATERIAL_CHARS_CAP:
            text = text[: max(0, MATERIAL_CHARS_CAP - used)]
            warnings.append(f"자료 본문 상한({MATERIAL_CHARS_CAP}자) 초과로 '{sel['파일']}' 일부만 포함")
        used += len(text)
        blocks.append(f"=== 자료{i}: {sel['제목']} ({sel['파일']}) — 포함 쪽: {sel.get('쪽', '전체')} ===\n{text}")
        meta.append({"번호": i, "키": sel["키"], "파일": sel["파일"], "제목": sel["제목"], "쪽수": len(doc["쪽"]), "쪽": sel.get("쪽", "전체")})
        if used >= MATERIAL_CHARS_CAP:
            break
    return "\n\n".join(blocks), meta, warnings


def _note_prompt(cfg, items: dict, s: dict, mat_text: str, mat_meta: list[dict]) -> str:
    recs = s.get("녹음", []) + s.get("영상", [])
    rec_blocks, total = [], 0.0
    for i, key in enumerate(recs, 1):
        it = items[key]
        start = (it.get("녹음시작") or "")[11:16]
        label = f"녹음{i}" if len(recs) > 1 else "녹음"
        rec_blocks.append(f"[{label}: {it['원본파일']}, {start} 시작, 길이 {it.get('길이', '?')}]\n{_recording_text(it)}")
        total += _recording_duration(it)
    note_blocks = []
    for i, key in enumerate(s.get("필기", []), 1):
        it = items[key]
        note_blocks.append(f"=== 필기{i}: {it['원본파일']} ===\n{read_text_any(Path(it['폴더']) / it['원본파일'])}")
    cite = "- 녹취: [hh:mm:ss]" + (" — 녹음이 여러 개이므로 [녹음2 hh:mm:ss] 처럼 번호를 붙인다" if len(recs) > 1 else "")
    n_q = cfg.get("notes", {}).get("questions", 5)
    return f"""과목: {s['강의명']}
날짜: {s['날짜']} ({s['요일']})
녹음: {len(recs)}개 (총 {fmt_ts(total)}) / 자료: {len(mat_meta)}개 / 필기: {len(note_blocks)}개

출력 형식 (반드시 이 순서. 머리말 3줄 + 구분선 + 본문):
제목: <이 회차를 한 구절로 나타내는 제목, 20자 내외>
한줄요약: <한 문장>
키워드: <쉼표로 구분한 핵심 용어 5~10개>
---
# <제목>
## 핵심 개념
- **개념명**: 설명 (근거)
## 수업 흐름
- [hh:mm:ss]~[hh:mm:ss] 다룬 내용
## 예시와 비유
- 교수가 든 예시·비유와 그것이 설명하는 개념 (근거)
## 자료 대응
| 자료 | 쪽 | 녹취 구간 | 내용 |
|---|---|---|---|
(자료가 없으면 이 절은 "없음")
## 공지·과제·시험 언급
- 날짜·범위·제출 방법 등 (근거). 없으면 "없음"
## 불확실한 부분
- [hh:mm:ss] "녹취 원문 짧게 인용" → 추정 해석
## 복습 질문
1. 질문 (근거 [hh:mm:ss])  … {n_q}개

근거 표기 규칙:
{cite}
- 자료: (자료1 p.5) — 번호와 쪽은 아래 자료 머리말의 '자료N'과 '=== p.N ===' 마커를 그대로 쓴다
- 필기: (필기)
- 녹취에는 없고 자료에만 있는 내용은 끝에 "(자료에만 있음)" 표시
- 해당 내용이 없는 절은 "없음"이라고만 쓴다. 항목을 지어내지 않는다.
- 분량: 본문 2000~6000자. 녹취가 길고 내용이 많을수록 상세하게.

<녹취록>
{chr(10).join(rec_blocks) if rec_blocks else '(녹음 없음)'}
</녹취록>

<자료>
{mat_text if mat_text else '(선택된 자료 없음)'}
</자료>

<필기>
{chr(10).join(note_blocks) if note_blocks else '(필기 없음)'}
</필기>"""


def _parse_note(text: str):
    """머리말(제목/한줄요약/키워드) + 본문 분리. 머리말이 없으면 본문에서 추정"""
    text = text.strip()
    header, body = {}, text
    m = re.match(r"^(.*?)\n-{3,}\s*\n(.*)$", text, re.S)
    if m and "제목" in m[1]:
        for ln in m[1].split("\n"):
            if ":" in ln:
                k, v = ln.split(":", 1)
                header[k.strip()] = v.strip()
        body = m[2].strip()
    if "제목" not in header:
        h1 = re.search(r"^#\s+(.*)$", body, re.M)
        header["제목"] = h1.group(1).strip() if h1 else ""
    kws = [k.strip() for k in re.split(r"[,、，]", header.get("키워드", "")) if k.strip()]
    return {"제목": header.get("제목", ""), "한줄요약": header.get("한줄요약", ""), "키워드": kws[:10]}, body


def check_note(body: str, total_seconds: float, mat_meta: list[dict]) -> list[str]:
    """근거 표기가 실제 범위 안에 있는지 등 기계적 검증 → 경고 목록"""
    warnings = []
    ts = re.findall(r"\[(?:녹음\d+ )?(\d{2}):(\d{2}):(\d{2})\]", body)
    secs = [int(h) * 3600 + int(m) * 60 + int(s) for h, m, s in ts]
    if total_seconds and secs:
        bad = [fmt_ts(x) for x in secs if x > total_seconds + 60]
        if bad:
            warnings.append(f"녹음 길이를 넘는 타임스탬프 {len(bad)}개 (예: {bad[0]})")
    mats = {m["번호"]: m for m in mat_meta}
    for n, p in re.findall(r"\(자료(\d+)\s*p\.?\s*(\d+)", body):
        n, p = int(n), int(p)
        if n not in mats:
            warnings.append(f"존재하지 않는 자료 번호 인용: 자료{n}")
            break
        if p > mats[n]["쪽수"]:
            warnings.append(f"자료{n} 쪽수({mats[n]['쪽수']}) 초과 인용: p.{p}")
            break
    sec = re.search(r"## 핵심 개념\n(.*?)(?=\n## |\Z)", body, re.S)
    if sec and not re.search(r"\[\d{2}:\d{2}:\d{2}\]|\(자료\d+|\(필기\)", sec.group(1)) and "없음" not in sec.group(1):
        warnings.append("'핵심 개념' 절에 근거 표기가 없음")
    if total_seconds and not secs:
        warnings.append("녹취 타임스탬프 인용이 전혀 없음")
    if len(body) < 800:
        warnings.append(f"노트가 매우 짧음 ({len(body)}자)")
    return warnings


def inputs_hash(items: dict, s: dict, selection: list[dict]) -> str:
    parts = [PROMPT_VERSION]
    for key in s.get("녹음", []) + s.get("영상", []) + s.get("필기", []):
        it = items[key]
        parts.append(f"{key}:{it.get('단계')}:{it.get('강의명')}")
    for sel in selection:
        parts.append(f"{sel['키']}:{sel.get('쪽')}")
    return sha1_text(*parts)


def generate_note(cfg, items: dict, s: dict, selection: list[dict], out_dir: Path) -> dict:
    """강의노트.md 생성 → 노트 메타 반환"""
    mat_text, mat_meta, warnings = _material_block(items, selection)
    prompt = _note_prompt(cfg, items, s, mat_text, mat_meta)
    timeout = max(cfg["claude"].get("timeout_sec", 600), 900)
    header, body = _parse_note(ask_claude(cfg, NOTE_SYSTEM, prompt, timeout=timeout))
    total = sum(_recording_duration(items[k]) for k in s.get("녹음", []) + s.get("영상", []))
    warnings += check_note(body, total, mat_meta)

    folder = session_folder(out_dir, s["강의명"], s["날짜"])
    folder.mkdir(parents=True, exist_ok=True)
    srcs = []
    for k in s.get("녹음", []) + s.get("영상", []):
        srcs.append(f"{items[k]['종류']}: {items[k]['원본파일']} ({items[k].get('길이', '?')})")
    for m in mat_meta:
        srcs.append(f"자료{m['번호']}: {m['제목']} ({m['파일']}, 쪽 {m['쪽']})")
    for k in s.get("필기", []):
        srcs.append(f"필기: {items[k]['원본파일']}")
    footer = "\n\n---\n## 출처\n" + "\n".join(f"- {x}" for x in srcs)
    if warnings:
        footer += "\n\n## 자동 검증 경고\n" + "\n".join(f"- {w}" for w in warnings)
    footer += f"\n\n_자동 생성: {dt.datetime.now():%Y-%m-%d %H:%M} · 녹취는 음성인식 결과라 오류가 있을 수 있음_"
    md = body + footer
    (folder / "강의노트.md").write_text(md, encoding="utf-8")
    meta = {**header, "경고": warnings, "자료": mat_meta, "생성시각": dt.datetime.now().isoformat(timespec="seconds"),
            "글자수": len(body)}
    write_json(folder / "회차정보.json", {"강의명": s["강의명"], "날짜": s["날짜"], "요일": s["요일"], "노트": meta,
                                        "녹음": [items[k]["원본파일"] for k in s.get("녹음", [])],
                                        "영상": [items[k]["원본파일"] for k in s.get("영상", [])],
                                        "필기": [items[k]["원본파일"] for k in s.get("필기", [])]})
    return meta
