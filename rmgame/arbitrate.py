# -*- coding: utf-8 -*-
"""wiki 仲裁 —— rmgame/arbitrate

当剧情摘要（当前事件）与 wiki 概念条目内容冲突时，以 raw 原文为锚点
裁决冲突类型与责任方（不修改任何数据，只输出裁决与建议）：

- wiki_biased      原文支持摘要一侧（或摘要侧证据在原文有明确依据），
                   而 wiki 词条素材（refs）未覆盖关键原文 → 词条采样不全，
                   应补 refs 并重建词条
- summary_biased   原文支持 wiki，当前事件的摘要只反映局部/误导信息
                   → 摘要视角局限，wiki 无需改（可选记录多视角）
- narrative_shift  原文本身在不同事件中对同一实体描述不一致
                   → 叙述诡计/剧情演变，两边都不算错，wiki 应记多视角
- both_incomplete  原文对冲突点没有明确表述 → 双方信息均不足，
                   暂不裁决，扩大检索或等更多剧情后重试

证据收集：词条 refs 条目全文 + 按概念名补检索命中条目（复用
rewriter._title_refs / _collect_ref_entries）+ 事件上下文与事件摘要。
LLM 调用走 llm.call_llm（kind="wiki_arbitrate"，配置唯一来源）。
"""

import json
import re as _re

from .rewriter import _collect_ref_entries, _title_refs

# 裁决分类（LLM 输出枚举）
VERDICTS = ("wiki_biased", "summary_biased", "narrative_shift", "both_incomplete")

_VERDICT_ZH = {
    "wiki_biased": "词条素材不全（wiki 采样缺关键原文）",
    "summary_biased": "摘要视角局部（当前事件信息不全面）",
    "narrative_shift": "原文多视角（叙述变化/诡计，两边不算矛盾）",
    "both_incomplete": "双方信息均不足",
}


def _real_llm(prompt: str, note: str = "", llm_cfg: dict = None) -> str:
    """真实 LLM 调用：统一走 llm.call_llm（配置默认唯一来源 + 自动日志）。"""
    from llm import call_llm
    cfg = llm_cfg or {}
    resp = call_llm([{"role": "user", "content": prompt}],
                    kind="wiki_arbitrate", max_tokens=4096, note=note,
                    model=cfg.get("model"), temperature=cfg.get("temperature"))
    return resp["choices"][0]["message"].get("content") or ""


def _parse_verdict(text: str) -> dict:
    """从 LLM 响应提取裁决 JSON；失败回退 (unknown, 原文本)。"""
    m = _re.search(r"\{.*\}", text or "", flags=_re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            v = data.get("verdict")
            if v in VERDICTS:
                return data
        except (json.JSONDecodeError, ValueError):
            pass
    return {"verdict": "unknown", "reason": (text or "").strip()[:200]}


def _fmt_entries(entries: list) -> str:
    lines = []
    for e in entries:
        head = f"{e['id']} | 引用地址 {e['ref_url']}"
        if e.get("speaker"):
            head += f" | {e['speaker']}"
        lines.append(f"【{head}】")
        if e.get("summary"):
            lines.append(f"事件摘要（参考）：{e['summary'][:300]}")
        lines.append(f"文本：{e['text']}")
        if e.get("choices"):
            lines.append(f"选项：{' / '.join(e['choices'])}")
        for b in e.get("branch_texts", []):
            lines.append(f"分支[{b['branch']}]：{b['text']}")
        lines.append("")
    return "\n".join(lines)


def _arbitration_prompt(slug: str, concept: dict, entries: list,
                        event_context: str = "", event_summary: str = "",
                        conflict: str = "") -> str:
    lines = [
        "你是游戏设定校订助手。有一处「剧情摘要与 wiki 词条内容冲突」需要你",
        "以游戏原文（RAW）为唯一权威裁决。原文中没有的信息不得臆测；",
        "原文存在明确表述时以此为准。",
        "",
        f"游戏：{slug}",
        f"冲突概念：{concept.get('title', '')}（{concept.get('kind', 'theme')}）",
        f"概念概括：{concept.get('summary', '')}",
        "",
    ]
    if conflict:
        lines += [f"冲突描述：{conflict}", ""]
    lines += [
        "==== wiki 词条（一方）====",
        (concept.get("content") or "（条目内容为空）")[:2000],
        "",
    ]
    if event_summary:
        lines += ["==== 当前事件摘要（另一方）====", event_summary[:1200], ""]
    if event_context:
        lines += ["==== 事件原文（冲突事件完整上下文）====",
                  event_context[:3500], ""]
    lines += [
        "==== raw 证据条目（词条 refs + 按概念名检索命中，全部原文）====",
        _fmt_entries(entries),
        "==== 结束 ====",
        "",
        "请裁决：哪个说法与原文一致、哪个是信息不全或误导；注意区分：",
        "- 若原文明确支持摘要侧而词条素材未覆盖该原文 → wiki_biased",
        "- 若原文支持词条而事件摘要本身片面 → summary_biased",
        "- 若原文不同事件里说法不同（叙述诡计/演变）→ narrative_shift",
        "- 若原文对冲突点无明确表述 → both_incomplete",
        "",
        "只输出 JSON（不要 Markdown 代码块、不要其他文字）：",
        '{"verdict": "wiki_biased|summary_biased|narrative_shift|both_incomplete",'
        ' "reason": "一句话归因", "evidence": "原文证据简述（引用条目id）",'
        ' "suggested_refs": ["Map013.6.39"]}',
        "suggested_refs：仅当 verdict=wiki_biased 时给出应补充进词条的关键条目 id；",
        "其他情况给空数组 []。",
    ]
    return "\n".join(lines)


def _fmt_result(slug: str, concept: dict, data: dict) -> str:
    v = data.get("verdict", "unknown")
    zh = _VERDICT_ZH.get(v, "未知/格式异常")
    out = [f"仲裁结果（{slug} / {concept.get('title', '')}）：{zh}"]
    if data.get("reason"):
        out.append(f"归因：{data['reason']}")
    if data.get("evidence"):
        out.append(f"原文证据：{data['evidence']}")
    refs = data.get("suggested_refs") or []
    if refs:
        out.append(f"建议补充 refs（{len(refs)} 条）：{'、'.join(refs)}")
        out.append("建议操作：调用 wiki_rebuild 工具"
                   f"（game={slug}，concept={concept.get('title', '')}，"
                   f"refs={refs}）补充这些 refs 并重建词条（wiki 侧素材不全）。")
    elif v == "wiki_biased":
        out.append("建议操作：调用 wiki_rebuild 工具"
                   f"（game={slug}，concept={concept.get('title', '')}）重建词条"
                   "（LLM 未给出具体条目，可先用 read_raw_text 按概念名检索补充素材）。")
    elif v == "summary_biased":
        out.append("建议操作：wiki 无需修改；该事件可记为多视角参考。")
    elif v == "narrative_shift":
        out.append("建议操作：词条改为多视角结构，记录各事件中的不同描述。")
    elif v == "both_incomplete":
        out.append("建议操作：暂不裁决；扩大原文检索或等更多剧情后重试。")
    return "\n".join(out)


def arbitrate(slug: str, title: str, event_id: str = "",
              conflict: str = "", llm_responder=None,
              llm_cfg: dict = None) -> str:
    """执行一次 wiki 仲裁，返回语义化结果文本（不改任何数据）。

    slug：游戏 slug；title：概念名；event_id：可选，冲突事件的条目 id
    （如 Map005.15.44，通常取角色当前回合 match_id）；conflict：可选，
    冲突描述（自然语言）；llm_responder 供离线自测注入。
    """
    from .wiki import query_concept
    from .llmfmt import build_event_context
    from .summarizer import load_summary

    res = query_concept(slug, title)   # 不带 builder：不触发重写
    if not res["ok"]:
        reason = {"no_wiki": "该游戏尚无 wiki", "no_concept": f"无概念「{title}」"
                  }.get(res["reason"], res["reason"])
        return f"仲裁失败：{reason}（先用 query_wiki/scan_game 建立概念）。"
    concept = res["concept"]
    if res.get("status") != "built":
        return (f"概念「{concept.get('title', title)}」尚未构建（{res['status']}），"
                "无法与摘要对比；可先用 query_wiki 触发构建再仲裁。")
    # query_concept 的 content 在 res 顶层（concept 是索引条目，无 content）
    concept = dict(concept)
    concept["content"] = res.get("content") or ""

    # 证据收集：词条 refs + 按概念名补检索（含事件摘要参考）
    try:
        refs = list(dict.fromkeys(
            list(concept.get("refs", [])) + _title_refs(slug, title)))
    except Exception:
        refs = list(concept.get("refs", []))
    entries = _collect_ref_entries(slug, refs)

    # 事件侧证据（可选）
    event_context, event_summary = "", ""
    if event_id:
        event_id = str(event_id).strip()
        ev_key = ".".join(event_id.split(".")[:2])
        event_summary = load_summary(slug, ev_key) or ""
        if not event_summary:
            try:
                from .wiki import resolve_raw_ref
                r = resolve_raw_ref(f"raw://{slug}/{event_id.split('.')[0]}.json"
                                    f"#{event_id}")
                pg = r["data"].get("page") if r["ok"] else None
                if pg is not None:
                    event_summary = (load_summary(slug, f"{ev_key}.p{pg}")
                                     or "")
            except Exception:
                pass
        event_context = build_event_context(slug, event_id)

    prompt = _arbitration_prompt(slug, concept, entries,
                                 event_context, event_summary, conflict)
    try:
        if llm_responder is not None:
            text = llm_responder(prompt)
        else:
            text = _real_llm(prompt, note=f"{slug}/{title}", llm_cfg=llm_cfg)
    except Exception as exc:
        return f"仲裁调用失败：{type(exc).__name__}: {exc}"
    data = _parse_verdict(text)
    return _fmt_result(slug, concept, data)
