# -*- coding: utf-8 -*-
"""wiki 目录/索引/引用管理 —— rmgame/wiki（M1）

职责（设计文档 §4.3）：
- wiki/<slug>/ 目录结构：index.md（总览+概念清单）+ index.json（机器索引）+ concepts/（条目）
- 机器索引读写：概念 → 状态(built/pending/error) + raw 引用
- raw:// 引用解析（需核对原文时返回 raw 对应条目/地图）
- query_concept 定位：按概念 title/kind/地图名匹配（M1 只定位；懒构建在 M2 接）
- 不含 LLM 逻辑（重写在 rewriter.py）
"""

import json
import re
import datetime as _dt
from pathlib import Path

from .discovery import RAW_DIR, WIKI_DIR

# 概念状态
STATUS_PENDING = "pending"
STATUS_BUILT = "built"
STATUS_ERROR = "error"
STATUS_STALE = "stale"


def _slug_dir(slug: str) -> Path:
    return WIKI_DIR / slug


def init_wiki(slug: str) -> Path:
    """创建 wiki/<slug>/ 目录结构；返回游戏 wiki 目录。"""
    d = _slug_dir(slug)
    (d / "concepts").mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 索引读写
# ---------------------------------------------------------------------------

def load_index(slug: str) -> dict or None:
    """读 wiki/<slug>/index.json；不存在或损坏返回 None。"""
    f = _slug_dir(slug) / "index.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None


def save_index(index: dict) -> Path:
    """写 index.json（原子写）。"""
    slug = index["slug"]
    d = init_wiki(slug)
    f = d / "index.json"
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(f)
    return f


def render_index_md(index: dict) -> str:
    """index.md 总览页：游戏信息 + 概念清单骨架（供查询导航）。"""
    lines = [
        f"# {index.get('name', index['slug'])}（{index.get('engine', '?')}）",
        "",
        f"> 概念清单 {len(index.get('concepts', []))} 条 | "
        f"生成于 {index.get('generated_at', '?')}",
        "",
        "## 概念清单",
        "",
    ]
    for c in index.get("concepts", []):
        status = c.get("status", STATUS_PENDING)
        mark = {"built": "✓", "pending": "·", "error": "✗", "stale": "~"}.get(status, "·")
        lines.append(
            f"- {mark} **{c['title']}**（{c.get('kind', '?')}）{c.get('summary', '')}")
    return "\n".join(lines)


def write_index_md(index: dict) -> Path:
    """写 index.md；返回路径。"""
    d = _slug_dir(index["slug"])
    f = d / "index.md"
    f.write_text(render_index_md(index), encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# raw:// 引用解析
# ---------------------------------------------------------------------------

def resolve_raw_ref(ref: str) -> dict:
    """解析 raw://<slug>/<map_file>.json[#<entry_id>] → 原文数据。

    返回 {"ok": True, "slug", "map_file", "entry_id", "data"}：
    - 带 #entry_id：data 为单条目 dict
    - 不带：data 为整地图 dict（{map_id, map_name, entries}）
    失败返回 {"ok": False, "reason"}。
    """
    if not ref.startswith("raw://"):
        return {"ok": False, "reason": f"非 raw 引用: {ref}"}
    rest = ref[len("raw://"):]
    path_part, _, entry_id = rest.partition("#")
    parts = path_part.split("/")
    if len(parts) != 2:
        return {"ok": False, "reason": f"引用格式错误: {ref}"}
    slug, map_file = parts
    if map_file == "CommonEvents.json":
        f = RAW_DIR / slug / "CommonEvents.json"
        entries_key = "common_events"
    elif map_file == "Troops.json":
        f = RAW_DIR / slug / "Troops.json"
        entries_key = "troops"
    else:
        f = RAW_DIR / slug / "maps" / map_file
        entries_key = "entries"
    if not f.exists():
        return {"ok": False, "reason": f"raw 文件不存在: {f}"}
    try:
        data = json.loads(f.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {"ok": False, "reason": f"raw 文件损坏: {f}"}
    if not entry_id:
        return {"ok": True, "slug": slug, "map_file": map_file,
                "entry_id": None, "data": data}
    for e in data.get(entries_key, []):
        if e.get("id") == entry_id:
            return {"ok": True, "slug": slug, "map_file": map_file,
                    "entry_id": entry_id, "data": e}
    return {"ok": False, "reason": f"条目不存在: {entry_id} (in {map_file})"}


# ---------------------------------------------------------------------------
# 概念查询定位 + 懒构建接入（M2）
# ---------------------------------------------------------------------------

def list_concepts(slug: str) -> list:
    """返回游戏的概念列表（title/kind/status/summary）；无 wiki 返回 []。

    供 query_wiki 空参数时展示现有概念（角色可据此选择查询目标）。
    """
    index = load_index(slug)
    if index is None:
        return []
    return [
        {"title": c.get("title", ""), "kind": c.get("kind", ""),
         "status": c.get("status", STATUS_PENDING),
         "summary": c.get("summary", "")}
        for c in index.get("concepts", [])
    ]


_ALIAS_LINE_RE = re.compile(r"^>\s*别名\s*[:：]\s*(.*)$")
_ALIAS_SPLIT_RE = re.compile(r"[、,，;；/]+")


def _parse_aliases(content: str):
    """从条目元信息行 `> 别名：` 解析同义称谓列表。

    返回：list（显式别名）/ []（显式「无」）/ None（未标注 —— 调用方
    保留既有 aliases，避免 force 重建把手工并入的别名冲掉）。
    """
    if not content:
        return None
    for line in (content or "").splitlines()[:6]:
        m = _ALIAS_LINE_RE.match(line.strip())
        if m:
            raw = m.group(1).strip().strip("「」『』")
            if not raw or raw in ("无", "无别名", "-", "none", "なし"):
                return []
            parts = [p.strip() for p in _ALIAS_SPLIT_RE.split(raw) if p.strip()]
            return parts[:8]
    return None


# 查询串拆词分隔符：空白 / 顿号 / 中英文逗号 / 斜杠
# （模型常把多个实体合并进一个 query，如「契约之门 谢拉」）
_QUERY_SPLIT_RE = re.compile(r"[\s、,，/]+")


def _split_query(q: str) -> list:
    """查询串拆词：空白/顿号/逗号/斜杠分隔，去空。"""
    return [t for t in _QUERY_SPLIT_RE.split(q or "") if t]


def _match_score(c: dict, q: str) -> int:
    """查询匹配质量分（越大越优；-1 = 不命中）。

    优先级：title 精确 > alias 精确 > title 子串 > alias 子串 >
    id 精确 > kind 子串 > refs 地图名子串。别名覆盖同义称谓/异译
    （如『猎妻迷宫之诅咒』之于『克己之诅咒』）。

    多词查询（空白/顿号/逗号/斜杠分隔，如「契约之门 谢拉」）按子词
    拆分后逐词评分累加：任一子词命中已收录概念即可命中，避免组合词
    整体落空；全部子词未命中才返回 -1。
    """
    tokens = _split_query(q)
    if len(tokens) == 1:
        return _match_score_token(c, tokens[0])
    total = 0
    for t in tokens:
        s = _match_score_token(c, t)
        if s > 0:
            total += s
    return total if total > 0 else -1


def _match_score_token(c: dict, q: str) -> int:
    """单子词对概念的匹配分（0 = 不命中；分值体系与原单词逻辑一致）。"""
    title = (c.get("title") or "").strip().lower()
    cid = (c.get("id") or "").lower()
    kind = (c.get("kind") or "").lower()
    aliases = [str(a).strip().lower() for a in (c.get("aliases") or [])
               if str(a).strip()]
    if q and q == title:
        return 100
    if q and q in aliases:
        return 90
    if q and title and q in title:
        return 60
    for a in aliases:
        if q and q in a:
            return 50
    if q and q == cid:
        return 40
    if q and q in kind:
        return 20
    for ref in c.get("refs", []):
        map_file = ref.split(".")[0] + ".json"
        if q in map_file.lower():
            return 10
    return 0


def query_concepts(slug: str, query: str, builder=None) -> dict:
    """按 query 定位全部命中概念（多词查询可命中多个），按匹配分降序。

    返回：
    - {"ok": True, "concepts": [{"concept": {...}, "status": str,
       "content": str|None}, ...]} —— 至少一个命中；最高分概念
       pending/error 且提供 builder 时现场懒构建（其余命中只回摘要，
       避免一次查询多次 LLM 重写；可单独 query_wiki 分别构建）。
    - {"ok": False, "reason": "no_wiki"|"no_concept"|"build_failed",
       "concept": ...}
      build_failed 时 concept 为最高分概念（error 状态，可稍后重试）。
    """
    index = load_index(slug)
    if index is None:
        return {"ok": False, "reason": "no_wiki", "slug": slug}
    q = (query or "").strip().lower()
    if not q:
        return {"ok": False, "reason": "no_concept", "slug": slug, "query": query}
    # 全部概念按匹配质量评分，收集命中（多词任一子词命中即入选）
    scored = []
    for c in index.get("concepts", []):
        s = _match_score(c, q)
        if s > 0:
            scored.append((s, c))
    if not scored:
        return {"ok": False, "reason": "no_concept", "slug": slug, "query": query}
    scored.sort(key=lambda x: -x[0])   # 高分在前；平局保持索引顺序（稳定）
    best = scored[0][1]
    # 最高分概念 pending / error 且可构建 → 现场懒构建（一次）
    built = None
    if best.get("status") in (STATUS_PENDING, STATUS_ERROR) and builder is not None:
        res = ensure_concept(slug, best["id"], builder)
        if not res.get("ok"):
            return {"ok": False, "reason": res.get("reason", "build_failed"),
                    "concept": best, "status": res.get("status"), "slug": slug}
        built = {"concept": res.get("concept") or best,
                 "status": res.get("status"), "content": res.get("content")}
    out = []
    for _s, c in scored:
        if c["id"] == best["id"] and built is not None:
            out.append(built)
        else:
            r = _concept_result(c, index)
            out.append({"concept": c, "status": r["status"],
                        "content": r["content"]})
    return {"ok": True, "concepts": out}


def query_concept(slug: str, query: str, builder=None) -> dict:
    """按 query 定位概念条目（单概念：取最高分命中；兼容旧调用）。

    多词查询（如「契约之门 谢拉」）取最高分概念返回；全部命中列表见
    query_concepts（_query_wiki 用它展示所有命中概念）。返回结构与
    query_concepts 单条一致，且保留原语义：pending/error 且提供 builder
    时现场懒构建；构建失败返回 {"ok": False, "reason": "build_failed"}。
    """
    res = query_concepts(slug, query, builder=builder)
    if not res["ok"]:
        return res
    first = res["concepts"][0]
    return {"ok": True, "concept": first["concept"],
            "status": first["status"], "content": first["content"]}


def ensure_concept(slug: str, concept_id: str, builder, force: bool = False) -> dict:
    """确保概念条目已构建（懒构建核心）。

    builder：callable(slug, concept) -> str|None，返回条目 md 文本；
    None 表示构建失败（置 error 状态，下次查询可重试）。
    force=True 时已 built 也强制重建。
    """
    index = load_index(slug)
    if index is None:
        return {"ok": False, "reason": "no_wiki", "slug": slug}
    c = next((x for x in index.get("concepts", [])
              if x.get("id") == concept_id), None)
    if c is None:
        return {"ok": False, "reason": "no_concept", "slug": slug}
    if not force and c.get("status") == STATUS_BUILT:
        return _concept_result(c, index)   # 已构建：直接命中缓存
    content = builder(slug, c)
    from .rewriter import REJECT_NO_RELEVANT_REFS
    if content == REJECT_NO_RELEVANT_REFS:
        # 拒收：refs 原文与概念名仅是字面/子串巧合，语义上无可靠支撑。
        # 不置 error（error 会让下次查询重复重建且可能残留误导条目），
        # 保持原状态并返回专用原因（register_concept 据此回滚/不入索引）。
        return {"ok": False, "reason": "no_relevant_ref",
                "concept": c, "status": c.get("status", STATUS_PENDING)}
    if not content:
        c["status"] = STATUS_ERROR         # 失败：下次查询重试
        save_index(index)
        write_index_md(index)              # 状态标记同步（error）
        return {"ok": False, "reason": "build_failed",
                "concept": c, "status": STATUS_ERROR}
    f = _slug_dir(slug) / c.get("file", f"concepts/{c['id']}.md")
    f.write_text(content, encoding="utf-8")
    c["status"] = STATUS_BUILT
    # 条目元信息行 `> 别名：` → 写回索引（查询别名可命中；未标注保留旧值）
    aliases = _parse_aliases(content)
    if aliases is not None:
        c["aliases"] = aliases
    save_index(index)
    write_index_md(index)                  # 状态标记同步（built）
    return {"ok": True, "concept": c, "status": STATUS_BUILT, "content": content}


def _ref_exists(slug: str, ref: str) -> bool:
    """校验 ref（裸条目 id 如 Map013.6.39，或 raw:// 引用）在 raw 中真实存在。"""
    if ref.startswith("raw://"):
        return bool(resolve_raw_ref(ref).get("ok"))
    parts = ref.split(".")
    if len(parts) < 2:
        return False
    map_file = parts[0] + ".json"
    return bool(resolve_raw_ref(f"raw://{slug}/{map_file}#{ref}").get("ok"))


def merge_refs(slug: str, concept_id: str, refs: list) -> dict:
    """把建议的 refs（如仲裁 suggested_refs）并入概念索引（去重 + 校验）。

    校验：仅收录 raw 中真实存在的条目 id（防 LLM 输出垃圾引用污染索引），
    已存在的 ref 自动跳过；校验失败的 ref 过滤并记录，不影响其余并入。
    返回 {"ok": True, "added": [...], "skipped": [...]}；
    index 缺失 / 概念不存在返回 {"ok": False, "reason": ...}。
    """
    index = load_index(slug)
    if index is None:
        return {"ok": False, "reason": "no_wiki", "slug": slug}
    c = next((x for x in index.get("concepts", [])
              if x.get("id") == concept_id), None)
    if c is None:
        return {"ok": False, "reason": "no_concept", "slug": slug}
    existing = {str(r) for r in c.get("refs", []) if str(r).strip()}
    added, skipped = [], []
    for r in refs or []:
        r = str(r).strip()
        if not r or r in existing:
            continue
        if _ref_exists(slug, r):
            added.append(r)
        else:
            skipped.append(r)
    if not added:
        return {"ok": True, "added": [], "skipped": skipped, "merged": False}
    c["refs"] = (c.get("refs") or []) + added
    save_index(index)
    write_index_md(index)
    return {"ok": True, "added": added, "skipped": skipped, "merged": True}


def register_concept(slug: str, concept: dict, builder, force: bool = False) -> dict:
    """现场收录：把新概念追加进索引并懒构建（查询未收录概念时调用）。

    concept：{title, kind, summary, refs}（id 由本函数按 title 自动生成，
    ASCII 安全且与既有概念不冲突）；builder 同 ensure_concept。
    返回结构同 ensure_concept；title 已存在时改为对其 ensure（防重复收录）；
    refs 空或 index 缺失时返回 {"ok": False, "reason": ...}。
    """
    index = load_index(slug)
    if index is None:
        return {"ok": False, "reason": "no_wiki", "slug": slug}
    title = (concept.get("title") or "").strip()
    if not title:
        return {"ok": False, "reason": "no_concept", "slug": slug, "query": ""}
    # 同名概念已存在 → 直接 ensure（防重复收录）
    c = next((x for x in index.get("concepts", [])
              if (x.get("title") or "").strip() == title), None)
    if c is not None:
        return ensure_concept(slug, c["id"], builder, force=force)
    refs = [str(r) for r in (concept.get("refs") or []) if str(r).strip()]
    if not refs:
        return {"ok": False, "reason": "no_concept", "slug": slug, "query": title}
    cid = _auto_concept_id(index, title)
    new = {
        "id": cid,
        "title": title,
        "kind": (concept.get("kind") or "lore"),
        "summary": (concept.get("summary") or ""),
        "status": STATUS_PENDING,
        "file": f"concepts/{cid}.md",
        "refs": refs[:8],
        "aliases": [str(a).strip() for a in (concept.get("aliases") or [])
                    if str(a).strip()][:8],
        "related": [str(r).strip() for r in (concept.get("related") or [])
                     if str(r).strip()][:8],
    }
    index.setdefault("concepts", []).append(new)
    save_index(index)
    write_index_md(index)
    res = ensure_concept(slug, cid, builder, force=force)
    if res.get("reason") == "no_relevant_ref":
        # 拒收：新概念 refs 与概念名语义无关（字面/子串巧合），
        # 回滚移除刚加入的骨架，避免残留误导条目（下次查询重新走收录流程）。
        idx = load_index(slug)
        if idx is not None:
            idx["concepts"] = [x for x in idx.get("concepts", [])
                               if x.get("id") != cid]
            save_index(idx)
            write_index_md(idx)
        return {"ok": False, "reason": "no_relevant_ref",
                "slug": slug, "query": title}
    return res


def link_alias(slug: str, title: str, refs: list, judge=None) -> dict:
    """现场收录前防分裂：查询词引用的原文与既有概念 refs 重叠时，
    把查询词并入该概念 aliases（不建新概念，避免同义条目分裂）。

    judge：可选 callable(query_title, concept) -> bool，判断查询词与候选
    概念是否**同义**（如异译「猎妻迷宫之诅咒」≈「克己之诅咒」）。refs
    重叠只说明"相关"，不等于"同义"（如物品「解咒药」与状态「逃脱」
    相关但不同义）；judge 提供时仅当判为同义才合并，否则返回
    {"ok": False, "linked": False, "judged": True, "concept": best}
    供调用方注册独立概念并建立关联。judge 缺失/异常时按旧行为合并
    （向后兼容；异常不阻断收录流程）。

    返回：
    - {"ok": True, "concept": c, "linked": True, "aliases": [...]}
      重叠概念取 refs 交集最大者；查询词已在其 aliases 中时不重复写入。
    - {"ok": False}：index 缺失 / title 与既有概念同名（应走正常路径）/
      无 refs 重叠（现场收录应注册新概念）/ judge 判为不同义。
    """
    index = load_index(slug)
    if index is None:
        return {"ok": False}
    t = (title or "").strip()
    refs_set = {str(r) for r in (refs or []) if str(r).strip()}
    if not t or not refs_set:
        return {"ok": False}
    best, best_n = None, 0
    for c in index.get("concepts", []):
        if (c.get("title") or "").strip() == t:
            return {"ok": False}          # 同名概念已存在：交给正常路径
        overlap = refs_set & {str(r) for r in c.get("refs", [])}
        if len(overlap) > best_n:
            best, best_n = c, len(overlap)
    if best is None or best_n == 0:
        return {"ok": False}
    # 语义同义判断：refs 重叠 ≠ 同义（跨层级概念仅相关，应独立并关联）
    if judge is not None:
        try:
            if not judge(t, best):
                return {"ok": False, "linked": False, "judged": True,
                        "concept": best}
        except Exception:
            pass   # 判断失败：按旧行为合并（不阻断流程）
    aliases = [str(a) for a in (best.get("aliases") or []) if str(a).strip()]
    if t not in aliases:
        aliases.append(t)
        best["aliases"] = aliases
        save_index(index)
        write_index_md(index)
    return {"ok": True, "concept": best, "linked": True, "aliases": aliases}


def _auto_concept_id(index: dict, title: str) -> str:
    """现场收录概念 id：c-auto-<ASCII 化 title>；全中文/冲突时追加序号。"""
    s = re.sub(r"[^0-9a-zA-Z]+", "-", title).strip("-").lower()
    base = f"c-auto-{s}" if s else "c-auto"
    existing = {c.get("id") for c in index.get("concepts", [])}
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def _concept_result(c: dict, index: dict) -> dict:
    status = c.get("status", STATUS_PENDING)
    content = None
    if status == STATUS_BUILT:
        d = _slug_dir(index["slug"])
        f = d / c.get("file", "")
        if not f.exists():
            # 兼容旧命名：早期条目文件为 concepts/<标题>.md（后改为 id 命名）
            alt = d / "concepts" / f"{c.get('title', '')}.md"
            if alt.exists():
                f = alt
            else:
                status = STATUS_ERROR  # 文件丢失，视作未构建
        if f.exists():
            content = f.read_text(encoding="utf-8")
    return {"ok": True, "concept": c, "status": status, "content": content}
