# -*- coding: utf-8 -*-
"""事件摘要 —— rmgame/summarizer

把事件完整上下文（raw 原文，可能数千字）用 LLM 压缩为结构化摘要，
缓存到 runtime/event_summary/<slug>/<event_id>.md（懒构建 + 断点续跑）。

- 摘要为纯重写（不含原文），标注"由原文生成，细节以原文为准"，保留条目 id 引用；
- LLM 调用可注入（llm_responder）以支持离线自测；默认真实调用 llm.call_llm；
- 每次 LLM 调用经 llm.call_llm 统一入口并自动记日志（kind='summary'）；
- 降级：缓存缺失/失败 → 返回 None，调用方回退原文截断（不影响主流程）。
"""

import datetime as _dt
import json
from pathlib import Path

# M4 依赖收敛（见 docs/REFACTOR_DESIGN.md §7）：llm 调用提升为顶层
# （llm 为顶层模块，不反向依赖 rmgame，无环）。
from .discovery import RUNTIME_DIR, WIKI_DIR
from llm import call_llm

SUMMARY_DIR = RUNTIME_DIR / "event_summary"


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

def _path(slug: str, event_id: str) -> Path:
    return SUMMARY_DIR / slug / f"{event_id}.md"


def load_summary(slug: str, event_id: str) -> str or None:
    """读事件摘要缓存；不存在/损坏返回 None。"""
    f = _path(slug, event_id)
    if not f.exists():
        return None
    try:
        return f.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _save_summary(slug: str, event_id: str, text: str) -> Path:
    f = _path(slug, event_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(f)
    return f


# ---------------------------------------------------------------------------
# 提示词与调用
# ---------------------------------------------------------------------------

def _load_wiki_knowledge(slug: str, max_items: int = 40) -> str:
    """读取 wiki/<slug>/index.json 的概念摘要，作为角色/设定知识参考。

    每条概念一行「标题（kind）：摘要」，信息密度高、体积小；
    wiki 缺失/损坏/为空返回 ""（安静降级，不影响摘要主流程）。
    知识为 LLM 推断产物，仅供说话人归因参考，与原文冲突时以原文为准。
    """
    f = WIKI_DIR / slug / "index.json"
    if not f.exists():
        return ""
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    concepts = data.get("concepts") or []
    lines = []
    for c in concepts[:max_items]:
        title = (c.get("title") or "").strip()
        kind = (c.get("kind") or "").strip()
        summary = (c.get("summary") or "").strip()
        if title and summary:
            tag = f"（{kind}）" if kind else ""
            lines.append(f"- {title}{tag}：{summary}")
    return "\n".join(lines)


_SPEAKER_RULES = (
    "==== 说话人归因规则（原文通常未标注说话人，请按下述方法推断）====\n"
    "- 台词中「我叫作 X」「我是 X」这类明确自我介绍：说话人本人就是 X（X 是被介绍者，不是听者）；\n"
    "- 括号（…）等符号包裹的段落，在日式游戏文本中常见为内心独白，其说话人通常即该事件叙述跟随的角色"
    "（游戏中被操控或视角跟随的对象，即主角）——"
    "若原文风格不符合此惯例则不要强行套用，以台词实际内容为准；\n"
    "- 问答配对与回合轮换：注意「你是 X 吧？」→「是的」这类问答中，X 是听者（被指认者）的属性，"
    "不是说话者自己的属性；\n"
    "- 设定参考与原文冲突时以原文为准，无关事件不必套用其中角色；\n"
    "- 仍无法确定时用「某人」指代并标注（推断），不要自行编造角色名；\n"
    "- 不得把自我介绍者与对话对象混为同一人。"
)


def _summary_prompt(slug: str, entry_id: str, context_text: str) -> str:
    know = _load_wiki_knowledge(slug)
    parts = [
        "你是游戏文本摘要助手。下面是一个 RPG Maker 游戏事件的完整对话原文"
        f"（游戏「{slug}」，当前条目 {entry_id}）。",
        "请生成结构化摘要（纯重写，不照抄原文台词）：",
        "- 剧情概要：3~5 句，覆盖整个事件的来龙去脉；",
        "- 关键角色动向：谁做了什么、说了什么重要的话；",
        "- 当前进展：事件结束时剧情停在哪里。",
        "要求：",
        "- 不包含原文台词；可引用条目 id（如 Map001.40.13）作为出处；",
        "- 在末尾标注「（摘要由原文生成，细节以原文为准）」；",
        "- 输出 200~400 字。",
        "- 原文可能是日文（原版日文游戏）：先准确理解原文再概括；转述为中文"
        "时专名（人名/地名）采用通行音译，首次出现附日文原文对照"
        "（如：プリムラ（普莉姆拉）），同一角色保持同一译名；",
    ]
    if know:
        parts += [
            "==== 游戏设定参考（wiki 推断知识，仅供参考，与原文台词冲突时以原文为准）====",
            know,
            "==== 设定参考结束 ====",
        ]
    parts += [
        _SPEAKER_RULES,
        "==== 事件原文 ====",
        f"{context_text}",
        "==== 结束 ====",
    ]
    return "\n".join(parts)


def summarize_event(slug: str, entry_id: str, context_text: str,
                    llm_responder=None, force: bool = False) -> str or None:
    """生成/读取事件摘要。

    context_text：事件完整上下文的 LLM 友好文本（monitor 的
    _fmt_event_context 或 llmfmt.build_event_context 产物）。
    返回摘要文本；缓存缺失且生成失败返回 None。
    """
    cached = load_summary(slug, entry_id)
    if cached and not force:
        return cached
    prompt = _summary_prompt(slug, entry_id, context_text or "（无文本）")
    text = None
    try:
        if llm_responder is not None:
            raw = llm_responder(prompt)
        else:
            raw = _real_llm(prompt, entry_id=entry_id)
        text = (raw or "").strip()
    except Exception:
        # 失败日志已由 llm.call_llm 记录（ok=False）
        return None
    if len(text) <= 20:  # 太短视为失败
        return None
    _save_summary(slug, entry_id, text)
    return text


def _real_llm(prompt: str, entry_id: str = "") -> str:
    """真实 LLM 调用：统一走 llm.call_llm（配置唯一来源 + 自动日志）。

    摘要任务需稳定输出长文本：固定关闭思维链（reasoning=False，避免思维链
    占满 token 预算把 content 挤没），并把 max_tokens 抬到 8192 防止截断；
    仅作用于事件摘要生成，不影响角色回合（call_llm 默认仍走配置）。
    """
    resp = call_llm([{"role": "user", "content": prompt}],
                    kind="summary", max_tokens=8192,
                    note=f"{entry_id} | 生成", reasoning=False)
    return resp["choices"][0]["message"].get("content") or ""
