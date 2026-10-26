"""Claude Code CLI(-p) 를 통한 Claude 호출 — 로그인된 claude.ai 계정 사용량에서 차감"""
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .common import log


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


def ask_claude(cfg, system: str, prompt: str, timeout: int | None = None) -> str:
    exe = find_claude(cfg)
    cmd = [exe, "-p", "--tools", "", "--system-prompt", system,
           "--no-session-persistence", "--setting-sources", "", "--output-format", "json"]
    if cfg["claude"].get("model"):
        cmd += ["--model", cfg["claude"]["model"]]
    timeout = timeout or cfg["claude"].get("timeout_sec", 600)
    last_err = None
    for attempt in range(3):
        p = None
        try:
            # 프로젝트 CLAUDE.md 등이 섞이지 않게 빈 임시 폴더에서 실행
            with tempfile.TemporaryDirectory() as tmp:
                p = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True,
                                   cwd=tmp, timeout=timeout)
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
    """응답 본문에서 JSON 객체 하나를 꺼냄 (코드블록 표시 등 앞뒤 잡음 허용)"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"JSON 응답 아님: {text[:200]}")
    return json.loads(m.group(0))
