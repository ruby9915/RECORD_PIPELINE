"""녹음·영상 → 텍스트 (faster-whisper)"""
import datetime as dt
import glob
import json
import os
import re
import sys
import time
from pathlib import Path

from .common import BASE, fmt_ts, log

_whisper_model = None


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
    """16kHz 모노 float32 파형. 영상 파일도 소리 트랙만 디코딩.
    (faster-whisper 내장 디코더는 PyAV 15+ 와 호환되지 않아 직접 처리)"""
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
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def hotwords_for(cfg, lecture, extra_terms=None):
    """강의별 용어 사전(config) + 해당 과목 자료에서 뽑은 핵심 용어 → 모든 구간에 적용되는 hotwords 문자열"""
    if not lecture:
        return None
    terms = [t.strip() for t in (cfg["whisper"].get("hotwords_by_lecture", {}).get(lecture) or "").split(",") if t.strip()]
    seen = {t.lower() for t in terms}
    for t in extra_terms or []:
        t = str(t).strip()
        if t and t.lower() not in seen and len(terms) < 60:
            terms.append(t)
            seen.add(t.lower())
    return f"{lecture} 강의입니다. {', '.join(terms)}" if terms else None


def _is_hotword_leak(text: str, hotwords: str) -> bool:
    """용어 목록을 그대로 읊은 환각 구간 판별: 단어 4개 이상이 전부 용어 사전에 있는 단어"""
    vocab = set(re.findall(r"[\w]+", hotwords))
    words = re.findall(r"[\w]+", text)
    return len(words) >= 4 and all(wd in vocab for wd in words)


def transcribe(cfg, media: Path, duration: float, work: Path, hotwords=None):
    """work/ 에 segments.json, 원본.txt, 원본_타임스탬프.txt 생성. (원본 텍스트, 경고 목록) 반환"""
    model = get_whisper(cfg)
    w = cfg["whisper"]
    segments, _ = model.transcribe(
        decode_audio(media),
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
