# -*- coding: utf-8 -*-
"""角色工具执行体 —— rmgame/bridge（M5）

给角色的工具通道提供执行体：5 个 rmgame 工具 → 语义化结果文本
（供 pet.py 的 agent 循环作为 tool 消息回传给 LLM）。

工具（与 data.py TOOLS / llm.py _build_rmgame_tools 对应）：
- discover_running  查询当前运行中的游戏（进程枚举 + 引擎识别 + 入库状态）
- start_game        启动游戏（注入 CDP 调试端口；仅 trust=user 可启动）
- read_current_text 读取当前文本快照（runtime/current.json）
- query_wiki        查询概念条目（pending 时现场 LLM 懒构建）
- scan_game         提取文本 + 概念发现（重建 wiki 骨架）

权限约束（设计文档 §7）：
- 只能操作游戏库（runtime/games.json）已注册的游戏；
  自动发现入库的游戏（trust=auto）只能读，不能启动（start_game 需人工
  确认升级 trust=user，CLI scan --approve）
- 只读/写角色目录内 raw/、wiki/、runtime/
- LLM 调用（懒构建/概念发现）复用 setting/llm.ini（唯一配置来源）
"""


def _resolve_game(args: dict):
    """从工具参数解析游戏；返回 (GameInfo, None) 或 (None, 错误文本)。"""
    from .discovery import load_games
    name = str(args.get("game") or "").strip()
    if not name:
        return None, "缺少 game 参数（游戏名称或 slug）。"
    games = load_games()
    # 主名（slug / name）精确优先；aliases 兜底（防多游戏别名冲突时误命中）
    g = next((x for x in games if x.slug == name or x.name == name), None)
    if g is None:
        g = next((x for x in games if name in getattr(x, "aliases", [])), None)
    if g is None:
        return None, (f"游戏库中无「{name}」：先调用 scan_game 工具"
                      "（或 CLI `scan` 发现并确认入库）。")
    return g, None


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def _discover_running(args: dict) -> str:
    """查询当前运行中的 RPG Maker 游戏（进程枚举 + 引擎识别 + 入库状态）。"""
    from .discovery import load_games
    from .monitor import enumerate_running
    try:
        running, _ports = enumerate_running()
    except Exception as exc:
        return f"枚举运行中的游戏失败：{type(exc).__name__}: {exc}"
    if not running:
        # 无运行中游戏：列出库中可启动项，引导角色直接 start_game
        games = load_games()
        names = [f"《{g.name}》" for g in games if g.name]
        tail = ""
        if names:
            tail = (f"游戏库中已有 {len(names)} 个游戏可启动："
                    + "、".join(names)
                    + "。若对方请求启动游戏，直接调用 start_game"
                    "（参数 game 用上述名称或 slug）。")
        return ("当前未检测到运行中的 RPG Maker 游戏"
                "（进程名 Game.exe + 引擎特征判定）。" + tail)
    games = load_games()
    lines = [f"检测到 {len(running)} 个运行中的 RPG Maker 游戏："]
    for info in running:
        # slug 或目录命中即视为已入库（自动发现解析的名称可能与旧库 slug 不同）
        reg = next((g for g in games
                    if g.slug == info.slug or g.dir == info.dir), None)
        if reg is not None:
            trust = getattr(reg, "trust", "user") or "user"
            state = "可启动" if trust == "user" else "已感知（待允许启动）"
            lines.append(f"- 《{info.name}》（{info.engine}）{info.dir}"
                         f" —— 已入库（{state}）")
        else:
            lines.append(f"- 《{info.name}》（{info.engine}）{info.dir}"
                         " —— 未入库候选（自动感知）")
    lines.append("未确认的游戏：点确认气泡「✅ 允许」，"
                 "或在右键菜单「🎮 游戏」里允许它。")
    return "\n".join(lines)

def _start_game(args: dict) -> str:
    """启动游戏（注入 CDP 调试端口）；trust=auto（自动发现）拒绝启动。"""
    from .monitor import build_snapshot, start_game, write_current
    g, err = _resolve_game(args)
    if err:
        return err
    if (getattr(g, "trust", "user") or "user") != "user":
        return (f"《{g.name}》是自动发现的游戏（尚未人工确认），吾辈还不能启动它。"
                "点一下吾辈身边的确认气泡上的「✅ 允许」，"
                "或在右键菜单「🎮 游戏」里允许它。")
    res = start_game(g)
    if not res["ok"]:
        return f"启动失败：{res.get('error', '未知错误')}"
    if res["state"] == "running":
        # 已在运行：尝试立即读一次快照（供环境段与 read_current_text）
        snap_note = _try_snapshot(g, res.get("port"))
        return f"《{g.name}》已在运行（调试端口 {res['port']}）。{snap_note}"
    # 刚启动：尝试立即读一次快照（游戏可能仍在加载，失败可稍后重试）
    snap_note = _try_snapshot(g, res.get("port"))
    return (f"已启动《{g.name}》（pid={res['pid']}，调试端口 {res['port']}）。"
            f"{snap_note}")


def _try_snapshot(g, port) -> str:
    """尝试 build_snapshot 并写入 current.json；返回简短说明文本。"""
    from .monitor import build_snapshot, write_current
    try:
        snap = build_snapshot(g, port=port)
        write_current(snap)
        if snap.get("text"):
            return f"已读取到当前文本：{snap['text'][:30]}…"
        return "（当前画面暂无文字，稍后可再读取。）"
    except Exception:
        return "（游戏加载中，稍后可用 read_current_text 读取文本。）"


def _fmt_snapshot(cur: dict) -> str:
    parts = [f"游戏：{cur.get('game')}"]
    if cur.get("map_name") or cur.get("map_id") is not None:
        parts.append(f"地图：{cur.get('map_name') or cur.get('map_id')}")
    if cur.get("scene"):
        parts.append(f"场景：{cur.get('scene')}")
    # 优先展示 raw 匹配到的精确原文（OCR 文本有噪声）
    text = cur.get("matched_text") or cur.get("text") or "（无）"
    parts.append(f"当前文本：{text}")
    # 当前事件摘要：全文概述，含当前位置之前与之后的内容（可能剧透）；
    # 仅经本工具提供（不进游戏环境段），细节以摘要末尾"以原文为准"声明为准
    es = (cur.get("event_summary") or "").strip()
    if es:
        parts.append("当前事件摘要（全文概述，含当前位置之前与之后的内容，可能剧透）：")
        parts.append(es)
    # 事件完整上下文（同 event_id 对话流，原文供核对）
    ec = (cur.get("event_context") or "").strip()
    if ec:
        parts.append(f"事件上下文（原文，供核对）：\n{ec[:800]}")
    parts.append(f"来源：{cur.get('source')} | 最后更新：{cur.get('updated_at')}")
    return "\n".join(parts)


def _snapshot_age_note(cur: dict) -> str:
    """快照新鲜度检查：过期（> rmgame_env_fresh_seconds）注明年龄。"""
    try:
        import settings
        from datetime import datetime
        fresh = float(settings.app_config().get("rmgame_env_fresh_seconds", 300))
        ts = cur.get("read_at") or cur.get("updated_at") or ""
        age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
    except Exception:
        return ""
    if age > fresh:
        return f"\n（注意：快照为 {int(age // 60)} 分钟前的画面，可能已变化）"
    return ""


def _read_current_text(args: dict) -> str:
    """读取当前文本快照；可选 game 参数指定游戏（无快照时主动实时读取）。"""
    from .monitor import build_snapshot, load_current, write_current
    game_name = str(args.get("game") or "").strip() or None
    if game_name:
        g, err = _resolve_game(args)
        if err:
            return err
        cur = load_current()
        if cur and cur.get("game") == g.slug:
            return _fmt_snapshot(cur) + _snapshot_age_note(cur)
        # 无该游戏快照：主动实时读取一次（CDP → OCR 兜底）
        try:
            snap = build_snapshot(g)
            write_current(snap)
            if snap.get("text"):
                return _fmt_snapshot(snap)
            return (f"《{g.name}》正在运行，但当前未读取到文字"
                    "（游戏画面可能无文字或窗口未在前台）。")
        except Exception as exc:
            return (f"无法读取《{g.name}》：{type(exc).__name__}: {exc} "
                    "（确认游戏已启动；CDP/OCR 兜底已尝试）")
    cur = load_current()
    if cur is None:
        return ("尚无文本快照：请先调用 start_game 启动游戏，"
                "或调用 read_current_text 并指定 game 参数。")
    return _fmt_snapshot(cur) + _snapshot_age_note(cur)


def _query_wiki(args: dict) -> str:
    from .rewriter import concept_rewrite
    from .wiki import query_concepts
    g, err = _resolve_game(args)
    if err:
        return err
    q = str(args.get("query") or "").strip()
    if not q:
        # 空参数：返回现有概念列表（角色可据此选择查询目标）
        from .wiki import list_concepts
        lst = list_concepts(g.slug)
        if not lst:
            return f"《{g.name}》尚无 wiki 概念：先调用 scan_game 构建概念骨架。"
        lines = [f"《{g.name}》概念列表（{len(lst)} 个，可用 query_wiki 查询）："]
        for c in lst:
            mark = {"built": "✓", "pending": "·", "error": "✗"}.get(c["status"], "·")
            lines.append(f"- {mark} {c['title']}（{c['kind']}）{c['summary']}")
        return "\n".join(lines)
    builder = lambda s, c: concept_rewrite(s, c)  # 现场 LLM 懒构建
    res = query_concepts(g.slug, q, builder=builder)
    if not res["ok"]:
        if res["reason"] == "no_concept":
            return _auto_register_concept(g, q, builder)  # 现场收录新概念
        return {
            "no_wiki": f"《{g.name}》尚无 wiki：先调用 scan_game 构建概念骨架。",
            "build_failed": f"概念「{q}」构建失败，可稍后重试。",
            "no_relevant_ref": (f"概念「{q}」的既有条目引用与概念语义无关"
                                 "（检索命中的多属字面/子串巧合），已拒收；"
                                 "可换个更具体的词查询，或用 wiki_rebuild 重建。"),
        }.get(res["reason"], res["reason"])
    # 多概念命中（组合查询如「契约之门 谢拉」）：第一条给完整条目，
    # 其余给摘要行（控制回传体积；可单独 query_wiki 分别查看）。
    hits = res["concepts"]
    first = hits[0]
    c0 = first["concept"]
    out = [f"概念：{c0['title']}（{c0['kind']}）"]
    out.append(_fmt_concept_body(c0, first))
    for item in hits[1:]:
        c = item["concept"]
        mark = {"built": "✓", "pending": "·", "error": "✗"}.get(item["status"], "·")
        out.append(f"相关概念：{mark} {c['title']}（{c['kind']}）{c.get('summary', '')}")
        if item["status"] not in ("built",):
            out.append(f"  （状态：{item['status']}，可单独 query_wiki 查询构建）")
    return "\n".join(out)


def _fmt_concept_body(c: dict, res: dict) -> str:
    """概念条目语义化展示（摘要/别名 + 内容截断），查询/收录/别名复用。"""
    out = []
    if c.get("summary"):
        out.append(f"摘要：{c['summary']}")
    if c.get("aliases"):
        out.append(f"别名：{'、'.join(c['aliases'])}")
    if res.get("content"):
        out.append("——条目内容——")
        out.append(res["content"][:1500])  # 控制回传体积
    else:
        out.append("（条目内容暂不可用，稍后重试）")
    return "\n".join(out)


def _judge_synonym(query: str, concept: dict) -> bool:
    """LLM 判断查询词与既有概念是否同义（合并）而非仅相关（应独立）。

    refs 重叠只说明"相关"；跨层级概念（如物品「解咒药」与状态「逃脱」）
    相关但不同义，不应合并成别名。判断失败返回 False（不合并，宁可注册
    独立概念并关联，也不锁死知识粒度）。
    """
    from llm import call_llm
    prompt = (
        "你是游戏设定整理助手。判断查询词与已有概念是否指同一个事物"
        "（同义/异译，如「猎妻迷宫之诅咒」≈「克己之诅咒」），还是两个"
        "不同但相关的概念（如「解咒药」是具体物品、「逃脱」是状态/结果，"
        "二者相关但不同义）。\n"
        f"查询词：{query}\n"
        f"已有概念：{concept.get('title', '')}（{concept.get('kind', '')}）\n"
        f"概括：{concept.get('summary', '')}\n"
        "它们的相关原文有重叠。只回答「同义」或「不同义」。"
    )
    try:
        resp = call_llm([{"role": "user", "content": prompt}],
                        kind="wiki_synonym", max_tokens=64,
                        note=f"{query} vs {concept.get('title', '')}",
                        reasoning=False)
        ans = resp["choices"][0]["message"].get("content") or ""
        return "同义" in ans and "不同义" not in ans
    except Exception:
        return False


def _auto_register_concept(g, q: str, builder) -> str:
    """现场收录：查询未收录概念时，用 raw/事件摘要命中条目建新概念并懒构建。

    命中素材（raw 字面 + 事件摘要桥接）→ 先防分裂（引用的原文与既有概念
    refs 重叠且 LLM 判为同义时并入其 aliases 并复用条目，不建重复概念；
    判为仅相关时注册独立概念并记录关联）；否则注册新概念并懒构建（LLM
    重写条目）；无素材或构建失败时如实回退未收录提示。
    """
    from .wiki import ensure_concept, link_alias, register_concept
    from .rewriter import _concept_refs
    refs = _concept_refs(g.slug, q)
    if not refs:
        return (f"未找到与「{q}」相关的概念（raw 与事件摘要均无可引用素材；"
                "不得自行补充该概念的设定细节——如实告知对方尚未收录；"
                "若需收录可调用 scan_game 补充资料）。")
    # 防分裂：refs 重叠且判为同义 → 并入别名，复用既有条目；
    # 判为仅相关 → 注册独立概念，并把重叠概念记为关联
    lk = link_alias(g.slug, q, refs, judge=_judge_synonym)
    if lk.get("linked"):
        c = lk["concept"]
        res = ensure_concept(g.slug, c["id"], builder)
        if res.get("ok"):
            out = [f"「{q}」是概念「{c['title']}」的别名，已并入并返回其条目："]
            out.append(_fmt_concept_body(c, res))
            return "\n".join(out)
        return (f"「{q}」已并入「{c['title']}」的别名，"
                "但条目构建失败，可稍后重试。")
    related = []
    if lk.get("judged") and lk.get("concept"):
        related = [lk["concept"]["title"]]
    concept = {
        "title": q,
        "kind": "lore",
        "summary": f"按「{q}」自动收录（{len(refs)} 处原文引用）",
        "refs": refs,
        "related": related,
    }
    res = register_concept(g.slug, concept, builder)
    if res.get("ok"):
        c = res["concept"]
        out = [f"已收录并构建概念：{c['title']}（{c['kind']}）"]
        out.append(_fmt_concept_body(c, res))
        return "\n".join(out)
    if res.get("reason") == "build_failed":
        return (f"已收录「{q}」骨架（{len(refs)} 处原文引用），"
                "但条目生成失败，可稍后重试或换关键词。")
    if res.get("reason") == "no_relevant_ref":
        return (f"「{q}」在原文中未找到语义相关的可靠支撑：检索命中的条目"
                "（如『喷出口』『说不出口』）与概念所指仅是字面/子串巧合，"
                "为避免误导未收录。可换个更具体的词查询（如「逃脱/逃离迷宫」"
                "），或先调用 scan_game 补充资料。")
    return f"现场收录「{q}」失败：{res.get('reason', '未知原因')}"


def _scan_game(args: dict) -> str:
    from .extract import extract_game, write_raw
    from .rewriter import build_maps_summary, concept_discovery, write_skeleton
    g, err = _resolve_game(args)
    if err:
        return err
    try:
        result = extract_game(g, all_text=bool(args.get("all")))
    except FileNotFoundError as exc:
        return f"提取失败：{exc}"
    raw_dir = write_raw(result)
    summary = build_maps_summary(raw_dir)
    text = f"《{g.name}》扫描完成：{result.map_count} 地图 / {result.entry_count} 条对话。"
    if not summary:
        return text + "（无地图对话数据。）"
    concepts = concept_discovery(g.slug, summary)  # 真实 LLM 概念发现
    if not concepts:
        return text + "（概念发现无结果。）"
    index = write_skeleton(g.slug, g.name, g.engine, concepts)
    return text + f" 概念骨架 {len(index['concepts'])} 个（查询具体概念时现场生成条目）。"


def _read_raw_text(args: dict) -> str:
    """raw → LLM 友好中间格式；条目 id 查询返回完整事件上下文。"""
    from .llmfmt import build_event_context, build_event_pages, build_friendly
    g, err = _resolve_game(args)
    if err:
        return err
    query = str(args.get("query") or "").strip() or None
    try:
        limit = max(1, min(500, int(args.get("limit") or 200)))
    except (TypeError, ValueError):
        limit = 200
    # 条目 id（Map001.40.13 等三段）→ 该事件同页上下文（优先于普通过滤）
    if query:
        ev = build_event_context(g.slug, query)
        if ev:
            return ev
    # 事件级 id（Map001.40 等两段）→ 整事件按页分组（多页=独立对话阶段）
    if query:
        ev = build_event_pages(g.slug, query)
        if ev:
            return ev
    text = build_friendly(g.slug, query=query, limit=limit)
    if not text.strip():
        return f"《{g.name}》中未找到与「{query}」相关的内容（确认游戏已 scan_game 提取）。"
    return text


def _wiki_rebuild(args: dict) -> str:
    """强制重建概念条目：可选先并入仲裁建议的 refs，再以原文为准重写。

    定位概念（query_concept 不带 builder）→ 可选 merge_refs（校验 raw
    存在性，非法条目过滤）→ ensure_concept(force=True) 强制 LLM 重写
    （旧条目自动作为 old_wiki 参考，以 RAW 为准）。返回新条目内容。
    """
    from .rewriter import concept_rewrite
    from .wiki import ensure_concept, merge_refs, query_concept
    g, err = _resolve_game(args)
    if err:
        return err
    title = str(args.get("concept") or "").strip()
    if not title:
        return "缺少 concept 参数（要重建的概念名或 id）。"
    loc = query_concept(g.slug, title)   # 不带 builder：只定位，不触发懒构建
    if not loc["ok"]:
        reason = {
            "no_wiki": "该游戏尚无 wiki（先调用 scan_game 构建概念骨架）。",
            "no_concept": f"无匹配概念（concept: {title}）。",
        }.get(loc["reason"], loc["reason"])
        return f"重建失败：{reason}"
    cid = loc["concept"]["id"]
    # 可选：并入建议 refs（校验 raw 存在性；字符串或列表都兼容）
    refs = args.get("refs") or []
    if isinstance(refs, str):
        refs = [r.strip() for r in refs.replace("，", ",").split(",") if r.strip()]
    merged_note = ""
    if refs:
        mg = merge_refs(g.slug, cid, refs)
        if not mg.get("ok"):
            return (f"重建失败：补充 refs 时出错"
                    f"（{mg.get('reason', '未知原因')}）。")
        if mg.get("added"):
            merged_note += f"已并入 {len(mg['added'])} 条 refs：{'、'.join(mg['added'])}。"
        if mg.get("skipped"):
            merged_note += (f"过滤非法 refs {len(mg['skipped'])} 条"
                            f"（raw 中不存在）：{'、'.join(mg['skipped'])}。")
    builder = lambda s, c: concept_rewrite(s, c)   # 真实 LLM 重写（old_wiki 参考）
    res = ensure_concept(g.slug, cid, builder, force=True)
    if not res.get("ok"):
        return (f"重建失败：{res.get('reason', '未知原因')}"
                + (f"（{merged_note}）" if merged_note else ""))
    c = res["concept"]
    out = [f"已重建概念：{c['title']}（{c['kind']}）"]
    if merged_note:
        out.append(merged_note)
    out.append(f"摘要：{c.get('summary', '')}")
    out.append(f"引用：{', '.join(c.get('refs', []))}")
    out.append("——条目内容——")
    out.append((res.get("content") or "")[:1500])
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 入口：按工具名分发（供 pet.py agent 循环调用）
# ---------------------------------------------------------------------------

def execute_tool(name: str, args: dict) -> str:
    """执行角色工具，返回语义化结果文本（tool 消息回传内容）。"""
    handlers = {
        "discover_running": _discover_running,
        "start_game": _start_game,
        "read_current_text": _read_current_text,
        "query_wiki": _query_wiki,
        "scan_game": _scan_game,
        "read_raw_text": _read_raw_text,
        "wiki_arbitrate": _wiki_arbitrate,
        "wiki_rebuild": _wiki_rebuild,
    }
    fn = handlers.get(name)
    if fn is None:
        return (f"未知工具：{name}（rmgame 支持 discover_running / start_game / "
                "read_current_text / query_wiki / scan_game / read_raw_text / "
                "wiki_arbitrate / wiki_rebuild）")
    try:
        return fn(args or {})
    except Exception as exc:
        return f"工具执行出错：{type(exc).__name__}: {exc}"


def _wiki_arbitrate(args: dict) -> str:
    """wiki 仲裁：剧情摘要与词条冲突时，以 raw 原文裁决（不改数据）。"""
    from .arbitrate import arbitrate
    g, err = _resolve_game(args)
    if err:
        return err
    title = str(args.get("concept") or "").strip()
    if not title:
        return "缺少 concept 参数（冲突概念名）。"
    event_id = str(args.get("event") or "").strip()
    conflict = str(args.get("conflict") or "").strip()
    return arbitrate(g.slug, title, event_id=event_id, conflict=conflict)
