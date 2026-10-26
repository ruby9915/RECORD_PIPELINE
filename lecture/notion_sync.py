"""노션 업로드: 회차 노트 → 데이터베이스 페이지 (Notion REST API, 내부 통합 토큰 사용)"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from .common import load_secrets, log

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"   # 구버전이지만 계속 지원됨. 데이터베이스 1개 = 데이터 소스 1개인 단순 구조
DB_TITLE = "강의 노트"
MAX_TEXT = 1900                  # 리치텍스트 1개 2,000자 제한
MAX_CHILDREN = 100               # 요청당 children 100개 제한
CODE_LANGS = {"python": "python", "py": "python", "sql": "sql", "js": "javascript", "javascript": "javascript",
              "java": "java", "c": "c", "cpp": "c++", "c++": "c++", "bash": "shell", "sh": "shell", "shell": "shell",
              "json": "json", "html": "html", "css": "css", "yaml": "yaml", "xml": "xml", "r": "r", "go": "go"}

DB_PROPERTIES = {
    "이름": {"title": {}},
    "과목": {"select": {}},
    "날짜": {"date": {}},
    "자료": {"multi_select": {"options": [{"name": n} for n in ("녹음", "영상", "자료", "필기")]}},
    "키워드": {"multi_select": {}},
    "한줄요약": {"rich_text": {}},
    "검토 필요": {"checkbox": {}},
    "경고": {"rich_text": {}},
    "회차ID": {"rich_text": {}},
    "폴더": {"rich_text": {}},
}


def get_token(cfg) -> str:
    env = cfg.get("notion", {}).get("token_env", "NOTION_TOKEN")
    tok = os.environ.get(env) or load_secrets().get("notion_token")
    if not tok:
        raise RuntimeError("노션 토큰이 없습니다. secrets.json 의 notion_token 또는 환경변수 NOTION_TOKEN 을 설정하세요.")
    return tok


def id_from_url(url_or_id: str) -> str:
    m = re.findall(r"[0-9a-fA-F]{32}", url_or_id.replace("-", ""))
    if not m:
        raise ValueError(f"노션 페이지 ID를 찾을 수 없음: {url_or_id}")
    h = m[-1].lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


class Notion:
    def __init__(self, token: str):
        self.token = token
        self._last = 0.0

    def req(self, method: str, path: str, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Notion-Version": NOTION_VERSION,
                   "Content-Type": "application/json"}
        for attempt in range(5):
            wait = 0.35 - (time.time() - self._last)   # 평균 3회/초 제한
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            r = urllib.request.Request(API + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(r, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                if e.code == 429 or e.code >= 500:
                    delay = float(e.headers.get("Retry-After") or 2 * (attempt + 1))
                    log(f"  노션 {e.code} → {delay:.0f}초 후 재시도")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"노션 API 오류 {e.code} {method} {path}: {text[:400]}") from None
            except urllib.error.URLError as e:
                log(f"  노션 연결 실패({attempt + 1}/5): {e}")
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"노션 API 재시도 초과: {method} {path}")

    # ── 데이터베이스 ──
    def find_database(self, parent_page_id: str, title: str):
        cursor = None
        while True:
            body = {"query": title, "filter": {"value": "database", "property": "object"}, "page_size": 50}
            if cursor:
                body["start_cursor"] = cursor
            res = self.req("POST", "/search", body)
            for db in res.get("results", []):
                t = "".join(x.get("plain_text", "") for x in db.get("title", []))
                if t == title and db.get("parent", {}).get("page_id") == parent_page_id and not db.get("archived"):
                    return db["id"]
            if not res.get("has_more"):
                return None
            cursor = res.get("next_cursor")

    def create_database(self, parent_page_id: str, title: str) -> str:
        res = self.req("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": DB_PROPERTIES,
        })
        return res["id"]

    def query_by_session(self, db_id: str, sid: str):
        res = self.req("POST", f"/databases/{db_id}/query",
                       {"filter": {"property": "회차ID", "rich_text": {"equals": sid}}, "page_size": 5})
        for p in res.get("results", []):
            if not p.get("archived"):
                return p
        return None

    # ── 페이지 ──
    def create_page(self, db_id: str, props: dict, children: list) -> dict:
        page = self.req("POST", "/pages", {"parent": {"database_id": db_id}, "properties": props,
                                           "children": children[:MAX_CHILDREN]})
        self.append_children(page["id"], children[MAX_CHILDREN:])
        return page

    def update_page(self, page_id: str, props: dict):
        return self.req("PATCH", f"/pages/{page_id}", {"properties": props})

    def append_children(self, block_id: str, children: list):
        for i in range(0, len(children), MAX_CHILDREN):
            self.req("PATCH", f"/blocks/{block_id}/children", {"children": children[i:i + MAX_CHILDREN]})

    def list_children(self, block_id: str) -> list:
        blocks, cursor = [], None
        while True:
            q = f"/blocks/{block_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            res = self.req("GET", q)
            blocks += res.get("results", [])
            if not res.get("has_more"):
                return blocks
            cursor = res.get("next_cursor")

    def clear_children(self, block_id: str):
        ids = [b["id"] for b in self.list_children(block_id)]
        for bid in ids:
            self.req("DELETE", f"/blocks/{bid}")
        return len(ids)


# ───────────────────────── 마크다운 → 노션 블록 ─────────────────────────

def _rt(text: str, bold=False, code=False, link=None) -> list:
    """리치텍스트 조각 (2,000자 제한에 맞춰 분할). 빈 문자열이면 빈 배열"""
    out = []
    for i in range(0, len(text), MAX_TEXT):
        obj = {"type": "text", "text": {"content": text[i:i + MAX_TEXT]}}
        if link:
            obj["text"]["link"] = {"url": link}
        ann = {}
        if bold:
            ann["bold"] = True
        if code:
            ann["code"] = True
        if ann:
            obj["annotations"] = ann
        out.append(obj)
    return out


_INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\(https?://[^)\s]+\))")


def inline(text: str) -> list:
    out = []
    for tok in _INLINE.split(text):
        if not tok:
            continue
        if tok.startswith("**") and tok.endswith("**"):
            out += _rt(tok[2:-2], bold=True)
        elif tok.startswith("`") and tok.endswith("`"):
            out += _rt(tok[1:-1], code=True)
        elif tok.startswith("[") and tok.endswith(")"):
            m = re.match(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", tok)
            out += _rt(m[1], link=m[2]) if m else _rt(tok)
        else:
            out += _rt(tok)
    return out[:100]


def _block(kind: str, rich: list, **extra) -> dict:
    return {"type": kind, kind: {"rich_text": rich, **extra}}


def _table(rows: list[str]) -> dict:
    cells = []
    for i, r in enumerate(rows):
        if i == 1 and re.match(r"^\|?\s*:?-{2,}", r):
            continue  # 머리글 구분선
        parts = [c.strip() for c in r.strip().strip("|").split("|")]
        cells.append(parts)
    width = max(len(c) for c in cells) if cells else 1
    rows_blocks = [{"type": "table_row", "table_row": {"cells": [inline(c) for c in (row + [""] * (width - len(row)))]}}
                   for row in cells]
    return {"type": "table", "table": {"table_width": width, "has_column_header": True, "has_row_header": False,
                                       "children": rows_blocks}}


_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")


def _list_blocks(items: list[tuple[int, str, str]]) -> list:
    """(들여쓰기, 기호, 텍스트) 목록 → 최대 3단계 중첩 블록 (요청당 중첩 2단계 제한)"""
    indents = sorted({ind for ind, _, _ in items})
    level_of = {ind: min(i, 2) for i, ind in enumerate(indents)}
    roots, stack = [], []  # stack: [(level, block)]
    for ind, mark, text in items:
        kind = "numbered_list_item" if mark[0].isdigit() else "bulleted_list_item"
        blk = _block(kind, inline(text))
        lvl = level_of[ind]
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        if stack:
            parent = stack[-1][1]
            parent[parent["type"]].setdefault("children", []).append(blk)
        else:
            roots.append(blk)
        stack.append((lvl, blk))
    return roots


def md_to_blocks(md: str) -> list:
    lines = md.replace("\r", "").split("\n")
    blocks, para, i = [], [], 0

    def flush():
        if para:
            blocks.append(_block("paragraph", inline("\n".join(para))))
            para.clear()

    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if not s:
            flush()
            i += 1
            continue
        if s.startswith("```"):
            flush()
            lang = CODE_LANGS.get(s[3:].strip().lower(), "plain text")
            j, code = i + 1, []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                code.append(lines[j])
                j += 1
            blocks.append({"type": "code", "code": {"rich_text": _rt("\n".join(code)), "language": lang}})
            i = j + 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush()
            blocks.append(_block(f"heading_{min(len(m[1]), 3)}", inline(m[2])))
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", s):
            flush()
            blocks.append({"type": "divider", "divider": {}})
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            flush()
            j, rows = i, []
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(lines[j])
                j += 1
            blocks.append(_table(rows))
            i = j
            continue
        if s.startswith(">"):
            flush()
            j, q = i, []
            while j < len(lines) and lines[j].strip().startswith(">"):
                q.append(lines[j].strip()[1:].strip())
                j += 1
            blocks.append(_block("quote", inline("\n".join(q))))
            i = j
            continue
        if _LIST_RE.match(ln):
            flush()
            j, items = i, []
            while j < len(lines):
                m2 = _LIST_RE.match(lines[j])
                if m2:
                    items.append((len(m2[1].expandtabs(4)), m2[2], m2[3].strip()))
                    j += 1
                elif lines[j].strip() and lines[j][:1] in (" ", "\t") and items:
                    ind, mk, tx = items[-1]
                    items[-1] = (ind, mk, tx + "\n" + lines[j].strip())  # 항목 이어지는 줄
                    j += 1
                else:
                    break
            blocks += _list_blocks(items)
            i = j
            continue
        para.append(s)
        i += 1
    flush()
    return blocks


def text_toggle(title: str, text: str) -> dict:
    """긴 본문(녹취록 등)을 접힌 토글 안의 문단들로"""
    paras, cur = [], ""
    for ln in text.split("\n"):
        if cur and len(cur) + len(ln) + 1 > MAX_TEXT:
            paras.append(cur)
            cur = ""
        cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        paras.append(cur)
    children = [_block("paragraph", _rt(p)) for p in paras[:MAX_CHILDREN]]
    blk = _block("toggle", _rt(title), children=children)
    return blk, paras[MAX_CHILDREN:]


# ───────────────────────── 동기화 ─────────────────────────

def ensure_database(cfg, nstore: dict, client: Notion) -> str:
    """config 의 database_id → 저장된 id → 부모 페이지 아래 같은 제목 검색 → 없으면 생성"""
    ncfg = cfg.get("notion", {})
    if ncfg.get("database_id"):
        return id_from_url(ncfg["database_id"])
    if nstore.get("database_id"):
        return nstore["database_id"]
    if not ncfg.get("parent_page_url"):
        raise RuntimeError("config.json 의 notion.parent_page_url 이 비어 있습니다 (노트를 넣을 노션 페이지 링크).")
    parent = id_from_url(ncfg["parent_page_url"])
    title = ncfg.get("database_title") or DB_TITLE
    db_id = client.find_database(parent, title)
    if db_id:
        log(f"노션: 기존 데이터베이스 '{title}' 사용")
    else:
        db_id = client.create_database(parent, title)
        log(f"노션: 데이터베이스 '{title}' 생성")
    nstore["database_id"] = db_id
    return db_id


def _props(s: dict, sid: str, meta: dict, kinds: list[str], folder: str) -> dict:
    title = f"{s['강의명']} {s['날짜']} — {meta.get('제목') or '강의노트'}"[:200]
    warn = " / ".join(meta.get("경고", []))
    kws = [{"name": k.replace(",", " ")[:100]} for k in meta.get("키워드", [])][:10]
    return {
        "이름": {"title": _rt(title)},
        "과목": {"select": {"name": s["강의명"].replace(",", " ")[:100]}},
        "날짜": {"date": {"start": s["날짜"]}},
        "자료": {"multi_select": [{"name": k} for k in kinds]},
        "키워드": {"multi_select": kws},
        "한줄요약": {"rich_text": _rt(meta.get("한줄요약", "")[:1900])},
        "검토 필요": {"checkbox": bool(warn)},
        "경고": {"rich_text": _rt(warn[:1900])},
        "회차ID": {"rich_text": _rt(sid)},
        "폴더": {"rich_text": _rt(folder[:1900])},
    }


def sync_session(cfg, nstore: dict, client: Notion, db_id: str, s: dict, sid: str, meta: dict,
                 note_md: str, kinds: list[str], folder: str, transcripts: list[tuple[str, str]]) -> str:
    """회차 페이지 생성/갱신 → 페이지 URL"""
    children = md_to_blocks(note_md)
    overflow = []  # (toggle index, 남은 문단들)
    if transcripts:
        children.append({"type": "divider", "divider": {}})
        for title, text in transcripts:
            blk, rest = text_toggle(title, text)
            children.append(blk)
            if rest:
                overflow.append((len(children) - 1, rest))
    props = _props(s, sid, meta, kinds, folder)
    rec = nstore.setdefault("pages", {}).get(sid) or {}
    page = None
    if rec.get("id"):
        try:
            page = client.req("GET", f"/pages/{rec['id']}")
            if page.get("archived"):
                page = None
        except RuntimeError:
            page = None
    if page is None:
        page = client.query_by_session(db_id, sid)
    if page:
        client.update_page(page["id"], props)
        n = client.clear_children(page["id"])
        client.append_children(page["id"], children)
        log(f"  노션 페이지 갱신 (기존 블록 {n}개 교체)")
    else:
        page = client.create_page(db_id, props, children)
        log("  노션 페이지 생성")
    if overflow:  # 토글 100문단 초과분: 페이지 자식 블록 순서 = 우리가 보낸 children 순서 → 위치로 토글을 찾아 이어 붙임
        kids = client.list_children(page["id"])
        for idx, rest in overflow:
            if idx < len(kids) and kids[idx].get("type") == "toggle":
                client.append_children(kids[idx]["id"], [_block("paragraph", _rt(p)) for p in rest])
            else:
                log(f"  ⚠ 녹취록 토글 위치를 찾지 못해 {len(rest)}개 문단을 생략")
    nstore["pages"][sid] = {"id": page["id"], "url": page.get("url", ""), "업로드해시": meta.get("해시", "")}
    return page.get("url", "")


def check_setup(cfg, nstore: dict) -> dict:
    """토큰·부모 페이지 접근·데이터베이스 준비 상태 확인 (업로드는 하지 않음)"""
    client = Notion(get_token(cfg))
    me = client.req("GET", "/users/me")
    parent = id_from_url(cfg.get("notion", {}).get("parent_page_url") or "")
    page = client.req("GET", f"/pages/{parent}")
    title = "".join(x.get("plain_text", "") for p in page.get("properties", {}).values()
                    if p.get("type") == "title" for x in p.get("title", []))
    db_id = ensure_database(cfg, nstore, client)
    return {"통합": me.get("name"), "부모 페이지": title or parent, "데이터베이스": db_id}
