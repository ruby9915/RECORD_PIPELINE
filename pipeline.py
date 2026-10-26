"""
강의 정리 파이프라인: 녹음·영상·강의자료·필기 → 과목/회차별 정리 → 강의노트 → 노션

실행: run.bat            (새 파일만 처리)
      run.bat --dry-run  (처리 대상만 출력)
      run.bat --notion-check (노션 토큰·부모 페이지·데이터베이스 준비 확인)

흐름 (파일별 단계는 _state.json 에 기록 → 중단돼도 다음 실행에서 이어서):
  자료(pdf/pptx/docx/hwpx) : 추출 → 과목 분류+개요(Claude) → <과목>/자료/
  녹음·영상                : 변환(Whisper) → 과목 분류(시간표/캘린더 → Claude) → 교정(Claude) → <과목>/<날짜>/<파일>/
  필기(txt/md)             : 읽기 → 과목·날짜 분류(Claude) → <과목>/<날짜>/필기/
  회차(과목+날짜)          : 관련 자료 선택(Claude) → 강의노트.md 생성(Claude) → 노션 페이지 → 강의목록
"""
import argparse
import datetime as dt
import json
import shutil
import sys
import time
from pathlib import Path

from lecture.audio import audio_info, hotwords_for, transcribe
from lecture.classify import classify_material, classify_note, classify_recording, lecture_names, time_based_lecture
from lecture.common import (UNCLASSIFIED, WEEKDAYS, JsonStore, date_label, file_key, fmt_ts, load_config, log,
                            read_json, read_text_any, sanitize, session_folder, session_id, sha1_text, write_json)
from lecture.correct import align_timestamps, correct
from lecture.extract import SUPPORTED, UNSUPPORTED, extract_document, pages_text
from lecture.index import write_index
from lecture.notes import build_sessions, generate_note, inputs_hash, select_materials, subject_materials


# ───────────────────────── 수집 ─────────────────────────

def file_kind(cfg, path: Path):
    ext = path.suffix.lower()
    if ext in {e.lower() for e in cfg.get("audio_extensions", [])}:
        return "녹음"
    if ext in {e.lower() for e in cfg.get("video_extensions", [])}:
        return "영상"
    if ext in {e.lower() for e in cfg.get("note_extensions", [".txt", ".md"])}:
        return "필기"
    if ext in SUPPORTED or ext in UNSUPPORTED:
        return "자료"
    return None


def scan(cfg, out_dir: Path):
    now = time.time()
    found = []
    for src in cfg.get("source_dirs", []):
        src = Path(src)
        if not src.is_dir():
            log(f"⚠ 원본 폴더 없음(건너뜀): {src}")
            continue
        for p in sorted(src.rglob("*")):
            if not p.is_file() or p.name.startswith(("~$", ".")):
                continue
            if any(part.startswith((".", "_")) for part in p.relative_to(src).parts[:-1]):
                continue  # 숨김/동기화 폴더(.stfolder 등)
            if out_dir in p.parents:
                continue
            kind = file_kind(cfg, p)
            if not kind:
                continue
            if now - p.stat().st_mtime < cfg.get("skip_if_modified_within_sec", 120):
                log(f"건너뜀(녹음/다운로드/동기화 중일 수 있음): {p.name}")
                continue
            found.append((p, kind))
    return found


def material_terms(items: dict, lecture: str) -> list[str]:
    """해당 과목 자료에서 Claude 가 뽑은 핵심용어 (음성 인식·교정 보조)"""
    terms = []
    for it in items.values():
        if it.get("종류") == "자료" and it.get("강의명") == lecture:
            terms += it.get("분류결과", {}).get("핵심용어", []) or []
    return terms


def migrate_layout(cfg, items: dict, out_dir: Path):
    """이미 처리된 녹음을 현재 폴더 구조(<과목>/<날짜>/<파일>/)로 옮기고 수정본_타임스탬프를 보충"""
    for key, it in items.items():
        if it.get("단계") != "done" or it.get("종류") not in ("녹음", "영상") or not (it.get("강의명") and it.get("날짜")):
            continue
        expected = session_folder(out_dir, it["강의명"], it["날짜"]) / Path(it["원본파일"]).stem
        if Path(it.get("폴더", "")).resolve() != expected.resolve() or not (expected / "수정본_타임스탬프.txt").exists():
            log(f"폴더 구조 갱신: {it['원본파일']} → {expected.relative_to(out_dir)}")
            place_recording(cfg, it, Path(it["원본경로"]), out_dir / "_work" / key, out_dir)


def apply_overrides(items: dict, out_dir: Path):
    """수동지정.json: {"파일명": {"강의명": "...", "날짜": "YYYY-MM-DD"}} → 자동 분류 결과를 덮어씀"""
    ov = read_json(out_dir / "수동지정.json", {}) or {}
    for key, it in items.items():
        o = ov.get(it.get("원본파일", ""))
        if not o:
            continue
        changed = False
        if o.get("강의명") and o["강의명"] != it.get("강의명"):
            it["강의명"], changed = sanitize(o["강의명"]), True
        if o.get("날짜") and o["날짜"] != it.get("날짜"):
            it["날짜"], changed = o["날짜"], True
            it["요일"] = WEEKDAYS[dt.date.fromisoformat(o["날짜"]).weekday()]
        if changed:
            it["분류방법"] = "수동지정"
            it["분류결과"] = {"신뢰도": "높음", "근거": "수동지정.json"}
            it["단계"] = {"녹음": "corrected", "영상": "corrected"}.get(it.get("종류"), "classified")
            log(f"수동지정 적용: {it['원본파일']} → {it.get('강의명')} {it.get('날짜', '')}")


# ───────────────────────── 녹음 · 영상 ─────────────────────────

def _movable(old, out_dir: Path) -> bool:
    """이전 위치가 결과 폴더 안에 있을 때만 이동 (상태 파일이 잘못돼도 바깥 폴더를 건드리지 않도록)"""
    try:
        return bool(old) and Path(old).exists() and out_dir.resolve() in Path(old).resolve().parents
    except OSError:
        return False


def place_recording(cfg, it: dict, media: Path, work: Path, out_dir: Path):
    folder = session_folder(out_dir, it["강의명"], it["날짜"]) / media.stem
    old = it.get("폴더")
    if _movable(old, out_dir) and Path(old).resolve() != folder.resolve():  # 재분류·구조 변경 시 이동
        folder.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(old, folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("원본.txt", "원본_타임스탬프.txt", "수정본.txt"):
        if (work / name).exists():
            shutil.copy2(work / name, folder / name)
    segs = read_json(work / "segments.json")
    if segs is not None and (folder / "수정본.txt").exists():
        stamped = align_timestamps(segs, (folder / "수정본.txt").read_text(encoding="utf-8"))
        (folder / "수정본_타임스탬프.txt").write_text(stamped, encoding="utf-8")
    want_copy = cfg.get("copy_audio", True) if it["종류"] == "녹음" else cfg.get("copy_video", False)
    if want_copy and media.exists() and not (folder / media.name).exists():
        shutil.copy2(media, folder / media.name)
    it["폴더"] = str(folder)
    write_json(folder / "정보.json", {k: v for k, v in it.items() if k != "폴더"})


def process_recording(cfg, items: dict, media: Path, key: str, kind: str, out_dir: Path, reclassify=False):
    it = items.setdefault(key, {"단계": "new"})
    it.update({"종류": kind, "원본파일": media.name, "원본경로": str(media)})
    work = out_dir / "_work" / key
    work.mkdir(parents=True, exist_ok=True)

    if "녹음시작" not in it:
        start, dur, src = audio_info(media)
        it.update({"녹음시작": start.isoformat(timespec="seconds"), "날짜": f"{start:%Y-%m-%d}",
                   "요일": WEEKDAYS[start.weekday()], "길이": fmt_ts(dur), "길이초": round(dur, 1), "시각출처": src})
    start = dt.datetime.fromisoformat(it["녹음시작"])
    # 온라인 강의 영상은 수업 시간과 무관 → 시각 기반 분류는 녹음에만
    tb = time_based_lecture(cfg, out_dir, start, it["길이초"]) if kind == "녹음" else None

    if it["단계"] == "new":
        hw = hotwords_for(cfg, tb[0] if tb else None, material_terms(items, tb[0]) if tb else None)
        log(f"[1/4] 텍스트 변환: {media.name} (길이 {it['길이']})" + (f" — 용어 사전: {tb[0]}" if hw else ""))
        _, leaks = transcribe(cfg, media, it["길이초"], work, hotwords=hw)
        it["변환경고"] = leaks
        it["단계"] = "transcribed"
    raw = (work / "원본.txt").read_text(encoding="utf-8")
    if not raw.strip():
        it.update({"강의명": UNCLASSIFIED, "분류방법": "인식된 음성 없음", "경고": ["인식된 텍스트 없음"]})
        (work / "수정본.txt").write_text("", encoding="utf-8")
        it["단계"] = "corrected"

    if it["단계"] == "transcribed" or (reclassify and raw.strip()):
        if tb:
            it.update({"강의명": tb[0], "분류방법": tb[1], "분류결과": tb[2]})
        else:
            log(f"[2/4] 과목 분류(Claude): {media.name}")
            name, res = classify_recording(cfg, raw, start, lecture_names(cfg, items), media.name)
            it.update({"강의명": name, "분류방법": "Claude 내용 분석", "분류결과": res})
        log(f"  → {it['강의명']} ({it['분류방법']})")
        if it["단계"] == "transcribed":
            it["단계"] = "classified"

    if it["단계"] == "classified":
        log(f"[3/4] 오탈자 교정(Claude): {media.name}")
        terms = hotwords_for(cfg, it["강의명"], material_terms(items, it["강의명"])) or ""
        _, warnings = correct(cfg, it["강의명"], raw, work, terms=terms.split("강의입니다. ")[-1])
        it["경고"] = it.get("변환경고", []) + warnings
        it["단계"] = "corrected"

    if it["단계"] in ("corrected", "done"):
        log(f"[4/4] 정리: {it['강의명']} / {date_label(it['날짜'])} / {media.stem}")
        place_recording(cfg, it, media, work, out_dir)
        it["단계"] = "done"


# ───────────────────────── 강의자료 ─────────────────────────

def place_material(it: dict, src: Path, work: Path, out_dir: Path):
    folder = out_dir / it["강의명"] / "자료"
    old = it.get("폴더")
    if _movable(old, out_dir) and Path(old).resolve() != folder.resolve():
        for suffix in ("", ".추출.json", ".추출.txt", ".개요.json"):
            f = Path(old) / (src.name + suffix)
            if f.exists():
                folder.mkdir(parents=True, exist_ok=True)
                shutil.move(f, folder / f.name)
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / src.name).exists():
        shutil.copy2(src, folder / src.name)
    if (work / "추출.json").exists():
        shutil.copy2(work / "추출.json", folder / (src.name + ".추출.json"))
        doc = read_json(work / "추출.json")
        (folder / (src.name + ".추출.txt")).write_text(pages_text(doc), encoding="utf-8")
    it["폴더"] = str(folder)
    write_json(folder / (src.name + ".개요.json"), {k: v for k, v in it.items() if k != "폴더"})


def process_material(cfg, items: dict, src: Path, key: str, out_dir: Path, reclassify=False):
    it = items.setdefault(key, {"단계": "new"})
    it.update({"종류": "자료", "원본파일": src.name, "원본경로": str(src)})
    work = out_dir / "_work" / key
    work.mkdir(parents=True, exist_ok=True)

    if src.suffix.lower() in UNSUPPORTED:
        it.update({"강의명": UNCLASSIFIED, "분류방법": "미지원 형식", "경고": [UNSUPPORTED[src.suffix.lower()]],
                   "분류결과": {"근거": UNSUPPORTED[src.suffix.lower()]}})
        log(f"[자료] 미지원 형식: {src.name} — {UNSUPPORTED[src.suffix.lower()]}")
        place_material(it, src, work, out_dir)
        it["단계"] = "done"
        return

    if it["단계"] == "new":
        log(f"[자료 1/3] 텍스트 추출: {src.name}")
        doc = extract_document(src)
        write_json(work / "추출.json", doc)
        it.update({"쪽수": len(doc["쪽"]), "쪽단위": doc["쪽단위"], "경고": doc["경고"]})
        it["단계"] = "extracted"
    if it["단계"] == "extracted" or reclassify:
        doc = read_json(work / "추출.json")
        if not any(p["text"].strip() for p in doc["쪽"]):
            it.update({"강의명": UNCLASSIFIED, "분류방법": "텍스트 없음", "분류결과": {"근거": "추출된 텍스트 없음"}})
        else:
            log(f"[자료 2/3] 과목 분류·개요(Claude): {src.name}")
            name, res = classify_material(cfg, doc, src.name, lecture_names(cfg, items))
            it.update({"강의명": name, "분류방법": "Claude 내용 분석", "분류결과": res})
        log(f"  → {it['강의명']} / {it.get('분류결과', {}).get('제목', '')}")
        if it["단계"] == "extracted":
            it["단계"] = "classified"
    if it["단계"] in ("classified", "done"):
        log(f"[자료 3/3] 정리: {it['강의명']} / 자료 / {src.name}")
        place_material(it, src, work, out_dir)
        it["단계"] = "done"


# ───────────────────────── 필기 ─────────────────────────

def place_note(it: dict, src: Path, out_dir: Path):
    if it["강의명"] == UNCLASSIFIED or not it.get("날짜"):
        folder = out_dir / UNCLASSIFIED / "필기"
    else:
        folder = session_folder(out_dir, it["강의명"], it["날짜"]) / "필기"
    old = it.get("폴더")
    if _movable(old, out_dir) and (Path(old) / src.name).exists() and Path(old).resolve() != folder.resolve():
        folder.mkdir(parents=True, exist_ok=True)
        shutil.move(Path(old) / src.name, folder / src.name)
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / src.name).exists():
        shutil.copy2(src, folder / src.name)
    it["폴더"] = str(folder)
    write_json(folder / (src.name + ".정보.json"), {k: v for k, v in it.items() if k != "폴더"})


def process_note(cfg, items: dict, src: Path, key: str, out_dir: Path, reclassify=False):
    it = items.setdefault(key, {"단계": "new"})
    it.update({"종류": "필기", "원본파일": src.name, "원본경로": str(src)})
    if it["단계"] == "new" or reclassify:
        text = read_text_any(src)
        mdate = dt.datetime.fromtimestamp(src.stat().st_mtime).date().isoformat()
        if not text.strip():
            it.update({"강의명": UNCLASSIFIED, "분류방법": "빈 파일", "분류결과": {"근거": "내용 없음"}})
        else:
            log(f"[필기 1/2] 과목·날짜 분류(Claude): {src.name}")
            name, res = classify_note(cfg, text, src.name, mdate, lecture_names(cfg, items))
            it.update({"강의명": name, "분류방법": "Claude 내용 분석", "분류결과": res})
        date = it.get("분류결과", {}).get("날짜") or mdate
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            date = mdate
        it.update({"날짜": date, "요일": WEEKDAYS[dt.date.fromisoformat(date).weekday()],
                   "날짜출처": "필기 내용" if it.get("분류결과", {}).get("날짜") else "파일 수정일"})
        log(f"  → {it['강의명']} / {date_label(date)}")
        it["단계"] = "classified"
    if it["단계"] in ("classified", "done"):
        log(f"[필기 2/2] 정리: {it['강의명']} / {date_label(it['날짜'])} / {src.name}")
        place_note(it, src, out_dir)
        it["단계"] = "done"


# ───────────────────────── 회차 노트 · 노션 ─────────────────────────

def process_sessions(cfg, items: dict, sessions: JsonStore, nstore: JsonStore, out_dir: Path, args):
    groups = build_sessions(items, sessions.data)
    sessions.save()
    notion_client = db_id = None
    ncfg = cfg.get("notion", {})
    if ncfg.get("enabled") and not args.no_notion:
        try:
            from lecture.notion_sync import Notion, ensure_database, get_token
            notion_client = Notion(get_token(cfg))
            db_id = ensure_database(cfg, nstore.data, notion_client)
            nstore.save()
        except Exception as e:
            log(f"⚠ 노션 연결 실패 — 이번 실행에서는 업로드를 건너뜁니다: {e}")
            notion_client = None

    made, max_notes, failed = 0, args.max_notes if args.max_notes is not None else cfg.get("notes", {}).get("max_per_run", 20), []
    for sid in sorted(groups):
        s = sessions.data[sid]
        if args.session and sid != args.session:
            continue
        if not (s.get("녹음") or s.get("영상") or s.get("필기")):
            continue
        try:
            # 1) 관련 자료 선택 (과목 자료 목록이나 회차 구성이 바뀌었을 때만 Claude 호출)
            mats = subject_materials(items, s["강의명"])
            sel_hash = sha1_text(*(k for k, _ in mats), *(s.get("녹음", []) + s.get("영상", []) + s.get("필기", [])))
            if not mats:
                s["자료선택"], s["자료선택해시"] = [], sel_hash
            elif s.get("자료선택해시") != sel_hash:
                log(f"[회차] 자료 선택(Claude): {sid}")
                s["자료선택"] = select_materials(cfg, items, s)
                s["자료선택해시"] = sel_hash
                log(f"  → {len(s['자료선택'])}개 선택: " + ", ".join(f"{m['파일']}({m['쪽']})" for m in s["자료선택"]))
                sessions.save()
            # 2) 강의노트 생성 (입력이 바뀌었을 때만)
            h = inputs_hash(items, s, s.get("자료선택", []))
            if (not args.no_notes) and (args.renote or (s.get("노트") or {}).get("해시") != h):
                if made >= max_notes:
                    log(f"[회차] 노트 생성 상한({max_notes}개) 도달 — 다음 실행에서 계속: {sid}")
                    continue
                log(f"[회차] 강의노트 생성(Claude): {sid}")
                meta = generate_note(cfg, items, s, s.get("자료선택", []), out_dir)
                meta["해시"] = h
                s["노트"] = meta
                made += 1
                log(f"  → '{meta.get('제목')}' {meta.get('글자수')}자" + (f" ⚠ {len(meta['경고'])}건" if meta.get("경고") else ""))
                sessions.save()
            # 3) 노션 업로드 (노트가 바뀌었을 때만)
            if notion_client and s.get("노트"):
                uploaded = (nstore.data.get("pages") or {}).get(sid, {}).get("업로드해시")
                if args.resync_notion or uploaded != s["노트"].get("해시"):
                    log(f"[회차] 노션 업로드: {sid}")
                    url = _sync_to_notion(cfg, items, s, sid, nstore.data, notion_client, db_id, out_dir)
                    nstore.save()
                    log(f"  → {url}")
        except Exception as e:
            log(f"실패(회차 {sid}): {e} (다음 실행 때 다시 시도)")
            failed.append(sid)
            sessions.save()
    return failed


def _sync_to_notion(cfg, items, s, sid, nstore, client, db_id, out_dir):
    from lecture.notion_sync import sync_session

    folder = session_folder(out_dir, s["강의명"], s["날짜"])
    md = (folder / "강의노트.md").read_text(encoding="utf-8")
    kinds = [k for k in ("녹음", "영상", "필기") if s.get(k)] + (["자료"] if s.get("자료선택") else [])
    transcripts = []
    if cfg.get("notion", {}).get("include_transcript", True):
        for key in s.get("녹음", []) + s.get("영상", []):
            f = Path(items[key]["폴더"]) / "수정본_타임스탬프.txt"
            if f.exists():
                transcripts.append((f"전체 녹취록(수정본) — {items[key]['원본파일']}", f.read_text(encoding="utf-8")))
        for key in s.get("필기", []):
            f = Path(items[key]["폴더"]) / items[key]["원본파일"]
            if f.exists():
                transcripts.append((f"필기 원문 — {items[key]['원본파일']}", read_text_any(f)))
    return sync_session(cfg, nstore, client, db_id, s, sid, s["노트"], md, kinds, str(folder), transcripts)


# ───────────────────────── 메인 ─────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="처리 대상만 출력")
    ap.add_argument("--only", help="이 이름의 파일만 처리")
    ap.add_argument("--redo", action="store_true", help="이미 처리된 파일도 처음부터 다시 (모델·용어 사전 변경 후)")
    ap.add_argument("--reclassify", action="store_true", help="이미 처리된 파일도 과목 분류 다시 수행")
    ap.add_argument("--no-notes", action="store_true", help="강의노트 생성 건너뜀")
    ap.add_argument("--renote", action="store_true", help="입력이 안 바뀌었어도 강의노트 다시 생성")
    ap.add_argument("--session", help="이 회차만 노트 생성/업로드 (형식: 과목|YYYY-MM-DD)")
    ap.add_argument("--max-notes", type=int, help="이번 실행에서 생성할 노트 수 상한 (기본 config notes.max_per_run)")
    ap.add_argument("--no-notion", action="store_true", help="노션 업로드 건너뜀")
    ap.add_argument("--resync-notion", action="store_true", help="모든 노트를 노션에 다시 업로드")
    ap.add_argument("--notion-check", action="store_true", help="노션 연결 상태만 확인하고 종료")
    ap.add_argument("--config", help="설정 파일 경로 (기본 config.json)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    out_dir = Path(cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    state = JsonStore(out_dir / "_state.json")        # 파일별 처리 기록
    sessions = JsonStore(out_dir / "_sessions.json")  # 회차별 노트·자료선택 기록
    nstore = JsonStore(out_dir / "_notion.json")      # 노션 데이터베이스·페이지 id
    items = state.data

    if args.notion_check:
        from lecture.notion_sync import check_setup
        info = check_setup(cfg, nstore.data)
        nstore.save()
        log("노션 연결 확인: " + json.dumps(info, ensure_ascii=False))
        return

    # 구버전 기록 호환: 종류 없는 항목은 녹음
    for it in items.values():
        it.setdefault("종류", "녹음")
    apply_overrides(items, out_dir)
    if not args.dry_run:
        migrate_layout(cfg, items, out_dir)
        state.save()

    todo = []
    for path, kind in scan(cfg, out_dir):
        if args.only and path.name != args.only:
            continue
        key = file_key(path)
        it = items.get(key)
        if it and args.redo:
            it["단계"] = "new"
            shutil.rmtree(out_dir / "_work" / key / "corrected", ignore_errors=True)
        elif it and it.get("단계") == "done" and not args.reclassify:
            continue
        todo.append((path, kind, key))

    log(f"처리 대상 {len(todo)}개" + (": " + ", ".join(f"{p.name}[{k}]" for p, k, _ in todo) if todo else " (새 파일 없음)"))
    if args.dry_run:
        return

    failed = []
    order = {"자료": 0, "녹음": 1, "영상": 1, "필기": 2}  # 자료의 핵심용어가 녹음 인식에 쓰이도록 자료 먼저
    for path, kind, key in sorted(todo, key=lambda t: (order[t[1]], t[0].name)):
        try:
            if kind in ("녹음", "영상"):
                process_recording(cfg, items, path, key, kind, out_dir, reclassify=args.reclassify)
            elif kind == "자료":
                process_material(cfg, items, path, key, out_dir, reclassify=args.reclassify)
            else:
                process_note(cfg, items, path, key, out_dir, reclassify=args.reclassify)
        except Exception as e:
            log(f"실패: {path.name} — {e} (다음 실행 때 이어서 진행)")
            failed.append(path.name)
        state.save()

    failed += process_sessions(cfg, items, sessions, nstore, out_dir, args)
    write_index(items, sessions.data, out_dir, nstore.data)
    log(f"완료. 목록: {out_dir / '강의목록.md'}" + (f" / 실패 {len(failed)}개: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
