# -*- coding: utf-8 -*-
"""raw → LLM 友好中间格式 —— rmgame/llmfmt

raw 的结构化 JSON（地图/公共事件条目）对 LLM 不友好：id/事件号/命令码
等程序字段冗余、多行文本难读。本模块做**确定性转换**：把 raw 转成紧凑
可读的文本格式（保留条目 id 供 raw:// 引用），供角色直接阅读、wiki
构建复用。转换不经过 LLM（无额外成本）。

格式：
【游戏：<slug>】
==== 地图：<地图名>（<文件>）====
- [<条目id>] <说话人>：「<文本>」
==== 公共事件 ====
- [<条目id>]：「<文本>」
"""

import json
import re
from pathlib import Path

from .discovery import RAW_DIR

DEFAULT_LIMIT = 200  # 单次返回条目数上限（防超长）

# 无意义占位说话人：RPG Maker 自动命名的事件名（EV001 等）
_NOISE_SPEAKER_RE = re.compile(r"^EV\d+$")

# 事件级 id（两段：地图/公共/战斗 + 事件号，如 Map005.16 / CommonEvents.39）
_EVENT_ID_RE = re.compile(r"^(Map\d+|CommonEvents|Troops)\.(\d+)$")

# 查询串拆词分隔符：空白 / 顿号 / 中英文逗号 / 斜杠
# （模型常把多个实体合并进一个 query，如「契约之门 谢拉」）
_QUERY_SPLIT_RE = re.compile(r"[\s、,，/]+")


def _split_query(q: str) -> list:
    """查询串拆词：空白/顿号/逗号/斜杠分隔，去空。"""
    return [t for t in _QUERY_SPLIT_RE.split(q or "") if t]


def is_noise_speaker(name: str) -> bool:
    """说话人是否为无意义占位（事件名 EV001 等，不是角色名）。"""
    return bool(name and _NOISE_SPEAKER_RE.match(name.strip()))


def _fmt_entry(e: dict) -> str:
    speaker = (e.get("speaker") or e.get("event_name") or "").strip()
    if is_noise_speaker(speaker):
        speaker = ""   # 占位事件名不显示（纯噪音）
    text = re.sub(r"\s+", " ", (e.get("text") or "")).strip()
    line = f"- [{e.get('id', '')}]"
    if speaker:
        line += f" {speaker}："
    line += f"「{text}」"
    return line


def _fmt_db_entry(e: dict) -> str:
    """数据库条目（道具/技能等）格式化：- [id] 名称：「文本」。"""
    name = (e.get("name") or "").strip()
    text = re.sub(r"\s+", " ", (e.get("text") or "")).strip()
    line = f"- [{e.get('id', '')}]"
    if name:
        line += f" {name}："
    line += f"「{text}」"
    return line


def _matches(query: str, map_stem: str, map_name: str, entry: dict) -> bool:
    """过滤判定：地图名/条目 id/条目文本关键词。

    事件级 id（Map005.16 等）保持整串精确前缀匹配；其余查询按空白/
    顿号/逗号/斜杠拆词（如「契约之门 谢拉」），任一子词命中即通过——
    组合查询只要包含已有内容即可命中，不再整体落空。
    """
    if not query:
        return True
    q = query.strip()
    if _EVENT_ID_RE.match(q):
        # 事件级 id（Map005.16 等）：精确前缀匹配，避免子串误命中其他事件
        # （如 Map005.1 命中 Map005.13/16、Map005.16 命中 Map005.160）
        eid = entry.get("id") or ""
        return bool(eid) and (eid == q or eid.startswith(q + "."))
    eid = entry.get("id") or ""
    speaker = entry.get("speaker") or ""
    text = entry.get("text") or ""
    for t in _split_query(q):
        if t in (map_stem or "") or t in (map_name or ""):
            return True
        if t in eid or t in speaker or t in text:
            return True
    return False


def build_friendly(slug: str, query: str = None,
                   limit: int = DEFAULT_LIMIT) -> str:
    """raw/<slug> → LLM 友好中间格式文本。

    query：None=全部（限 limit）；'Map001'/地图名=单地图；'CommonEvents'
    =公共事件；其他=关键词过滤（地图名/条目 id/条目文本）。
    limit：条目数上限（默认 200），超出截断并标注。
    """
    base = RAW_DIR / slug
    lines = [f"【游戏：{slug}】"]
    count = 0
    truncated = False

    def add(entry) -> bool:
        nonlocal count, truncated
        if limit and count >= limit:
            truncated = True
            return False
        lines.append(_fmt_entry(entry))
        count += 1
        return True

    maps_dir = base / "maps"
    if maps_dir.is_dir():
        for f in sorted(maps_dir.glob("Map[0-9]*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError):
                continue
            map_name = data.get("map_name", f.stem)
            # 地图级过滤：仅当 query 是明确地图引用（Map<数字>）且不匹配时才跳过；
            # 其他关键词（角色名/文本）不跳过，交给条目级过滤
            map_hit = bool(query) and (query in (f.stem or "") or query in (map_name or ""))
            if query and not map_hit and re.match(r"^Map\d+$", query):
                continue
            # 先收集命中条目，无命中则不打印地图标题（避免空标题浪费）
            hits = [e for e in data.get("entries", [])
                    if not query or _matches(query, f.stem, map_name, e)]
            if not hits:
                continue
            lines.append(f"==== 地图：{map_name}（{f.name}）====")
            for e in hits:
                if not add(e):
                    break
            if truncated:
                break

    f = base / "CommonEvents.json"
    if f.exists() and not truncated:
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            data = {}
        # 公共事件过滤：空/明确引用("CommonEvents"/"公共事件") → 全部；
        # 明确地图引用(Map\d+) → 跳过；其他关键词 → 条目级过滤
        ce_explicit = query in ("CommonEvents", "公共事件")
        if not query or ce_explicit or not re.match(r"^Map\d+$", query or ""):
            lines.append("==== 公共事件 ====")
            for e in data.get("common_events", []):
                if query and not ce_explicit and not _matches(query, "CommonEvents", "公共事件", e):
                    continue
                if not add(e):
                    break

    # 战斗事件（Troops.json）：query 明确引用或关键词命中才输出
    f = base / "Troops.json"
    if f.exists() and not truncated:
        try:
            tdata = json.loads(f.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            tdata = {}
        t_explicit = query in ("Troops", "战斗")
        if not query or t_explicit or not re.match(r"^Map\d+$", query or ""):
            lines.append("==== 战斗事件 ====")
            for e in tdata.get("troops", []):
                if query and not t_explicit and not _matches(query, "Troops", "战斗事件", e):
                    continue
                if not add(e):
                    break

    # 数据库（--all 提取的道具/技能等）：query 明确引用或关键词命中才输出
    db_file = base / "database.json"
    if db_file.exists() and not truncated:
        try:
            db_rows = json.loads(db_file.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            db_rows = []
        db_explicit = bool(query) and query in (
            "database", "道具", "技能", "武器", "防具", "敌人", "职业", "状态", "角色")
        if not query or db_explicit or not re.match(r"^Map\d+$", query or ""):
            lines.append("==== 数据库（道具/技能/角色等）====")
            for e in db_rows or []:
                if not isinstance(e, dict):
                    continue
                if query and not db_explicit:
                    if not (query in (e.get("name") or "")
                            or query in (e.get("text") or "")
                            or query in (e.get("type") or "")):
                        continue
                if limit and count >= limit:
                    truncated = True
                    break
                lines.append(_fmt_db_entry(e))
                count += 1
            if truncated:
                lines.append(f"（已达 {limit} 条上限，可用 query 缩小范围）")

    if count == 0:
        return ""
    if truncated:
        lines.append(f"（已达 {limit} 条上限，可用 query 缩小范围）")
    return "\n".join(lines)


def search_raw_entries(slug: str, query: str, limit: int = 8) -> list:
    """按关键词搜索 raw 条目，返回命中条目元数据（供现场收录 refs 使用）。

    覆盖地图条目 / 公共事件 / 战斗事件（对话类）；匹配规则与
    build_friendly 一致（地图名/条目 id/说话人/文本子串）。
    返回 [{id, text, speaker, map_file, map_name}]，按出现顺序，最多 limit 个；
    query 为空或无可命中条目返回 []。
    """
    q = (query or "").strip()
    if not q:
        return []
    base = RAW_DIR / slug
    out = []

    def add(e: dict, map_file: str, map_name: str) -> bool:
        if _matches(q, map_file, map_name, e):
            out.append({
                "id": e.get("id", ""),
                "text": e.get("text", ""),
                "speaker": e.get("speaker") or e.get("event_name") or "",
                "map_file": map_file,
                "map_name": map_name,
            })
            return True
        return False

    maps_dir = base / "maps"
    if maps_dir.is_dir():
        for f in sorted(maps_dir.glob("Map[0-9]*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError):
                continue
            map_name = data.get("map_name", f.stem)
            for e in data.get("entries", []):
                if add(e, f.stem, map_name) and len(out) >= limit:
                    return out
    for fname, key, label in (("CommonEvents.json", "common_events", "公共事件"),
                              ("Troops.json", "troops", "战斗事件")):
        f = base / fname
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue
        for e in data.get(key, []):
            if add(e, Path(fname).stem, label) and len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------------------
# 事件完整上下文（按条目 id 拉取）
# ---------------------------------------------------------------------------

_ENTRY_ID_RE = re.compile(r"^(Map\d+|CommonEvents|Troops)\.(\d+)\.(\d+)$")


def build_event_context(slug: str, entry_id: str,
                        max_chars: int = 4000) -> str:
    """按条目 id 拉取事件上下文（LLM 友好格式）。

    entry_id 形如 Map001.40.13 / CommonEvents.218.0 / Troops.1.0：
    返回**同事件同页面**（event_id + page）的条目文本（按序，事件开头
    在前），含总段数说明；旧 raw 无 page 字段时返回整个事件（向后兼容）；
    识别失败或无匹配返回 ""。
    """
    m = _ENTRY_ID_RE.match((entry_id or "").strip())
    if not m:
        return ""
    map_file = f"{m.group(1)}.json"
    ev_id = int(m.group(2))
    from .matcher import _event_context
    ctx = _event_context(slug, map_file, ev_id)
    if not ctx:
        return ""
    # 定位该条目所属页面（同事件多页 = 独立对话阶段，避免混入其他页）
    target = next((e for e in ctx if e.get("id") == entry_id), None)
    page = target.get("page") if target else None
    if page is not None:
        same_page = [e for e in ctx if e.get("page") == page]
        if same_page:
            ctx = same_page
    lines = [f"【事件完整上下文：{entry_id}（共 {len(ctx)} 段）】"]
    total = 0
    truncated = False
    for e in ctx:
        spk = (e.get("speaker") or "").strip()
        if is_noise_speaker(spk):
            spk = ""   # 占位事件名不显示（纯噪音）
        seg = (spk + "：" if spk else "") \
            + (e.get("text") or "").replace("\n", " ")
        if total + len(seg) > max_chars:
            truncated = True
            break
        lines.append(f"- [{e['id']}] {seg}")
        total += len(seg)
    if truncated:
        lines.append(f"…（已达 {max_chars} 字上限，事件共 {len(ctx)} 段）")
    return "\n".join(lines)


def build_event_pages(slug: str, entry_id: str,
                      max_chars: int = 4000) -> str:
    """按事件级 id（Map001.40 / CommonEvents.218 / Troops.1，两段）拉取
    事件全部页面，按页分组输出（页间标注，避免跨页重复条目 id 混淆）。

    与 build_event_context（三段 id，返回 target 所在页）互补：多页事件
    的页内指令序号各自从 0 起算，条目 id 跨页重复，直接平铺会把多个
    对话阶段混成一段。本函数按 page 分组、页首标注段数，让调用方
    （LLM）能区分独立对话阶段；旧 raw 无 page 字段时退化为整事件顺序
    输出（向后兼容）；识别失败或无匹配返回 ""。
    """
    m = _EVENT_ID_RE.match((entry_id or "").strip())
    if not m:
        return ""
    map_file = f"{m.group(1)}.json"
    ev_id = int(m.group(2))
    from .matcher import _event_context
    ctx = _event_context(slug, map_file, ev_id)
    if not ctx:
        return ""
    # 按页分组（保持 raw 顺序）；旧 raw 无 page 字段 → 单组（无标注）
    pages = []
    for e in ctx:
        if not pages or pages[-1][0] != e.get("page"):
            pages.append((e.get("page"), []))
        pages[-1][1].append(e)
    has_pages = any(p is not None for p, _ in pages)
    title = f"【事件完整上下文：{entry_id}（共 {len(ctx)} 段"
    title += f" / {len(pages)} 页，按页分组）】" if has_pages else "）】"
    lines = [title]
    total = 0
    truncated = False
    for pno, group in pages:
        head = (f"==== 页 {pno}（{len(group)} 段）===="
                if pno is not None else f"==== 事件全文（{len(group)} 段）====")
        if total + len(head) > max_chars:
            truncated = True
            break
        lines.append(head)
        total += len(head)
        for e in group:
            spk = (e.get("speaker") or "").strip()
            if is_noise_speaker(spk):
                spk = ""   # 占位事件名不显示（纯噪音）
            seg = (spk + "：" if spk else "") \
                + (e.get("text") or "").replace("\n", " ")
            if total + len(seg) > max_chars:
                truncated = True
                break
            lines.append(f"- [{e['id']}] {seg}")
            total += len(seg)
        if truncated:
            break
    if truncated:
        lines.append("…（已达 " + f"{max_chars} 字上限，事件共 {len(ctx)} 段"
                     + (f" / {len(pages)} 页" if has_pages else "")
                     + "；可用三段式条目 id（如 Map005.16.30）读取单页）")
    return "\n".join(lines)
