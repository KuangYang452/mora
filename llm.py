# -*- coding: utf-8 -*-
"""LLM 调用编排与兼容垫片 —— llm

M1 拆分（见 docs/REFACTOR_DESIGN.md §4）后，本模块只保留：
- 编排：call_llm（统一入口，全局串行锁 + 通用 LLM 调用日志 + 空响应重试）、
  _build_tools（工具 schema，唯一来源 tools.SPECS）、summarize_history
  （历史合并）、_fmt_messages_for_log（日志渲染）；
- 兼容垫片：从 llm_prompt / llm_parse / llm_client / content_mode /
  session / llm_card 再导出历史符号（迁移期消费方 from llm import X
  零改动；v2.1 移除垫片，消费方改直连新模块）。

全仓架构约定以 README「架构」八条为唯一入口；领域实现见各新模块
docstring（llm_prompt / llm_parse / llm_client / content_mode / session /
llm_card），本模块不重复。
"""

import json
import threading

import logutil
import settings
import tools

import content_mode
import llm_card
import llm_client
import llm_parse
import llm_prompt
import session
from llm_client import ChatError, ChatClient, load_llm_config
from llm_parse import _cache_stats, extract_reasoning

# 全局串行锁：所有 LLM 调用（回合/合并/摘要/wiki 重写）排队执行，
# 任意时刻只有一个请求在途，后续调用阻塞等待（避免并发调用与上下文交错）。
_LLM_CALL_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 日志渲染（输入忠实性：content + tool_calls 全量）
# ---------------------------------------------------------------------------

def _fmt_messages_for_log(messages: list) -> str:
    """messages 列表 → 日志用可读文本（忠实于输入：content + tool_calls 全量渲染）。

    日志必须等于输入：assistant 消息的 tool_calls 字段（调用名 + 参数 JSON）
    也逐条列出，避免「日志看不到自己调过什么」的误导；tool 消息附
    tool_call_id。system / user / assistant / tool 一律按原始字段渲染。
    """
    if not messages:
        return "（空）"
    lines = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content") or ""
        if role == "tool":
            content = f"{content}\n（tool_call_id: {m.get('tool_call_id', '')}）"
        tcs = m.get("tool_calls")
        if tcs:
            tc_lines = []
            for tc in tcs:
                fn = tc.get("function") or {}
                tc_lines.append(
                    f"  - {fn.get('name', '?')}: {fn.get('arguments', '{}')}")
            content = (f"{content}\n（tool_calls: {len(tcs)}）\n"
                       + "\n".join(tc_lines))
        lines.append(f"────── {role.upper()} ──────\n{content}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 统一 LLM 调用入口
# ---------------------------------------------------------------------------

def call_llm(messages: list, *, tools: list = None, kind: str = "round",
             retry: bool = False, timeout: int = 180,
             tool_choice: str = None,
             max_tokens: int = None, note: str = "",
             model: str = None, temperature: float = None,
             reasoning: bool = None) -> dict:
    """统一 LLM 调用入口 —— 所有 LLM 调用必须走此接口。

    - 配置默认唯一来源 setting/llm.ini（内部 llm_client.ChatClient）；
      model / temperature 仅作显式临时覆盖（CLI 等场景），不设兜底默认值；
    - 自动写通用 LLM 调用日志（log/llm_<kind>_*.txt，含完整交付消息与
      原始响应，成功/失败均记录，见 logutil.log_llm_call）；
    - retry=True 时空响应自动重试一次（chat_with_retry）；
    - 失败抛异常（日志已记录 ok=False）。
    """
    cfg = dict(load_llm_config())
    if max_tokens:
        cfg["max_tokens"] = max_tokens
    if model:
        cfg["model"] = model
    if temperature is not None:
        cfg["temperature"] = float(temperature)
    if reasoning is not None:
        cfg["reasoning"] = bool(reasoning)
    if tool_choice is None:
        # 会话级默认：settings.app_get("tool_choice")（"auto" 保持旧行为；热键，
        # 修改 app.ini 后下回合生效）
        tool_choice = settings.app_get("tool_choice", "auto")
    client = ChatClient(llm_cfg=cfg)
    prompt_log = _fmt_messages_for_log(messages)
    print(f"\n{'=' * 20} LLM 调用 [{kind}] {'=' * 20}")
    if note:
        print(f"note: {note}")
    print(prompt_log)
    # 同步串行：全部 LLM 调用排队（持锁期间后续 call_llm 阻塞等待），
    # 避免回合/合并/摘要/wiki 重写并发交错，也避免同刻多个请求打 API。
    with _LLM_CALL_LOCK:
        try:
            if retry:
                resp = client.chat_with_retry(messages, timeout=timeout,
                                              tools=tools,
                                              tool_choice=tool_choice)
            else:
                resp = client.chat(messages, timeout=timeout, tools=tools,
                                   tool_choice=tool_choice)
            resp_text = json.dumps(resp, ensure_ascii=False, indent=2)
            reasoning_text = extract_reasoning(resp)
            cache_line = _cache_stats(resp)
            logutil.log_llm_call(kind, prompt_log, resp_text, ok=True,
                                 note=note, reasoning=reasoning_text,
                                 cache=cache_line)
            print(f"{'=' * 20} LLM 响应 [{kind}] {'=' * 20}")
            if cache_line:
                print(cache_line)
            if reasoning_text:
                print("【LLM 推理内容】")
                print(reasoning_text)
            print(resp_text)
            return resp
        except Exception as exc:
            logutil.log_llm_call(kind, prompt_log,
                                 f"{type(exc).__name__}: {exc}",
                                 ok=False, note=note)
            print(f"{'=' * 20} LLM 调用失败 [{kind}] {'=' * 20}")
            print(f"{type(exc).__name__}: {exc}")
            raise


def _build_tools() -> list:
    """全部工具 schema（唯一来源 tools.SPECS；rmgame 工具按开关热读过滤）。"""
    return tools.schema_list(rmgame_enabled=settings.app_get("rmgame_enabled", True))


def summarize_history(msgs: list, old_merge: dict = None,
                      llm_responder=None) -> str or None:
    """把较早的对话历史压缩为合并摘要。

    msgs：被合并的原始消息列表（含 time）。
    old_merge：旧合并条目 dict（{summary, ...}）或 None，供新摘要延续。
    返回摘要文本；调用失败/过短返回 None（调用方保持现状，下次再试）。

    时间窗不由 LLM 标注：摘要只写内容，程序在注入时按被合并消息的绝对
    时间生成绝对时间窗（context._merge_time_span，逐字节稳定），避免相对
    时间随注入漂移误导时间线。
    """
    from context import abs_time_label
    lines = []
    for m in msgs:
        label = abs_time_label(m.get("time"))
        who = "角色" if m.get("role") == "assistant" else "对方"
        head = f"{label} {who}：" if label else f"{who}："
        lines.append(head + m.get("content", ""))
    prompt = (
        "你是对话摘要助手。下面是一段较早的角色（当前桌宠）"
        "与对方的对话历史，已被移出当前上下文。\n"
        "请压缩为结构化摘要（纯重写，不照抄原文台词）：\n"
        "- 剧情/话题进展、重要信息、对方与角色关系的变化（好感相关事件）；\n"
        "- 保留关键称呼与话语要点；\n"
        "- 输出 200~400 字。\n"
        "- 只输出摘要正文：不要标注时间段（时间窗由程序按被合并消息的"
        "绝对时间生成），不要使用 Markdown 符号（如 **、###、- 列表），"
        "用自然语言叙述。\n"
    )
    if old_merge and (old_merge.get("summary") or "").strip():
        prompt += ("\n==== 更早的合并摘要（延续其信息，勿重复展开）====\n"
                   + old_merge["summary"][:1500] + "\n")
    prompt += "\n==== 对话历史 ====\n" + "\n".join(lines)[:6000] + "\n==== 结束 ===="
    try:
        if llm_responder is not None:
            raw = llm_responder(prompt)
        else:
            resp = call_llm([{"role": "user", "content": prompt}],
                            kind="merge", max_tokens=8192, reasoning=False,
                            note=f"历史合并 | {len(msgs)} 条消息 | "
                                 f"旧合并{'有' if old_merge else '无'}")
            raw = resp["choices"][0]["message"].get("content") or ""
        text = (raw or "").strip()
        return text if len(text) > 20 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 兼容垫片（M1 迁移期：消费方 from llm import X 零改动；v2.1 移除垫片，
# 消费方按行内注释的目标模块改直连导入。移除时按本段符号清单逐一核对。）
# ---------------------------------------------------------------------------
from content_mode import (  # noqa: E402,F401  （迁移目标：content_mode）
    CONTENT_MODE, _mode_allowed_skills, _normalize_content_mode,
    content_mode_directive, content_mode_rule, set_content_mode)
from session import (  # noqa: E402,F401  （迁移目标：session）
    INITIAL_STATE, USER_REFERENCE, _CHAR, _semantic_state, get_character,
    level_for)
from llm_card import (  # noqa: E402,F401  （迁移目标：llm_card）
    _adapt_style, _categorize_book, _clean, _instantiate, _psych_cot,
    _strip_status_block, extract_opening)
from llm_prompt import (  # noqa: E402,F401  （迁移目标：llm_prompt）
    THINKING_STYLE_INSTRUCT, activation_instruction, build_activation,
    build_system_prompt, env_section, mid_static_sections, state_section,
    thinking_style_for, tool_list_section, turn_section)
from llm_parse import (  # noqa: E402,F401  （迁移目标：llm_parse）
    Reply, apply_state, extract_tool_calls, has_query_intent, parse_llm_reply,
    parse_llm_response, tool_result_text)
from llm_client import ChatError, ChatClient, load_llm_config  # noqa: E402,F401


# ---------------------------------------------------------------------------
# 离线自测（无需 API Key）：领域自测已随各新模块（各自可独立运行），
# 本函数验证编排层与跨模块组装
# ---------------------------------------------------------------------------

def selftest() -> None:
    content_mode.selftest()
    session.selftest()
    llm_card.selftest()
    llm_prompt.selftest()
    llm_parse.selftest()
    llm_client.selftest()
    from textutil import selftest as textutil_selftest
    textutil_selftest()
    _selftest_orchestration()


def _selftest_orchestration() -> None:
    card = session.get_character()
    st0 = dict(session.INITIAL_STATE)

    # 工具 schema 注册表（唯一来源 tools.SPECS，与 function schema 一致）
    tools_list = _build_tools()
    names = [t["function"]["name"] for t in tools_list]
    assert names[0] == "say", names
    assert names[1] == "update_state", names
    assert {"start_game", "read_current_text", "query_wiki", "scan_game"} <= set(names), names
    assert "discover_running" in names, "应注册运行中游戏查询工具"
    st_tool = next(t for t in tools_list if t["function"]["name"] == "start_game")
    assert st_tool["function"]["parameters"]["required"] == ["game"], st_tool
    qw_tool = next(t for t in tools_list if t["function"]["name"] == "query_wiki")
    assert qw_tool["function"]["parameters"]["required"] == ["game"], qw_tool
    assert "query_archive" in names, "应注册归档查询工具"
    notes_tool = next(t for t in tools_list if t["function"]["name"] == "mora_notes")
    note_acts = notes_tool["function"]["parameters"]["properties"]["action"]["enum"]
    assert set(note_acts) == {"list", "read", "write", "append", "delete"}, note_acts
    assert notes_tool["function"]["parameters"]["required"] == ["action"], notes_tool
    arb = next(t for t in tools_list if t["function"]["name"] == "wiki_arbitrate")
    assert arb["function"]["parameters"]["required"] == ["game", "concept"], arb
    spec_names = {s.name for s in tools.SPECS}
    assert spec_names == {"say", "update_state", "think", "discover_running",
                          "start_game", "read_current_text", "query_wiki",
                          "scan_game", "read_raw_text", "wiki_arbitrate",
                          "wiki_rebuild", "rename_game", "query_archive",
                          "mora_notes"}, spec_names
    assert "emote" not in spec_names and "bounce" not in spec_names, \
        "emote/bounce 应为 update_state 参数"
    assert spec_names <= set(names), f"SPECS 含 schema 未注册的工具: {spec_names - set(names)}"
    assert tools.query_names() == {"discover_running", "read_current_text", "query_wiki",
                                   "scan_game", "read_raw_text", "wiki_arbitrate",
                                   "wiki_rebuild"}, tools.query_names()
    assert tools.game_world_names() == tools.query_names() | {"start_game"}, \
        tools.game_world_names()
    print("  tools.SPECS: 与 function schema 名称集合一致（无 emote/bounce 幽灵工具）✓")

    # rmgame 开关关闭：schema 不注册 + 提示词清单/行为提示同步隐藏（单一来源过滤）
    _saved_enabled = settings.app_get("rmgame_enabled", True)
    try:
        settings.override("rmgame_enabled", False)
        names_off = [t["function"]["name"] for t in _build_tools()]
        assert "query_wiki" not in names_off and "start_game" not in names_off, names_off
        assert {"say", "update_state", "query_archive", "mora_notes"} <= set(names_off), names_off
        sp_off = llm_prompt.build_system_prompt(card, state=st0)
        assert "query_wiki" not in sp_off and "start_game" not in sp_off, \
            "rmgame 关闭后提示词不应列出 rmgame 工具"
        assert "「read_current_text」" not in sp_off, "工具清单条目应移出静态段"
        tl_off = llm_prompt.tool_list_section(None)
        assert tl_off and "query_wiki" not in tl_off and "start_game" not in tl_off, \
            "rmgame 关闭后工具清单不应含 rmgame 工具"
        assert "query_archive" in tl_off and "mora_notes" in tl_off, "常开工具应保留"
        assert "可调用 read_current_text" not in sp_off, "开关关闭后不应提示调用 rmgame 工具"
        assert "【对方正在…】" not in sp_off
        assert "game_context" not in sp_off, "开关关闭后技能清单不应含 game_context"
        act_off = llm_prompt.activation_instruction()
        assert "先查再答" not in act_off, "开关关闭后激活指令不应含 GAME_RULES"
    finally:
        settings.override("rmgame_enabled", _saved_enabled)
    print("  rmgame 开关: 关闭后 schema 与提示词清单同步隐藏（单一来源过滤）✓")
    print("  多工具 schema: rmgame 9 工具 + query_archive + mora_notes（按开关注册）✓")

    # 历史合并摘要：提示词含时间标注要求；fake responder 验证调用链
    merge_msgs = [
        {"role": "user", "content": "你好", "time": "2026-08-12T19:00:00"},
        {"role": "assistant", "content": "哼哼～", "time": "2026-08-12T19:00:05"},
    ]
    seen = {}

    def fake_resp(prompt):
        seen["prompt"] = prompt
        return "这段对话发生于 3 小时前。聊了初次见面的内容。"

    out = summarize_history(merge_msgs, old_merge={"summary": "旧摘要"},
                            llm_responder=fake_resp)
    assert out and "3 小时前" in out, out
    assert "对话历史" in seen["prompt"] and "不要标注时间段" in seen["prompt"], \
        "合并提示词应要求不标注时间（时间窗由程序换算）"
    assert "不要使用 Markdown 符号" in seen["prompt"], "合并提示词应要求纯文本（无 Markdown）"
    assert "旧摘要" in seen["prompt"], "合并提示词应延续旧合并摘要"
    assert summarize_history(merge_msgs, llm_responder=lambda p: "") is None, "过短应返回 None"
    print("  历史合并: summarize_history（绝对时间输入 / 旧摘要延续 / 失败降级）✓")

    # 跨模块组装集成：静态在前 → 半动态 → 历史 → 动态尾部 → 时间锚点 → 激活指令
    from context import ContextManager as _CM
    ctx_i = _CM(rounds=2, opening=["开场A", "开场B"])
    ctx_i.add_user("你好")
    ctx_i.add_assistant("哼哼～")
    sys_prompt = llm_prompt.build_system_prompt(card, state=st0)
    msgs_i = ctx_i.build_messages(
        sys_prompt,
        activation="ACT",
        first_user_instr=llm_prompt.thinking_style_for(st0),
        pre_time=[llm_prompt.env_section(None) or "E",
                  llm_prompt.tool_list_section(None) or "T",
                  llm_prompt.state_section(st0)],
        mid_static=llm_prompt.mid_static_sections(st0),
    )
    assert msgs_i[0]["role"] == "system", "静态段在前"
    assert "【好感等级规则】" in msgs_i[1]["content"], "半动态段应在静态之后"
    assert msgs_i[2]["role"] != "system", "半动态段之后才是历史（开场白为 assistant）"
    tail_sys = [m["content"] for m in msgs_i if m["role"] == "system"]
    assert any("【本回合可用工具】" in c for c in tail_sys), "工具清单段应注入（尾部）"
    assert "【当前时间】" in msgs_i[-2]["content"] and msgs_i[-1]["content"].endswith("ACT"), \
        "时间锚点在激活指令之前"
    assert not any("【本回合推进】" in c for c in tail_sys), \
        "推进段应由调用方在每轮尾部追加（不进冻结前缀）"
    print("  组装集成: 静态在前 → 半动态 → 历史 → 动态尾部 → 时间锚点 → 激活指令 ✓")

    print("[llm.selftest] 全部通过 ✓（领域模块自测 + 编排/组装集成）")


if __name__ == "__main__":
    selftest()
