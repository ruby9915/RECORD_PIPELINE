"""강의목록.md / .csv — 회차·자료 일람"""
import csv
import datetime as dt
import os
from pathlib import Path

from .common import UNCLASSIFIED, date_label, session_folder


def _rel(p, out_dir):
    try:
        return os.path.relpath(p, out_dir).replace("\\", "/")
    except ValueError:
        return str(p)


def write_index(items: dict, sessions: dict, out_dir: Path, nstore: dict):
    rows = []
    for sid, s in sessions.items():
        if s.get("항목없음"):
            continue
        folder = session_folder(out_dir, s["강의명"], s["날짜"])
        meta = s.get("노트") or {}
        page = (nstore.get("pages") or {}).get(sid) or {}
        rows.append({
            "강의명": s["강의명"], "날짜": s["날짜"], "요일": s["요일"],
            "제목": meta.get("제목", ""), "한줄요약": meta.get("한줄요약", ""),
            "녹음": len(s.get("녹음", [])), "영상": len(s.get("영상", [])),
            "자료": len(s.get("자료선택", [])), "필기": len(s.get("필기", [])),
            "노트": _rel(folder / "강의노트.md", out_dir) if (folder / "강의노트.md").exists() else "",
            "노션": page.get("url", ""),
            "경고": " / ".join(meta.get("경고", []) + s.get("경고", [])),
        })
    rows.sort(key=lambda r: (r["강의명"], r["날짜"]))
    cols = ["강의명", "날짜", "요일", "제목", "한줄요약", "녹음", "영상", "자료", "필기", "노트", "노션", "경고"]
    with open(out_dir / "강의목록.csv", "w", encoding="utf-8-sig", newline="") as f:  # 엑셀 호환
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    md = ["# 강의 정리 목록", "", f"갱신: {dt.datetime.now():%Y-%m-%d %H:%M}", ""]
    lectures = sorted({r["강의명"] for r in rows} | {it["강의명"] for it in items.values()
                                                     if it.get("종류") == "자료" and it.get("강의명")})
    for lec in lectures:
        md += [f"## {lec}", ""]
        srows = [r for r in rows if r["강의명"] == lec]
        if srows:
            md += ["| 날짜 | 요일 | 제목 | 녹음 | 영상 | 자료 | 필기 | 노트 | 노션 |", "|---|---|---|---|---|---|---|---|---|"]
            for r in srows:
                note = f"[노트](<{r['노트']}>)" if r["노트"] else ""
                notion = f"[열기]({r['노션']})" if r["노션"] else ""
                md.append(f"| {r['날짜']} | {r['요일']} | {r['제목']}{' ⚠' if r['경고'] else ''} | {r['녹음']} | {r['영상']} "
                          f"| {r['자료']} | {r['필기']} | {note} | {notion} |")
            md.append("")
        mats = [it for it in items.values() if it.get("종류") == "자료" and it.get("강의명") == lec and it.get("단계") == "done"]
        if mats:
            md += ["**자료**", ""]
            for it in sorted(mats, key=lambda x: x["원본파일"]):
                r = it.get("분류결과", {})
                p = Path(it["폴더"]) / it["원본파일"]
                md.append(f"- [{r.get('제목') or it['원본파일']}](<{_rel(p, out_dir)}>) — {r.get('종류', '')}"
                          f"{', ' + str(r.get('주차')) + '주차' if r.get('주차') else ''}"
                          f"{' ⚠ ' + ' / '.join(it['경고']) if it.get('경고') else ''}")
            md.append("")
    unclassified = [it for it in items.values() if it.get("강의명") == UNCLASSIFIED and it.get("단계") == "done"]
    if unclassified:
        md += ["## 미분류 (직접 확인 필요)", ""]
        for it in sorted(unclassified, key=lambda x: x["원본파일"]):
            why = it.get("분류결과", {}).get("근거") or " / ".join(it.get("경고", []))
            md.append(f"- {it['종류']}: {it['원본파일']} — {why}")
        md.append("")
    md += ["---", "- 자동 분류 결과를 바꾸려면 `수동지정.json` 에 `{\"파일명\": {\"강의명\": \"...\", \"날짜\": \"YYYY-MM-DD\"}}` 를 적고 다시 실행", ""]
    (out_dir / "강의목록.md").write_text("\n".join(md), encoding="utf-8")
