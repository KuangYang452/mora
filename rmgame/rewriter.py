# -*- coding: utf-8 -*-
"""LLM 重写管线 —— rmgame/rewriter（M1：阶段 1 概念发现）

职责（设计文档 §4.3）：
- 阶段 1（本文件）：概念发现 —— 读 raw 索引摘要（每地图：地图名/事件名/
  条目 head），分批喂给 LLM，输出概念清单 [{id, title, kind, summary, refs}]
- 阶段 2（M2）：逐概念重写 —— 读概念 refs 的 raw 条目全文，LLM 写概念条目
- 纯重写、不含原文；条目内容按需懒构建

LLM 调用可注入（llm_responder）以支持离线自测；默认真实调用 llm.ChatClient。
"""

import json
import re
import datetime as _dt
from pathlib import Path

# M4 依赖收敛（见 docs/REFACTOR_DESIGN.md §7）：从函数内 lazy 提升为顶层——
# rmgame 内部依赖方向唯一化（只向下，无环）。wiki↔rewriter 环已消除：
# REJECT_NO_RELEVANT_REFS 归位 wiki（词条域），本模块单向引用。
# llm 为顶层模块（llm 不反向依赖 rmgame，无环）。
from .discovery import RUNTIME_DIR, WIKI_DIR
from .wiki import (REJECT_NO_RELEVANT_REFS, load_index, resolve_raw_ref,
                   save_index, write_index_md)
from .summarizer import SUMMARY_DIR, load_summary
from .llmfmt import is_noise_speaker, search_raw_entries
from .matcher import _event_context
from llm import call_llm

# 每批地图数（摘要体量控制，见文档 §4.3 阶段 1）
BATCH_MAPS = 30
# 每批最多输出概念数（提示词约束）
MAX_CONCEPTS_PER_BATCH = 20
# 每游戏概念总上限（文档 §4.3 限数）
MAX_CONCEPTS = 50
# 单概念重写失败重试次数（文档 §4.3 阶段 2：失败重试 N 次）
REWRITE_RETRIES = 2
# 概念重写素材补检索：按概念名每个实体词最多检索条数（refs 漏选兜底）
EXTRA_REF_LIMIT = 16

VALID_KINDS = {"character", "relationship", "theme", "place", "lore"}

# 提示词里要求 LLM 在全部片段无关时输出的单行标记（concept_rewrite 检测）。
_REJECT_TOKEN = "<<NO_RELEVANT_REFS>>"


# ---------------------------------------------------------------------------
# 摘要构建（raw → 索引摘要）
# ---------------------------------------------------------------------------

def build_maps_summary(game_raw_dir) -> list:
    """从 raw/<slug>/maps/*.json 构建概念发现的输入摘要。

    每地图：{map_file, map_name, events: {事件名: 条目数}, heads: [{id, head}]}
    heads 每地图最多 8 条，每条 head 截取 30 字符 —— 体量可控。
    """
    maps_dir = Path(game_raw_dir) / "maps"
    if not maps_dir.is_dir():
        return []

    def map_no(p: Path) -> int:
        m = re.search(r"(\d+)", p.stem)
        return int(m.group(1)) if m else 0

    out = []
    for f in sorted(maps_dir.glob("Map*.json"), key=map_no):
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue
        events: dict = {}
        heads = []
        seen_events = set()
        for e in data.get("entries", []):
            ev = e.get("event_name") or "?"
            events[ev] = events.get(ev, 0) + 1
            if ev in seen_events:
                continue  # 每事件最多取样 1 条 head（避免重复文本淹没摘要）
            seen_events.add(ev)
            if len(heads) >= 16:
                break
            heads.append({
                "id": e.get("id", ""),
                "head": (e.get("text", "") or "").replace("\n", "/")[:40],
            })
        out.append({
            "map_file": f.name,
            "map_name": data.get("map_name", f.stem),
            "events": events,
            "heads": heads,
        })

    # 战斗事件（Troops.json）：战斗中的对话/台词 → 概念发现可见
    f = Path(game_raw_dir) / "Troops.json"
    if f.exists():
        try:
            tdata = json.loads(f.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            tdata = {}
        events = {}
        heads = []
        for e in tdata.get("troops", []):
            ev = e.get("event_name") or "战斗"
            events[ev] = events.get(ev, 0) + 1
            if len(heads) < 8:
                heads.append({
                    "id": e.get("id", ""),
                    "head": (e.get("text", "") or "").replace("\n", "/")[:40],
                })
        out.append({"map_file": "Troops.json", "map_name": "战斗事件",
                    "events": events, "heads": heads})

    # 公共事件（CommonEvents.json）：主线/通用剧情文本 → 概念发现可见。
    # 与地图事件同为对话范畴；条目数多，事件计数按 event_id 分组，
    # head 分散采样覆盖不同公共事件（避免只见开头），最多 40 条。
    f = Path(game_raw_dir) / "CommonEvents.json"
    if f.exists():
        try:
            cdata = json.loads(f.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            cdata = {}
        ce = [e for e in cdata.get("common_events", []) if isinstance(e, dict)]
        if ce:
            events = {}
            for e in ce:
                ev = f"公共事件{e.get('event_id', '?')}"
                events[ev] = events.get(ev, 0) + 1
            # 只保留 ≥2 条的碎片事件，且最多 60 个键（防摘要过大）
            events = {k: v for k, v in events.items() if v >= 2}
            events = dict(list(events.items())[:60])
            step = max(1, len(ce) // 40)
            heads = [{"id": e.get("id", ""),
                      "head": (e.get("text", "") or "").replace("\n", "/")[:40]}
                     for e in ce[::step][:40]]
            out.append({"map_file": "CommonEvents.json",
                        "map_name": "公共事件（主线/通用剧情）",
                        "events": events, "heads": heads})

    # 数据库（--all 提取）：道具/技能/角色等文本 → 概念发现可见
    f = Path(game_raw_dir) / "database.json"
    if f.exists():
        try:
            drows = json.loads(f.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            drows = []
        events = {}
        heads = []
        for e in drows or []:
            if not isinstance(e, dict):
                continue
            kind = e.get("type", "?")
            events[kind] = events.get(kind, 0) + 1
            if len(heads) < 16:
                name = e.get("name", "")
                text = (e.get("text", "") or "").replace("\n", "/")[:40]
                heads.append({"id": e.get("id", ""),
                              "head": f"{name}：{text}" if name else text})
        out.append({"map_file": "database.json", "map_name": "数据库（道具/技能等）",
                    "events": events, "heads": heads})
    return out


# ---------------------------------------------------------------------------
# 提示词与解析
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """概念 id 用：非字母数字 → 连字符，仅保留 ASCII。"""
    s = re.sub(r"[^0-9a-zA-Z]+", "-", text).strip("-").lower()
    return s or "concept"


def _discovery_prompt(slug: str, summary: list, batch_no: int, total: int) -> str:
    lines = [
        "你是游戏文本分析助手。以下是一个 RPG Maker 游戏「"
        f"{slug}」的对话索引摘要（第 {batch_no}/{total} 批，"
        f"共 {len(summary)} 张地图）。",
        "请从中提炼值得点评参考的「概念」：重要角色、角色关系、地点、",
        "主题意象、世界观设定等。",
        "注意：",
        "- 说话人常是事件编号（如 EV001/EV002），请根据对话内容推断其身份"
        "与角色定位（如村人/冒险者/商人），并据此命名概念",
        "- 无对话的地图（事件：无对话）是正常的，多为过渡/战斗地图，"
        "不要作为主要概念",
        "- 优先提炼有实际对话内容支撑的概念，宁缺毋滥",
        "- 摘要原文可能是日文（原版日文游戏）：概念标题用中文，专名采用"
        "通行音译并与概念重写阶段保持一致（首次出现可附日文原文对照，"
        "如：プリムラ（普莉姆拉））",
        "每个概念输出一个 JSON 对象，整体输出 JSON 数组"
        f"（最多 {MAX_CONCEPTS_PER_BATCH} 个，按重要性降序）：",
        '{"id": "c_<英文标识>", "title": "<概念名>", '
        '"kind": "character|relationship|theme|place|lore", '
        '"summary": "<一句话概括，30字内>", "refs": ["<条目id>"]}',
        "refs 必须来自摘要中的条目 id（如 Map001.1.0），每个概念 1~8 个。",
        "只输出 JSON 数组，不要输出其他任何文字。",
        "",
        "==== 摘要 ====",
    ]
    for m in summary:
        lines.append(f"【{m['map_file']} | {m['map_name']}】")
        ev_s = "、".join(f"{k}({v}条)" for k, v in m["events"].items()) or "（无对话）"
        lines.append(f"事件：{ev_s}")
        for h in m["heads"]:
            lines.append(f"- {h['id']}: {h['head']}")
        lines.append("")
    return "\n".join(lines)


def _parse_concept_json(text: str) -> list:
    """解析 LLM 输出的概念 JSON 数组（多级容错）。

    1) 整体解析（直接 / 代码块 / 首尾括号切片）
    2) 尾逗号容错（LLM 常见输出 `},]` / `,}`）
    3) 逐对象提取兜底（整体非 JSON 但含多个 {...} 对象时）
    """
    text = (text or "").strip()
    candidates = [text]
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        candidates.insert(0, m.group(1).strip())
    for c in candidates:
        start, end = c.find("["), c.rfind("]")
        if 0 <= start < end:
            seg = c[start:end + 1]
            for attempt in (seg, re.sub(r",\s*([}\]])", r"\1", seg)):
                try:
                    return json.loads(attempt)
                except json.JSONDecodeError:
                    continue
    # 逐对象兜底：概念对象无嵌套 dict，{...} 可整体匹配
    # （覆盖 LLM 输出被 max_tokens 截断、整体 JSON 不完整的情况）
    out = []
    for o in re.findall(r"\{[^{}]*\}", text, re.S):
        try:
            out.append(json.loads(o))
        except json.JSONDecodeError:
            continue
    return out


def _normalize_concepts(raw_list: list) -> list:
    """清洗 LLM 输出：字段白名单 + kind 校验 + id/title 兜底。"""
    out = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip() or "未命名概念"
        kind = str(item.get("kind", "")).strip().lower()
        if kind not in VALID_KINDS:
            kind = "theme"
        refs = item.get("refs") or []
        if isinstance(refs, str):
            refs = [r.strip() for r in refs.split(",") if r.strip()]
        refs = [r for r in refs if isinstance(r, str) and r.strip()][:8]
        if not refs:
            continue  # 无引用则丢弃（无法溯源）
        aliases = item.get("aliases") or []
        if isinstance(aliases, str):
            aliases = re.split(r"[、,，;；/]+", aliases)
        aliases = [str(a).strip() for a in aliases if str(a).strip()][:8]
        out.append({
            "id": _slugify(item.get("id") or title) or "concept",
            "title": title,
            "kind": kind,
            "summary": str(item.get("summary", "")).strip()[:80],
            "refs": refs,
            "aliases": aliases,
        })
    return out


def _merge_concepts(batches: list) -> list:
    """多批概念合并去重（按 title），refs 并集；限 MAX_CONCEPTS。"""
    merged = {}
    for c in batches:
        key = c["title"]
        if key in merged:
            seen = set(merged[key]["refs"])
            for r in c["refs"]:
                if r not in seen:
                    merged[key]["refs"].append(r)
                    seen.add(r)
        else:
            merged[key] = dict(c)
    return list(merged.values())[:MAX_CONCEPTS]


# ---------------------------------------------------------------------------
# 阶段 1 入口
# ---------------------------------------------------------------------------

def concept_discovery(slug: str, summary: list,
                      llm_responder=None, llm_cfg: dict = None) -> list:
    """概念发现：分批调用 LLM 提炼概念清单。

    llm_responder：可注入的 callable(prompt) -> str（离线自测用）；
    为 None 时用真实 llm.call_llm（llm_cfg 可临时覆盖 model/temperature）。
    """
    if not summary:
        return []
    batches = [summary[i:i + BATCH_MAPS]
               for i in range(0, len(summary), BATCH_MAPS)]
    all_concepts = []
    total = len(batches)
    for i, batch in enumerate(batches, 1):
        prompt = _discovery_prompt(slug, batch, i, total)
        if llm_responder is not None:
            text = llm_responder(prompt)
        else:
            text = _real_llm(prompt, kind="wiki_discovery",
                             note=f"批 {i}/{total}", llm_cfg=llm_cfg)
        parsed = _parse_concept_json(text)
        if not parsed:
            _log_discovery_failure(slug, i, text)  # 调试：原始响应落盘
        all_concepts.extend(_normalize_concepts(parsed))
    return _merge_concepts(all_concepts)


def _log_discovery_failure(slug: str, batch_no: int, raw_text: str) -> None:
    """批解析为空时把 LLM 原始响应写入 runtime/discovery_debug/（排查用）。"""
    try:
        d = RUNTIME_DIR / "discovery_debug"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{slug}_batch{batch_no}.txt"
        f.write_text(raw_text or "（空响应）", encoding="utf-8")
    except OSError:
        pass


def _real_llm(prompt: str, kind: str = "wiki_discovery", note: str = "",
              llm_cfg: dict = None) -> str:
    """真实 LLM 调用：统一走 llm.call_llm（配置默认唯一来源 + 自动日志）。

    重写/发现任务需要长输出：固定关闭思维链（reasoning=False，避免思维链
    占满 token 预算把 content 挤没），并把 max_tokens 抬到 8192 防止响应截断；
    llm_cfg 仅作显式临时覆盖（model/temperature），不设兜底默认值。
    仅作用于 wiki 生成类调用，不影响角色回合（call_llm 默认仍走配置）。
    """
    cfg = llm_cfg or {}
    resp = call_llm([{"role": "user", "content": prompt}],
                    kind=kind, max_tokens=8192, note=note,
                    model=cfg.get("model"), temperature=cfg.get("temperature"),
                    reasoning=False)
    return resp["choices"][0]["message"].get("content") or ""


# ---------------------------------------------------------------------------
# 阶段 2：逐概念重写（懒构建）
# ---------------------------------------------------------------------------

def _collect_ref_entries(slug: str, refs: list) -> list:
    """收集概念 refs 指向的 raw 条目全文（供 LLM 重写）。

    ref 形如 `Map001.1.0`（mapId.eventId.commandIndex）→ 由 ref 推导
    map_file，再经 raw:// 引用解析读取条目原文。
    """
    out = []
    for ref in refs:
        map_file = ref.split(".")[0] + ".json"
        r = resolve_raw_ref(f"raw://{slug}/{map_file}#{ref}")
        if not r["ok"]:
            continue
        e = r["data"]
        ev_key = ".".join(ref.split(".")[:2]) if "." in ref else ref
        summary = None
        try:
            summary = load_summary(slug, ev_key) or None
            # key 兼容：摘要文件可能带页面后缀（Map005.13.p0），refs 无 page
            if not summary and e.get("page") is not None:
                summary = (load_summary(slug, f"{ev_key}.p{e['page']}")
                           or None)
        except Exception:
            summary = None
        out.append({
            "id": ref,
            "map_file": map_file,
            "ref_url": f"raw://{slug}/{map_file}#{ref}",
            "speaker": e.get("speaker") or e.get("event_name") or "",
            "text": e.get("text", ""),
            "choices": e.get("choices", []),
            "branch_texts": e.get("branch_texts", []),
            "summary": summary,
        })
    return out


def _rewrite_prompt(slug: str, concept: dict, entries: list,
                    old_wiki: str = None) -> str:
    lines = [
        "你是游戏文本整理助手。请把下面这组游戏对话片段重写为一个「概念条目」。",
        f"概念：{concept.get('title', '')}（{concept.get('kind', 'theme')}）",
        f"概括：{concept.get('summary', '')}",
        "",
        "要求：",
        "- 纯重写：用自己的话组织，不照抄原文台词",
        "- 条目内**不包含原文**：正文不出现对话原文",
        "- 以概念为中心组织：## 概述 / ## 相关角色 / ## 相关地点 / ## 剧情线索",
        "- 引用：相关叙述后附 raw:// 引用，**直接原样复制下方条目标注的"
        "「引用地址」**，不要改写格式",
        "- 输出 Markdown，以 `# 概念：<标题>` 开头",
        "- **相关性自检（重要）**：下方「相关原文片段」由关键词子串检索捞到，",
        "  与概念名可能只是字面/子串巧合（例如概念「出口」会命中『喷出口』"
        "『说不出口』这类含“出口”字样、但与『迷宫出口/逃脱通道』无关的句子）。",
        "  请先逐段判断这些片段是否**语义上**真正属于该概念（概念名及其「概括」",
        "  所指的事物/主题）；若**没有**任何片段语义上属于该概念（全部只是字面/",
        "  子串巧合），只输出一行 " + _REJECT_TOKEN + " 并停止，不要生成条目；",
        "  只要存在至少一段语义相关片段，就正常生成条目（无关片段在正文中忽略、"
        "  不引用）。",
        "- 标题行之后加一行元信息 `> 别名：`，列出该概念在游戏中的其他叫法/"
        "异译（如『猎妻迷宫之诅咒』之于『克己之诅咒』），顿号分隔；",
        "没有别名则写 `> 别名：无`",
        "- **有限推断**：若多个片段/事件摘要共同指向某一结论（如某物品的"
        "功能、某机制的位置逻辑），可基于素材综合推断写入；**推断性结论须"
        "在该句标注（推断）**，与原文直接陈述区分；素材完全无支撑的猜测"
        "不得写入。",
        "- 原文可能是日文（原版日文游戏）：先准确理解原文再重写；转述为中文"
        "时专名（人名/地名/术语）采用通行音译，首次出现附日文原文对照"
        "（如：プリムラ（普莉姆拉）），同一角色全程保持同一译名；",
        "",
    ]
    if old_wiki:
        lines += [
            "==== 既有 wiki 内容（参考，可能过期或不准确）====",
            "以下为该概念的既有条目内容，仅供对比与参考——它可能已过期"
            "或不准确，请以下方「相关原文片段」的 RAW 内容为准重新组织：",
            old_wiki[:3000],
            "",
        ]
    related = concept.get("related") or []
    if related:
        lines += [
            "==== 相关概念 ====",
            "以下概念与本概念语义相关但**不是同一事物**（refs 有重叠），",
            "正文可适当提及关联（如「参见/相关」），但不要把它们与本概念"
            "混为一谈：",
            "、".join(str(r) for r in related[:8]),
            "",
        ]
    lines += [
        "==== 相关原文片段 ====",
    ]
    for e in entries:
        spk = (e.get("speaker") or "").strip()
        if is_noise_speaker(spk):
            spk = ""   # 占位事件名（EV001 等）不显示（纯噪音）
        head = f"{e['id']} | 引用地址 {e['ref_url']}"
        if spk:
            head += f" | {spk}"
        lines.append(f"【{head}】")
        if e.get("summary"):
            lines.append(f"事件摘要（参考，以原文为准）：{e['summary'][:400]}")
        lines.append(f"文本：{e['text']}")
        if e["choices"]:
            lines.append(f"选项：{' / '.join(e['choices'])}")
        for b in e["branch_texts"]:
            lines.append(f"分支[{b['branch']}]：{b['text']}")
        lines.append("")
    return "\n".join(lines)


_TERM_SPLIT_RE = re.compile(r"[的之与和及在、，。·\s]")


def _split_terms(text: str) -> list:
    """把概念名/别名按常见助词/连接词切成实体词（「佐拉的败北事件」→
    「佐拉」「败北事件」）。"""
    cands = [p.strip() for p in _TERM_SPLIT_RE.split(text or "")
             if len(p.strip()) >= 2]
    return list(dict.fromkeys(cands))


_SUMMARY_EV_RE = re.compile(r"^(Map\d+|CommonEvents|Troops)\.(\d+)(?:\.p(\d+))?$")


def _summary_refs(slug: str, terms: list, limit: int = 8) -> list:
    """事件摘要关键词检索：摘要含任一检索词 → 返回该事件（页）第一条目 id。

    摘要覆盖事件全貌、措辞比原文更概括（如摘要含「解咒药」而原文是
    「解咒的药」），能桥接 raw 字面检索的形态漏检——关键事件（如通关
    事件）即使原文无连续检索词，只要摘要含概念实体即可被捞到。refs
    指向条目后，_collect_ref_entries 会自动带出该事件摘要供 LLM 参考。
    """
    d = SUMMARY_DIR / slug
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if not any(t and t in text for t in terms):
            continue
        m = _SUMMARY_EV_RE.match(f.stem)
        if not m:
            continue
        map_file = f"{m.group(1)}.json"
        ev_id = int(m.group(2))
        page = int(m.group(3)) if m.group(3) else None
        ctx = _event_context(slug, map_file, ev_id, page=page)
        first = next((e for e in ctx if e.get("id")), None)
        if first and first["id"] not in out:
            out.append(first["id"])
        if len(out) >= limit:
            break
    return out


def _concept_refs(slug: str, title: str, aliases=None, limit: int = None) -> list:
    """概念相关 refs 候选：标题+别名实体词 → raw 字面检索 ∪ 事件摘要桥接。

    检索键 = 标题与别名的拆分实体词（「解咒药」→「解咒」「药」，可命中
    「解咒的药」这类形态变体）；raw 检索捞原文出现实体的条目，摘要桥接
    捞摘要含实体的关键事件。两者并集去重，供词条生成/现场收录兜底。
    """
    limit = limit or EXTRA_REF_LIMIT
    terms = _split_terms(title or "")
    for a in aliases or []:
        terms += _split_terms(a)
    terms = list(dict.fromkeys([t for t in terms if t]))[:8]
    out = []
    for t in terms:
        out += _title_refs(slug, t)
    out += _summary_refs(slug, terms, limit=limit)
    return list(dict.fromkeys(out))


def _title_refs(slug: str, title: str) -> list:
    """按概念名全文检索补充 refs（refs 漏选/选错时兜底）。

    title 按常见助词/连接词切出实体词（如「佐拉的败北事件」→「佐拉」
    「败北事件」），逐个在 raw 里全文子串检索，命中条目 id 并集返回——
    原文里只要出现过概念实体即可被捞到，不依赖发现阶段 refs 选得准。
    检索前过滤无信息量文本（回想房间的"要播放…吗？"确认框、过短碎片），
    避免纯菜单标题占满名额挤掉后地图的关键对话。
    """
    import re as _re

    def _useful(text: str) -> bool:
        t = (text or "").strip()
        if len(t) < 6:
            return False
        if "要播放" in t or "要连环播放" in t:
            return False      # 回想房间播放确认框（无设定信息）
        return True

    cands = [p.strip() for p in _re.split(r"[的之与和及在、，。·\s]", title or "")
             if len(p.strip()) >= 2]
    cands = list(dict.fromkeys(cands))
    out = []
    for c in cands:
        try:
            hits = search_raw_entries(slug, c, limit=EXTRA_REF_LIMIT * 2)
        except Exception:
            continue
        n = 0
        for h in hits:
            if not _useful(h.get("text", "")):
                continue
            rid = h.get("id")
            if rid and rid not in out:
                out.append(rid)
            n += 1
            if n >= EXTRA_REF_LIMIT:
                break
    return out


def concept_rewrite(slug: str, concept: dict,
                    llm_responder=None, llm_cfg: dict = None):
    """阶段 2：把概念重写为条目 Markdown（纯重写 + raw:// 引用）。

    输入：概念 dict（含 id/title/kind/summary/refs）；
    输出：条目 md 文本；收集不到原文或 LLM 重试后仍失败返回 None。
    素材 = 概念 refs 指向的条目全文 + 按概念名（实体词）全文检索命中的
    条目（去重并集）—— 发现阶段 refs 漏选/选错时兜底，保证原文中出现的
    概念实体不会因 refs 未引用而丢失。
    llm_responder：可注入的 callable(prompt) -> str（离线自测用）；
    llm_cfg 仅作显式临时覆盖（model/temperature），统一走 llm.call_llm。
    """
    refs = list(concept.get("refs", []))
    try:
        extra = _concept_refs(slug, concept.get("title", ""),
                              concept.get("aliases"))
        refs = list(dict.fromkeys(refs + extra))[:40]   # 去重保序，控制素材量
    except Exception:
        pass
    entries = _collect_ref_entries(slug, refs)
    if not entries:
        return None
    # 附既有 wiki 内容（若该概念已有 built 条目）—— 供对比参考，以 RAW 为准
    old_wiki = None
    try:
        idx = load_index(slug)
        if idx:
            c0 = next((x for x in idx.get("concepts", [])
                       if x.get("id") == concept.get("id")), None)
            if c0 and c0.get("status") == "built":
                f = WIKI_DIR / slug / c0.get("file", "")
                if f.exists():
                    old_wiki = f.read_text(encoding="utf-8")
    except Exception:
        old_wiki = None
    prompt = _rewrite_prompt(slug, concept, entries, old_wiki=old_wiki)
    for _ in range(REWRITE_RETRIES):
        try:
            if llm_responder is not None:
                text = llm_responder(prompt)
            else:
                text = _real_llm(prompt, kind="wiki_rewrite",
                                 note=f"{concept.get('title', '')}",
                                 llm_cfg=llm_cfg)
        except Exception:
            text = None
        if text and text.strip():
            if _REJECT_TOKEN in text:
                return REJECT_NO_RELEVANT_REFS  # 全部片段与概念语义无关：拒收
            return text.strip()
    return None


# ---------------------------------------------------------------------------
# 骨架落盘（概念发现结果 → wiki/index.json + index.md）
# ---------------------------------------------------------------------------

def write_skeleton(slug: str, name: str, engine: str, concepts: list) -> dict:
    """把概念清单写入 wiki/<slug>/ 骨架；返回 index dict。

    以概念名为键去重（保留首个，refs 并集）—— 防御重复词条（同一概念
    被 LLM 以不同 id 输出、或跨批次/多次发现重复）。

    保留已构建条目：旧索引中 status=built 且条目文件仍存在的概念，
    重建时保留 built 状态与既有文件（id/file/aliases 沿用，refs 新旧并集），
    避免每次 scan_game 后全部回到 pending、下次查询全部重新 LLM 重写；
    旧文件丢失则照常回退 pending（下次查询懒构建补齐）。
    """
    # 旧索引：按 title 记录已构建概念（同标题视为同一概念，id 命名变化不影响）
    old_built = {}
    old_index = load_index(slug)
    if old_index:
        for c in old_index.get("concepts", []):
            t = (c.get("title") or "").strip()
            if t and c.get("status") == "built":
                old_built[t] = c
    dedup = {}
    for c in concepts:
        key = (c.get("title") or "").strip()
        if not key:
            continue
        if key in dedup:
            seen = set(dedup[key]["refs"])
            for r in c.get("refs", []):
                if r not in seen:
                    dedup[key]["refs"].append(r)
                    seen.add(r)
        else:
            dedup[key] = dict(c)
    concepts = list(dedup.values())
    new_concepts = []
    for c in concepts:
        item = {
            "id": c["id"],
            "title": c["title"],
            "kind": c["kind"],
            "summary": c["summary"],
            "status": "pending",          # 阶段 2 懒构建后置 built
            "file": f"concepts/{c['id']}.md",  # 文件名用 ASCII 安全 id
            "refs": c["refs"],
            "aliases": c.get("aliases") or [],  # 发现阶段 LLM 提供的同义称谓
        }
        old = old_built.get((c.get("title") or "").strip())
        if old is not None and (WIKI_DIR / slug / old.get("file", "")).exists():
            # 既有条目仍在：保留 built，避免重复重写；id/file 沿用旧值
            # （已有引用/缓存路径不失效），aliases 新旧并集，refs 新旧并集
            item["status"] = "built"
            item["id"] = old["id"]
            item["file"] = old["file"]
            item["aliases"] = list(dict.fromkeys(
                (old.get("aliases") or []) + (c.get("aliases") or [])))[:8]
            seen = set(c["refs"])
            item["refs"] = c["refs"] + [r for r in (old.get("refs") or [])
                                        if r not in seen]
        new_concepts.append(item)
    index = {
        "slug": slug,
        "name": name,
        "engine": engine,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "concepts": new_concepts,
    }
    save_index(index)
    write_index_md(index)
    return index
