"""Claude 오탈자 교정 (청크 단위, 중단 시 이어서) + 교정본에 타임스탬프 되붙이기"""
import concurrent.futures as cf
import difflib
import re
from pathlib import Path

from .claude_cli import ask_claude
from .common import UNCLASSIFIED, fmt_ts, log

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


def correct_chunk(cfg, lecture: str, chunk: str, idx: int, total: int, terms: str = ""):
    prompt = f"""강의명: {lecture if lecture != UNCLASSIFIED else "알 수 없음"}
{('이 강의에 나오는 용어(참고): ' + terms) if terms else ''}
아래는 강의 녹취록의 {idx + 1}/{total} 번째 부분이다.

교정 규칙:
- 음성인식 오류로 잘못 적힌 단어(특히 전문용어, 고유명사), 맞춤법, 띄어쓰기, 문장부호를 고친다.
- 내용을 요약·삭제·추가·재배열하지 않는다. 말투(구어체)와 반복 표현도 그대로 둔다.
- 확신이 없는 부분은 원문을 유지한다.
- 줄 구성은 원문과 똑같이 유지한다 (줄을 합치거나 나누지 않는다).

<원문>
{chunk}
</원문>"""
    ratio = 0.0
    for attempt in range(2):
        out = ask_claude(cfg, CORRECT_SYSTEM, prompt).strip()
        out = re.sub(r"^</?원문>|</?원문>$", "", out).strip()
        ratio = len(out) / max(len(chunk), 1)
        if 0.8 <= ratio <= 1.25:
            return out, None
        log(f"  청크 {idx + 1}: 길이 비율 {ratio:.2f} 이상 → 재시도")
    # 요약/누락 의심 → 내용 손실을 막기 위해 원문 유지
    return chunk, f"청크 {idx + 1}: 교정 결과 길이 비정상(비율 {ratio:.2f}) → 원문 유지"


def correct(cfg, lecture: str, raw: str, work: Path, terms: str = ""):
    """청크별 결과를 work/corrected/ 에 저장 → 중단돼도 끝난 청크는 재사용. (교정본, 경고) 반환"""
    cdir = work / "corrected"
    cdir.mkdir(exist_ok=True)
    chunks = split_chunks(raw, cfg["claude"].get("chunk_chars", 3000))
    warnings = []

    def job(i):
        f = cdir / f"{i:04d}.txt"
        if f.exists():
            return i, f.read_text(encoding="utf-8"), None
        out, warn = correct_chunk(cfg, lecture, chunks[i], i, len(chunks), terms)
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


def align_timestamps(segments: list[dict], corrected: str) -> str:
    """원본 구간(segments)의 시각을 교정본 줄에 되붙임.
    줄 수가 같으면 1:1, 다르면 difflib 로 대응 줄을 찾고 대응이 없는 줄은 직전 시각을 이어받음"""
    orig = [s["text"] for s in segments]
    stamps = [fmt_ts(s["start"]) for s in segments]
    corr = corrected.split("\n") if corrected else []
    if len(corr) == len(orig):
        return "\n".join(f"[{t}] {c}" for t, c in zip(stamps, corr))
    out, last = [], "00:00:00"
    sm = difflib.SequenceMatcher(None, orig, corr, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            continue
        for k, j in enumerate(range(j1, j2)):
            if tag == "equal":
                last = stamps[i1 + k]
            elif tag == "replace":
                last = stamps[min(i1 + k, i2 - 1)]
            out.append(f"[{last}] {corr[j]}")
    return "\n".join(out)
