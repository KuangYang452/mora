# -*- coding: utf-8 -*-
"""LLM 响应解析与状态结算 —— llm_parse

从 llm.py 拆出（M1，见 docs/REFACTOR_DESIGN.md §4）：LLM 原始响应 →
Reply（台词/状态字段/技能）、工具调用提取、推理内容与缓存统计提取、
查询意向检测、状态结算（硬编码规则优先，约定 6）、update_state 结果
语义化回传。

依赖：session（初始状态/降级台词/状态语义化）、content_mode（技能白名单）、
llm_client（ChatError——调用链异常类型）、data（apply_delta）、textutil
（台词清洗）。不依赖 llm（编排）与 llm_prompt——解析层与组装/编排解耦。
"""

import json
import re

from textutil import strip_paren_annotations, strip_time_prefix
from data import apply_delta

import session
from session import INITIAL_STATE, _CHAR, _semantic_state
import content_mode
from content_mode import _mode_allowed_skills
from llm_client import ChatError


class Reply:
    """解析后的 LLM 回合输出。"""

    def __init__(self, reply: str, affection_delta=0, inner_thought="",
                 emote="none", bounce=False, skills=None, raw_text=""):
        self.reply = reply
        self.affection_delta = affection_delta
        self.inner_thought = inner_thought
        self.emote = emote
        self.bounce = bool(bounce)
        self.skills = skills          # None = LLM 未声明（保留原状态）；[] = 关闭
        self.raw_text = raw_text


def _extract_json(text: str):
    """三级容错：直接解析 → 代码块 → 首尾大括号。"""
    text = (text or "").strip()
    if not text:
        return None
    # 1) 直接解析
    try:
        return json.loads(text)
    except Exception:
        pass
    # 2) 代码块
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    # 3) 首尾大括号
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return None


def _fallback_line(key: str, default: str) -> str:
    """降级台词：优先角色包 fallback 模板（实例化），缺省用通用表述。"""
    fb = _CHAR.fallback(key)
    if fb:
        return _CHAR.instantiate(fb)
    return _CHAR.instantiate(default)


def parse_llm_reply(text: str) -> Reply:
    """解析 LLM 输出为 Reply；解析失败时降级为纯文本台词。"""
    obj = _extract_json(text)
    if obj is None:
        # 降级：模型没按格式输出，把原文当台词（括号旁白一并清洗）
        return Reply(reply=strip_paren_annotations((text or "").strip())[:300]
                     or _fallback_line("silent", "（{{char_self}}只是安静地看着你）"),
                     raw_text=text)

    def s(*keys, default=""):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return default

    def i(key, default=0):
        try:
            return int(obj.get(key, default))
        except (TypeError, ValueError):
            return default

    reply = strip_paren_annotations(s("reply", "speech", "text", "message"))
    if not reply:
        reply = _fallback_line("silent", "（{{char_self}}只是安静地看着你）")
    skills = obj.get("skills")
    if isinstance(skills, str) and skills.strip():
        skills = [s.strip() for s in skills.split(",") if s.strip()]
    elif not isinstance(skills, list):
        skills = None
    return Reply(
        reply=reply[:400],
        affection_delta=i("affection_delta", 0),
        inner_thought=s("inner_thought", "thought"),
        emote=s("emote", "emotion", default="none"),
        bounce=bool(obj.get("bounce", False)),
        skills=skills,
        raw_text=text,
    )


# ---------------------------------------------------------------------------
# 状态结算（硬编码规则，LLM 权限低于此）
# ---------------------------------------------------------------------------

def apply_state(state: dict, parsed: Reply) -> dict:
    """按硬编码规则应用 LLM 的调整。

    - affection：只认 delta，经 data.apply_delta 限幅结算；LLM 无法直接写绝对值。
    - inner_thought：动态数据，LLM 可自主更新。
    - 等级：由数值经 session.level_for 映射，LLM 无法直接指定。
    - skills：LLM 通过工具声明的技能开关（单独变量），程序只做白名单过滤；
      白名单不依赖原生思考模式（reasoning）：推理通道是 think 工具，始终可用；
      白名单按当前内容模式过滤（content_mode._mode_allowed_skills：SFW 下
      剔除 nsfw_only 技能，如角色卡的性癖分析，含存档残留激活状态）。
    """
    new = dict(state)
    new["affection"] = apply_delta(int(state.get("affection", INITIAL_STATE["affection"])),
                                   parsed.affection_delta)
    if parsed.inner_thought:
        new["inner_thought"] = parsed.inner_thought[:100]
    # 技能白名单 = data.SKILLS 定义 ∩ 当前内容模式允许（_mode_allowed_skills，
    # SFW 下剔除 nsfw_only 技能）。未声明时保留原状态，但同样按当前模式过滤
    # （清除 SFW 下存档残留的 nsfw_only 激活状态）。
    allowed = _mode_allowed_skills()
    if parsed.skills is not None:
        new["skills"] = [s for s in parsed.skills if s in allowed]
    else:
        new["skills"] = [s for s in new.get("skills", []) if s in allowed]
    return new


# ---------------------------------------------------------------------------
# 原始响应解析（function calling 路径）
# ---------------------------------------------------------------------------

def extract_reasoning(resp: dict) -> str:
    """从 API 响应提取推理内容文本（reasoning_content / reasoning 字段）；无则空串。

    推理模型（deepseek-reasoner 等）会在 message.reasoning_content 返回思考链；
    部分 OpenAI 兼容端点用 message.reasoning。仅提取字符串，不做任何处理。
    """
    try:
        msg = resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    for key in ("reasoning_content", "reasoning"):
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _cache_stats(resp: dict) -> str or None:
    """从响应 usage 提取缓存命中统计（DeepSeek 系字段：
    prompt_cache_hit_tokens / prompt_cache_miss_tokens，或
    prompt_tokens_details.cached_tokens）；端点无缓存字段时返回 None
    （不记录，避免误导）。
    """
    try:
        usage = resp["usage"]
    except (KeyError, TypeError):
        return None
    if not isinstance(usage, dict):
        return None
    hit = usage.get("prompt_cache_hit_tokens")
    if hit is None:
        hit = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    total = usage.get("prompt_tokens")
    if hit is None and miss is None:
        return None
    hit = int(hit or 0)
    miss = int(miss if miss is not None else max(0, int(total or 0) - hit))
    total_n = int(total or (hit + miss))
    ratio = (hit / total_n * 100) if total_n else 0.0
    return f"缓存命中: hit={hit} miss={miss} 命中率={ratio:.1f}% (总 {total_n})"


def parse_llm_response(resp: dict) -> Reply:
    """从 API 响应解析 Reply：content → 台词；tool_calls → 状态字段。

    兼容旧路径：模型未走工具而把 JSON 写进 content 时，回退到 parse_llm_reply。
    """
    try:
        msg = resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise ChatError(f"响应格式异常: {str(resp)[:300]}")

    content = strip_paren_annotations(
        strip_time_prefix((msg.get("content") or "").strip()))
    state_args = {}
    speech = None
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        if fn.get("name") == "say":
            # 台词通道：say.text 优先于 content（content 仅作降级）
            try:
                sa = json.loads(fn.get("arguments") or "{}")
            except (TypeError, ValueError):
                sa = {}
            t = strip_paren_annotations(
                strip_time_prefix(str(sa.get("text") or "")).strip())
            if t:
                speech = t
        elif fn.get("name") == "update_state":
            try:
                state_args = json.loads(fn.get("arguments") or "{}")
            except (TypeError, ValueError):
                state_args = {}

    if speech or state_args:
        # 工具路径：say.text 是台词（缺省回退 content），状态字段来自 update_state
        return Reply(
            reply=speech or content or _fallback_line("silent", "（{{char_self}}只是安静地看着你）"),
            affection_delta=state_args.get("affection_delta", 0),
            inner_thought=state_args.get("inner_thought", ""),
            emote=state_args.get("emote", "none"),
            bounce=bool(state_args.get("bounce", False)),
            skills=state_args.get("skills"),
            raw_text=json.dumps(resp, ensure_ascii=False),
        )
    # 未走工具：content 可能是纯文本（降级为台词）或旧的 JSON 对象
    return parse_llm_reply(content or _fallback_line("silent", "（{{char_self}}只是安静地看着你）"))


def extract_tool_calls(resp: dict) -> list:
    """从 API 响应提取工具调用列表 [{id, name, arguments}]；无则空列表。

    agent 循环使用：模型调用工具后，程序执行并把结果作为 tool 角色消息
    回传，让模型决定是否继续推进（直至不再调用工具或到达轮数上限）。
    """
    try:
        msg = resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return []
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        calls.append({
            "id": tc.get("id") or f"call_{len(calls) + 1}",
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments") or "{}",
        })
    return calls


# 查询意向信号（供 agent 循环校验"说了要查但没真调工具"）：
# 命中词覆盖角色的典型口头查询说法——翻书/翻翻/查询/查证/原文/设定/词条。
# 保守起见不匹配单字"查"与弱信号"看看"（闲聊里"让吾辈看看"≠查询意图）。
_QUERY_INTENT_RE = re.compile(
    r"翻[书翻]|查(?:询|证|一|下|查|资料|设定|原文|书)|原文|设定|词条|搜(?:索)?")


def has_query_intent(text: str) -> bool:
    """台词/推理文本是否出现查询意向；供意向-动作校验（retry_on_vague_query）。"""
    return bool(_QUERY_INTENT_RE.search(text or ""))


def tool_result_text(state: dict) -> str:
    """update_state 执行结果回传给 LLM 的语义化摘要（不暴露代码结构）。"""
    skills = state.get("skills") or []
    skill_s = "、".join(skills) if skills else "无"
    text = (
        "状态更新已生效。\n"
        + _semantic_state(state)
        + f"\n当前已激活技能：{skill_s}。\n"
        "你可以调用 say 工具说出台词结束本回合（台词与状态更新可同时调用）。"
    )
    # 显性关闭提示：技能激活是持久的，话题结束后需要 LLM 显式关闭，
    # 避免技能残留到无关对话。只提示当前内容模式下仍可用的激活技能
    # （SFW 下 nsfw_only 技能已被过滤，无残留可能，不提示）。
    active = sorted(_mode_allowed_skills() & set(skills))
    if active:
        text += (
            f"\n提示：技能（{'、'.join(active)}）仍处于激活状态。"
            "话题结束或离开后，请在下次调用 update_state 时把 skills "
            "设为空数组以关闭它。"
        )
    return text


# ---------------------------------------------------------------------------
# 离线自测（无需 API Key）
# ---------------------------------------------------------------------------

def selftest() -> None:
    # function calling：响应解析（content 台词 + tool_calls 状态）
    resp_tool = {
        "choices": [{"message": {
            "role": "assistant",
            "content": "哼哼～杂鱼❤️ 今天想看书吗？",
            "tool_calls": [{"function": {
                "name": "update_state",
                "arguments": '{"affection_delta": 2, "inner_thought": "想逗逗他", '
                             '"emote": "戏谑", "bounce": true, "skills": ["fetish_analysis"]}'}}],
        }}]}
    pr = parse_llm_response(resp_tool)
    assert pr.reply == "哼哼～杂鱼❤️ 今天想看书吗？", pr.reply
    assert pr.affection_delta == 2 and pr.inner_thought == "想逗逗他"
    assert pr.emote == "戏谑" and pr.bounce is True
    assert pr.skills == ["fetish_analysis"], pr.skills
    # 纯文本响应（未走工具）→ 降级为台词
    pr2 = parse_llm_response({"choices": [{"message": {"content": "吾辈懒得理你～"}}]})
    assert pr2.reply == "吾辈懒得理你～" and pr2.affection_delta == 0
    # 旧路径兼容：content 是 JSON 对象（模型没走工具）
    pr3 = parse_llm_response({"choices": [{"message": {
        "content": '{"reply": "x", "affection_delta": 1}'}}]})
    assert pr3.affection_delta == 1, pr3.__dict__
    # 非状态工具（rmgame）调用：不影响状态，content 仍是台词
    pr_t = parse_llm_response({"choices": [{"message": {
        "content": "吾辈去开游戏了～",
        "tool_calls": [{"function": {"name": "start_game",
                                     "arguments": '{"game": "demo"}'}}]}}]})
    assert pr_t.reply == "吾辈去开游戏了～" and pr_t.affection_delta == 0, pr_t.__dict__
    assert pr_t.skills is None and pr_t.inner_thought == "", pr_t.__dict__
    # say 台词通道：say.text 优先（可与 update_state 并行）；content 仅降级
    pr_say = parse_llm_response({"choices": [{"message": {
        "content": "（不应被采用）",
        "tool_calls": [
            {"function": {"name": "say",
                          "arguments": '{"text": "哼哼～杂鱼❤️ 看什么呢？"}'}},
            {"function": {"name": "update_state",
                          "arguments": '{"affection_delta": 1, "emote": "戏谑", '
                                       '"bounce": true, "skills": []}'}},
        ]}}]})
    assert pr_say.reply == "哼哼～杂鱼❤️ 看什么呢？", pr_say.__dict__
    assert pr_say.affection_delta == 1 and pr_say.emote == "戏谑"
    assert pr_say.bounce is True and pr_say.skills == [], pr_say.__dict__
    assert "不应被采用" not in pr_say.reply, "say.text 应优先于 content"
    # 括号旁白清洗：say.text 与 content 降级路径都剥括号内容
    pr_say_paren = parse_llm_response({"choices": [{"message": {
        "tool_calls": [{"function": {"name": "say",
                                      "arguments": "{\"text\": \"（歪头凑近）看什么看\"}"}}]}}]})
    assert pr_say_paren.reply == "看什么看", pr_say_paren.__dict__
    pr_c_paren = parse_llm_response({"choices": [{"message": {
        "content": "(飘到屏幕前) 哎呀呀～（得意）"}}]})
    assert pr_c_paren.reply == "哎呀呀～", pr_c_paren.__dict__
    print("  function calling: say 台词 + update_state 状态 / 纯文本降级 / 旧 JSON 兼容 / 多工具 ✓")

    # agent 循环：工具调用提取 + 工具结果语义化回传
    calls = extract_tool_calls(resp_tool)
    assert len(calls) == 1 and calls[0]["name"] == "update_state", calls
    assert calls[0]["arguments"] and '"skills"' in calls[0]["arguments"], calls
    assert extract_tool_calls({"choices": [{"message": {"content": "x"}}]}) == []
    assert extract_tool_calls({}) == []
    rst = tool_result_text(dict(session.INITIAL_STATE))
    assert "状态更新已生效" in rst and "好感度 20/100" in rst, rst
    rst_skill = tool_result_text({"affection": 20, "inner_thought": "x", "skills": ["fetish_analysis"]})
    assert "fetish_analysis" in rst_skill and "调用 say 工具说出台词" in rst_skill, rst_skill
    # 显性关闭提示：技能激活时回传应提醒关闭方式；未激活不出现
    assert "设为空数组以关闭它" in rst_skill, "应含显性关闭提示"
    assert "设为空数组以关闭它" not in rst, "未激活技能不应有关闭提示"
    assert "affection_delta" not in rst, "工具结果不应暴露代码字段名"
    print("  agent 循环: 工具调用提取 / 语义化结果回传（不暴露字段名）✓")

    # 查询意向检测（意向-动作校验 retry_on_vague_query）
    assert has_query_intent("让吾辈翻翻书确认一下❤️") is True
    assert has_query_intent("吾辈去查证一下设定") is True
    assert has_query_intent("吾辈看看这迷宫有什么好怕的") is False, "弱信号「看看」不应命中"
    assert has_query_intent("哼哼～杂鱼看什么呢") is False
    assert has_query_intent("") is False
    print("  意向检测: has_query_intent（翻书/查证/原文/设定 命中，弱信号不误伤）✓")

    # JSON 解析：标准 / 代码块包裹 / 带废话 / 完全非 JSON
    r1 = parse_llm_reply('{"reply": "哼哼～杂鱼❤️", "affection_delta": 3, '
                         '"inner_thought": "这家伙今天有点可爱", "emote": "得意", "bounce": true}')
    assert r1.reply == "哼哼～杂鱼❤️" and r1.affection_delta == 3 and r1.bounce is True, r1.__dict__
    r2 = parse_llm_reply('好的，以下是回复：\n```json\n{"reply": "杂鱼看什么看", "affection_delta": -2}\n```\n完')
    assert r2.reply == "杂鱼看什么看" and r2.affection_delta == -2, r2.__dict__
    r3 = parse_llm_reply('哼哼～杂鱼❤️ 吾辈今天心情不错～')
    assert "杂鱼" in r3.reply, r3.__dict__  # 降级为纯文本
    # skills 解析：列表 / 逗号字符串 / 未声明
    r4 = parse_llm_reply('{"reply": "x", "skills": ["fetish_analysis"]}')
    assert r4.skills == ["fetish_analysis"], r4.skills
    r5 = parse_llm_reply('{"reply": "x", "skills": "fetish_analysis, other"}')
    assert r5.skills == ["fetish_analysis", "other"], r5.skills
    r6 = parse_llm_reply('{"reply": "x"}')
    assert r6.skills is None, r6.skills

    # 状态结算：delta 限幅 + 等级映射 + LLM 无法直接写绝对值
    st = dict(session.INITIAL_STATE)  # 20
    st = apply_state(st, parse_llm_reply('{"reply": "x", "affection_delta": 99}'))
    assert st["affection"] == 25, st  # 20 + 5（硬编码上限）
    assert session.level_for(st["affection"]) == "初遇", session.level_for(st["affection"])
    st = apply_state(st, parse_llm_reply('{"reply": "x", "affection_delta": -99}'))
    assert st["affection"] == 20, st  # 25 - 5
    st = apply_state(st, parse_llm_reply('{"reply": "x", "affection": 100}'))
    assert st["affection"] == 20, st  # LLM 想直接写绝对值 → 无效，无 delta 则不变
    st = apply_state(st, parse_llm_reply('{"reply": "x", "inner_thought": "想摸鱼"}'))
    assert st["inner_thought"] == "想摸鱼", st

    # 技能管理：LLM 通过工具声明；程序白名单过滤；未声明保留原状态
    # 白名单与原生思考模式解耦（推理通道是 think 工具，始终可用）
    st2 = dict(session.INITIAL_STATE)
    st2 = apply_state(st2, parse_llm_reply('{"reply": "x", "skills": ["fetish_analysis"]}'))
    assert st2["skills"] == ["fetish_analysis"], st2
    st2 = apply_state(st2, parse_llm_reply('{"reply": "x", "skills": ["未知技能"]}'))
    assert st2["skills"] == [], st2  # 白名单过滤：未知技能被剔除
    st2 = apply_state(st2, parse_llm_reply('{"reply": "x"}'))
    assert st2["skills"] == [], st2  # 未声明 → 保留（已为 []）
    st_any = apply_state(dict(session.INITIAL_STATE),
                         parse_llm_reply('{"reply": "x", "skills": ["fetish_analysis"]}'))
    assert st_any["skills"] == ["fetish_analysis"], "fetish_analysis 白名单不依赖原生思考模式"
    print("[llm_parse.selftest] 通过 ✓ function calling / 工具提取 / 意向检测 / JSON 容错 / 状态结算与白名单")


if __name__ == "__main__":
    selftest()
