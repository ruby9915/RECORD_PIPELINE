"""공통 유틸: 로그, 설정, JSON 상태 저장, 경로 규칙"""
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
UNCLASSIFIED = "미분류"

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_config(path=None):
    with open(path or BASE / "config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    # 구버전 설정(source_dir 하나) 호환
    if "source_dirs" not in cfg and cfg.get("source_dir"):
        cfg["source_dirs"] = [cfg["source_dir"]]
    return cfg


def load_secrets() -> dict:
    """secrets.json (git 제외) — 노션 토큰 등 비밀값"""
    p = BASE / "secrets.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


class JsonStore:
    """JSON 파일 하나를 dict 로 다루는 상태 저장소 (임시 파일에 쓴 뒤 교체 → 중단돼도 깨지지 않음)"""

    def __init__(self, path: Path):
        self.path = path
        self.data = {}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def write_json(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path, default=None):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def file_key(path: Path) -> str:
    """파일 이름이 바뀌어도 같은 파일로 인식하도록 크기 + 앞 4MB 내용으로 해시"""
    h = hashlib.sha1(str(path.stat().st_size).encode())
    with open(path, "rb") as f:
        h.update(f.read(4 * 1024 * 1024))
    return h.hexdigest()[:16]


def sha1_text(*parts) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    return name or UNCLASSIFIED


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def date_label(date_str: str) -> str:
    """'2026-09-15' → '2026-09-15(화)'"""
    d = dt.date.fromisoformat(date_str)
    return f"{date_str}({WEEKDAYS[d.weekday()]})"


def session_id(lecture: str, date_str: str) -> str:
    return f"{lecture}|{date_str}"


def session_folder(out_dir: Path, lecture: str, date_str: str) -> Path:
    return out_dir / lecture / date_label(date_str)


def read_text_any(path: Path) -> str:
    """BOM/인코딩을 추정해 텍스트 파일 읽기 (UTF-8 → CP949 순)"""
    data = path.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
