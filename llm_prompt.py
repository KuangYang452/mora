# -*- coding: utf-8 -*-
"""语义化提示词组装 —— llm_prompt

从 llm.py 拆出（M1，见 docs/REFACTOR_DESIGN.md §4）：把角色卡原文与
状态/环境组装为交付给 LLM 的语义化提示词段——静态 system prompt
（build_system_prompt）+ 半动态段（mid_static_sections）+ 动态尾部段
（env_section / tool_list_section / state_section / turn_section）+
激活指令（build_activation）+ 思维链风格指令（thinking_style_for）。
全仓架构约定以 README「架构」八条为唯一入口，本 docstring 只引用不重复。

- 静态在前、动态在后（约定 4）：build_system_prompt 只含纯静态内容
  （身份/性格/情境/记忆/行为协议），输出与 state/env 无关——等级/技能/
  游戏环境全部由调用方经 build_messages 的 mid_static / pre_time 注入，
  静态段因此完全字节稳定（缓存前缀不随等级/技能/游戏启停变化）。
- 提示词完全语义化（约定 5）：状态用自然语言叙述（session._semantic_state），
  不暴露代码结构。
- 提示词吝啬（约定 7）与内容模式两处投放、技能→开关依赖方向（约定 8）：
  见 content_mode 模块（本模块为实现处的调用方）。
- 提示词篇幅预算（约定 7 机器可执行形态）：prompt_budget() 单一预算表，
  selftest 断言 + debug.py --prompt 逐段打印，防开发迭代回潮。
- 输出协议（约定 6）：台词走 say、状态走 update_state，权限低于硬编码；
  工具清单由 tools.SPECS 派生（单一来源，与 function schema 同源）。

依赖：session（快照/状态语义化）、content_mode、llm_card（卡解析）、
data（SKILLS/GAME_RULES/SCENE）、tools、settings。不依赖 llm（编排）
与 llm_parse/llm_client——提示词组装与调用/解析解耦。
"""

import re

import settings
import tools
from data import GAME_RULES, SCENE, SKILLS

import session
from session import USER_REFERENCE, _CHAR, INITIAL_STATE, get_character, level_for, _semantic_state
import content_mode
from content_mode import _mode_allowed_skills, content_mode_rule, content_mode_directive
import llm_card
from llm_card import (COT_ADAPT, _adapt_style, _categorize_book, _clean,
                      _instantiate, _psych_cot)


# ---------------------------------------------------------------------------
# 指令层常量：不放入 system prompt，而是作为对话上下文之后的独立消息
# （ContextManager.build_messages 附加）—— 激活指令为最后一条 system
# 消息，靠近模型生成位置，服从度更高。
# ---------------------------------------------------------------------------

def activation_instruction() -> str:
    """激活指令主体：按 rmgame 开关决定是否含游戏点评引用句。

    行为细节（台词规范/状态更新/工具用法/自主推进）以系统提示词
    【每回合的行为方式】与【输出红线】为权威；本指令只做开场与引用，
    不重复规则全文——避免同一条规则在上下文两处出现、模型对
    「哪处为准」产生歧义。GAME_RULES 全文只由游戏环境段
    （env_section 的【游戏点评规则】）注入一次。
    rmgame 关闭时游戏点评工具未注册，引用句一并隐藏。
    """
    text = (
        f"现在，请以{_CHAR.identity}的身份，对{USER_REFERENCE}作出回应。\n"
        "你的内部思考通过 think 工具进行，台词通过 say 工具说出，状态通过 update_state 工具更新；"
        "台词、状态与其他工具的具体要求，以系统提示词"
        "【每回合的行为方式】与【输出红线】为准。\n"
    )
    if settings.app_get("rmgame_enabled", True):
        text += (
            f"{USER_REFERENCE}正在玩 RPG Maker 游戏时，回答前先按随游戏环境段注入的"
            "【游戏点评规则】执行先查再答，不得凭空脑补游戏设定。\n"
        )
    return text

# 思维链风格指令：参照 DeepSeek-V4 角色扮演思考模式切换指南
# （github.com/victorchen96/deepseek_v4_rolepaly_instruct）的指令块，
# 注入到第一条 user 消息末尾（ContextManager.build_messages first_user_instr）：
# - 激活 fetish_analysis → 思维模式要求（推理式思维链）
# - 未激活 → 角色沉浸要求（角色扮演式思维链）
# 文本借鉴仓库原文的精神而非照抄：前两条形式规则保留原文，第 3 条贴合本场景
# （思维模式要求 → 心理分析；角色沉浸要求 → 角色扮演式内心戏）。
# 原版指令指向原生思考块（<think>），但本项目工具循环内的推理通道是 think 工具
# （DeepSeek 工具调用与原生思维链互斥，工具循环内没有 reasoning_content 通道）——
# 指令改指 think 工具记录的内容，风格规则照常约束模型在 think 调用中如何思考。
THINKING_STYLE_INSTRUCT = {
    "logical": (
        f"""【思维模式要求】在你的思考（think 工具记录）中，请遵守以下规则：
1. 禁止使用圆括号包裹内心独白，例如\"（心想：……）\"或\"(内心OS：……)\"，
所有分析内容直接陈述即可
2. 禁止以角色第一人称描写内心活动，例如\"我心想\"\"我觉得\"\"我暗自\"等，
请用分析性语言替代
3. 思考内容应聚焦于心理分析：分析{USER_REFERENCE}的言行与性癖线索、调用心理学与
性学理论、形成可检验的假设、规划追问与引导方式，不要在思考中进行
角色扮演式的内心戏表演
4. 分析用词与所读文本同尺度：直白词照用、不代称不省略，
不得在思考里先行委婉降级（思考层的收敛会逐层传导到台词）"""
    ),
    "immersion": (
        "【角色沉浸要求】在你的思考（think 工具记录）中，请遵守以下规则：\n"
        "1. 以角色第一人称直接陈述内心想法与感受，不要用（心想：…）等括号"
        "包裹写法，"
        "2. 用第一人称描写角色的内心感受，例如\"我心想\"\"我觉得\"\"我暗自\"等\n"
        "3. 思考内容应沉浸在角色中："
        + (_CHAR.instantiate(_CHAR.immersion_perspective)
           if _CHAR.immersion_perspective
           else f"以角色的视角观察{USER_REFERENCE}")
        + f"，通过内心独白分析{USER_REFERENCE}的言行与心思，并规划如何回应"
        + "\n4. 思考用词与所读文本同尺度：直白词照用、不代称不省略，"
          "不得在思考里先行委婉降级（思考层的收敛会逐层传导到台词）"
    ),
}


def thinking_style_for(state: dict = None) -> str:
    """按技能状态选择思维链风格指令：激活 fetish_analysis → 推理式，否则角色沉浸式。

    返回的指令文本注入到第一条 user 消息末尾（build_messages 的 first_user_instr）。
    指令作用于 think 工具记录的内容（工具循环内的推理通道），与原生思考模式
    （setting/llm.ini 的 reasoning → thinking.type）无关，始终注入。
    """
    state = state or {}
    if _mode_allowed_skills() & set(state.get("skills") or []):
        return THINKING_STYLE_INSTRUCT["logical"]
    return THINKING_STYLE_INSTRUCT["immersion"]


def build_activation(state: dict = None, card: dict = None) -> str:
    """完整激活指令 = 内容模式标记 + activation_instruction() [+ 心理 COT（<thinking_format> 包裹，仅激活时）]。

    内容模式裸标记（`内容模式:NSFW`）放首行：build_messages 以「【本回合指令】\n」+
    本返回值组装，标记因此紧邻【本回合指令】标题、贴近输出位置——模型有安全化
    倾向，需在输出前重复强调模式（近因效应）；规则行另见【输出红线】块
    （content_mode_rule）。
    心理 COT 内容取自原型「心理COT」世界书条目（llm_card._psych_cot），占位符已
    实例化；原型步骤后追加 COT_ADAPT（素材来源 + 收敛判断），以 <thinking_format>
    标签包裹，明确推理只通过 think 工具记录（think 是工具循环内的推理通道——
    DeepSeek 工具调用与原生思维链互斥，工具循环内无 reasoning_content 通道），
    不写入 say 台词或其他工具参数。
    心理 COT 不依赖原生思考模式（setting/llm.ini 的 reasoning）：think 工具通道
    始终可用，技能激活即注入，reasoning 开闭不影响。
    """
    act = content_mode_directive() + "\n" + activation_instruction()
    state = state or {}
    if card and (_mode_allowed_skills() & set(state.get("skills") or [])):
        cot_title, cot_body = _psych_cot(card)
        if cot_body:
            act += (
                "\n\n<thinking_format>\n"
                f"在思考阶段按以下步骤完成心理分析推理；推理只通过 think 工具记录"
                "（DeepSeek 工具调用与思维链互斥，工具循环内没有 reasoning_content "
                "通道），不得写入 say 台词或其他工具参数。\n"
                "必须完整走完所有步骤（含最后的收敛判断）后再输出台词，"
                "不得跳过思考直接回复。\n\n"
                + cot_body
                + COT_ADAPT
                + "\n</thinking_format>"
            )
    return act


# ---------------------------------------------------------------------------
# 动态环境段
# ---------------------------------------------------------------------------

def _env_section(env: dict) -> str or None:
    """游戏环境快照 → 语义化叙述（动态环境层）；无有效信息返回 None。

    env 由调用方（pet.py）传入 runtime/current.json 的新鲜快照；
    本模块不读写数据文件（架构约定）。

    环境段整体以 <game_environment> 包裹：这段内容全部属于"游戏世界"，
    与角色（桌宠）所处的现实/图书馆世界在结构上隔离。段首映射句显式
    建立三方同一性：{USER_REFERENCE}=玩家、{USER_REFERENCE}队伍中的角色=
    {USER_REFERENCE}在游戏中的化身、游戏角色≠角色（角色不在游戏内），防止
    模型把游戏内叙述（如谢拉的视角/台词）内化为自身经历。映射句不指名
    任何具体游戏或角色，换任何游戏都成立（避免过拟合到《猎妻迷宫》/谢拉）。
    """
    game = (env or {}).get("game_name") or (env or {}).get("game") or ""
    if not game:
        return None
    lines = [
        f"{USER_REFERENCE}似乎正在玩《{game}》。",
        "以下环境信息全部描述游戏世界内部的情况：其中出现的角色"
        f"（含{USER_REFERENCE}队伍成员）都是游戏内的人物——{USER_REFERENCE}操控的那一个，"
        f"是{USER_REFERENCE}在游戏中的化身，游戏内位置即{USER_REFERENCE}所在位置；其余是"
        f"{USER_REFERENCE}同行的游戏角色。它们都与你（角色）无关：你不在游戏内，"
        f"只是游戏外的观察者与点评者。{USER_REFERENCE}若拿游戏角色与你的性格或"
        "偏好作类比，那只是比喻，不是你的经历。",
    ]
    map_name = env.get("map_name") or env.get("map_id")
    if map_name:
        lines.append(f"当前地图：{map_name}")
    if env.get("scene"):
        lines.append(f"当前场景：{env.get('scene')}")
    # 战斗信息（Scene_Battle 时展示敌方/阶段/玩家方/可用行动）
    if env.get("scene") == "Scene_Battle":
        bt = (env.get("battle_troop") or "").strip()
        bp = (env.get("battle_phase") or "").strip()
        if bt or bp:
            parts = []
            if bt:
                parts.append(f"敌方部队：{bt}")
            if bp:
                parts.append(f"战斗阶段：{bp}")
            lines.append("战斗信息：" + "，".join(parts))
        ai = (env.get("actor_info") or "").strip()
        ac = (env.get("actor_commands") or "").strip()
        if ai:
            lines.append(f"当前行动者：{ai}")
        if ac:
            lines.append(f"可用行动：{ac}")
        # 技能选择界面：技能表 + 当前选中技能详情
        sl = (env.get("skill_list") or "").strip()
        sc = (env.get("skill_current") or "").strip()
        if sl:
            lines.append(f"技能表：{sl}")
        if sc:
            lines.append(f"当前选中技能：{sc}")
    # {USER_REFERENCE}队伍成员（任何场景都有值：探索/菜单/战斗）。
    # 用"{USER_REFERENCE}队伍"而非"我方"：环境段是{USER_REFERENCE}（玩家）的视角，
    # 避免模型把"我方"解读为角色一方、把游戏角色当成与角色同侧的存在。
    pi = (env.get("party_info") or "").strip()
    if pi:
        lines.append(f"{USER_REFERENCE}队伍成员：{pi}")
    # 菜单界面（Scene_Menu）：命令列表 + 当前选中（_STATE_EXPR 自动仅在菜单时有值）
    mc = (env.get("menu_commands") or "").strip()
    mcur = (env.get("menu_current") or "").strip()
    if mc:
        lines.append(f"菜单命令：{mc}")
    if mcur:
        lines.append(f"当前选中：{mcur}")
    # 通用列表/帮助窗口通配（图鉴/物品/技能等自定义界面；跳过 _commandWindow 防重复）
    lc = (env.get("list_current") or "").strip()
    ht = (env.get("help_text") or "").strip()
    if lc:
        lines.append(f"当前界面选中：{lc}")
    if ht:
        lines.append(f"帮助文本：{ht[:120]}")
    # 当前对话：CDP 的 text 是精确原文直接显示；OCR 只在匹配到 raw 时显示
    # （匹配失败不暴露 OCR 噪声，避免误导）
    src = env.get("source", "cdp")
    if src == "cdp":
        text = (env.get("text") or "").strip()
        if text:
            lines.append(f"当前对话：「{text[:80]}」")
    else:
        text = (env.get("matched_text") or "").strip()
        if text:
            lines.append(f"当前对话：「{text[:80]}」")
        elif (env.get("text") or "").strip():
            lines.append("当前对话：（画面文字未能识别，勿据此猜测）")
    # 事件信息：环境段只锚定当前位置（事件前文=原文开头，不剧透）；
    # 事件摘要（全文概述，含当前位置之后的内容，可能剧透）不进环境段——
    # 只经 read_current_text 工具输出提供，角色主动查询时才知道后续剧情。
    ec = (env.get("event_context") or "").strip()
    if ec:
        lines.append("事件前文（原文开头）：")
        lines.append(f"{ec[:200]}")
    mid = (env.get("match_id") or "").strip()
    if mid:
        lines.append(
            f"如需当前事件的剧情概述，调用 read_current_text（返回该事件全文摘要，"
            f"含当前位置之前与之后的内容，可能剧透）；"
            f"核对逐字台词用 read_raw_text 按条目 id「{mid}」读取该事件页完整上下文。")
    lines.append(f"你可以用 query_wiki 查询该游戏的资料，或点评{USER_REFERENCE}的游戏进度。")
    return (f"【{USER_REFERENCE}正在…】\n<game_environment>\n"
            + "\n".join(lines) + "\n</game_environment>")


def env_section(env: dict) -> str or None:
    """游戏环境快照段（动态尾部注入）：环境语义化叙述 + 【游戏点评规则】。

    环境快照随游戏画面高频变化；GAME_RULES 全文（data.GAME_RULES，单一
    事实来源）紧随其后注入——从行为协议段移出，使静态 system prompt
    不随游戏启停变化（缓存前缀稳定）。游戏名为自动识别名时附改名引导
    （预期链路：读取内容 → rename_game 起规范名）。无有效环境信息返回 None。
    """
    seg = _env_section(env)
    if not seg:
        return None
    if settings.app_get("rmgame_enabled", True):
        rules = f"【游戏点评规则】（{USER_REFERENCE}正在玩 RPG Maker 游戏时生效）\n" + GAME_RULES
        wb = (SKILLS.get("game_context", {}) or {}).get("world_book") or ""
        if wb:
            rules += "\n" + wb
        seg = seg + "\n\n" + rules
        # 改名触发：游戏名是自动识别名（含版本号/后缀）时给出引导，
        # 让「识别 → 读取 → 改名」链路自发打通（rename_game 见工具清单）
        gname = (env or {}).get("game_name") or (env or {}).get("game") or ""
        if _looks_auto_name(gname):
            seg += (f"\n（游戏名「{gname}」是自动识别名，含版本号/文件名残留；"
                    "读取到足够内容后可调用 rename_game 基于游戏内容给它起规范"
                    "名称，之后用新名指代即可。）")
    return seg


def tool_list_section(env: dict = None) -> str or None:
    """工具清单段（动态尾部注入）：本回合可用工具的完整清单。

    从静态行为协议段移出（清单随游戏环境动态变化，放尾部不切缓存前缀；
    见 build_system_prompt 协议段只引用本段）。条目由 tools.SPECS 派生
    （名称+简述+参数+返回值，与 function schema 同源，不会双源不一致）；
    say / update_state / think 已有专门行为条目不重复列出。
    动态组装规则：discover_running / start_game 常驻（发现与启动在任何
    状态下都可用）；其余 rmgame 工具仅在有游戏环境时注入（没有环境时
    读取/查询类工具不可用——模型应先用 discover_running 确认，再于
    下一轮按尾部清单调用）。rmgame 关闭时全部 rmgame 工具不注入。
    无工具可列（rmgame 关闭且仅剩内建工具被排除时）返回 None。
    """
    if not settings.app_get("rmgame_enabled", True):
        listed = [s for s in tools.specs(False)
                  if s.name not in ("say", "update_state", "think")]
    else:
        env_on = bool(env)
        listed = []
        for s in tools.specs(True):
            if s.name in ("say", "update_state", "think"):
                continue
            if s.rmgame and s.name not in ("discover_running", "start_game") \
                    and not env_on:
                continue
            listed.append(s)
    if not listed:
        return None
    lines = [t.prompt_entry() for t in listed]
    return ("【本回合可用工具】\n"
            + "\n".join("  · " + ln for ln in lines))


def turn_section(agent_turn: tuple) -> str or None:
    """本回合推进节奏段（每轮尾部注入——多轮工具循环中轮次计数每轮必变，
    故不进入冻结前缀；首轮给完整纪律与轮次预算，后续轮逐步收紧，末轮
    强制交付）。agent_turn=None 返回 None。
    """
    if agent_turn is None:
        return None
    turn, max_turns = agent_turn
    max_turns = max(1, int(max_turns))
    if turn >= max_turns:
        rhythm = (f"本回合已到第 {turn}/{max_turns} 轮（上限）。"
                  "请直接调用 say 工具说出台词交付，不要再调用其他工具。")
    elif turn <= 1:
        rhythm = (f"本回合你最多可自主推进 {max_turns} 轮工具调用。"
                  "建议节奏：需要多轮查证时，先用 think 写下分析计划与"
                  "待验证要点；每轮最多调用一个查询工具，多轮查证时逐轮"
                  "推进并把每轮关键结论写入 think，"
                  "基于结果组织台词后，信息足够立即调用 say 交付；"
                  "不要零散地一次只查一项，也不要为查而查。")
    else:
        rhythm = (f"本回合已进行 {turn - 1} 轮。若信息已足够，"
                  "请直接调用 say 说出台词交付；"
                  "仅当关键信息仍缺失时再调用查询工具。")
    return "【本回合推进】\n" + rhythm


# ---------------------------------------------------------------------------
# 半动态层（mid_static，由 pet.py 经 build_messages(mid_static=) 在静态
# system 之后、中期记忆之前注入）与动态尾部段（pre_time，历史之后、
# 时间锚点之前注入）：
# - 半动态【好感等级规则】【已激活技能】：跨级/激活才变（按设计预期一日
#   不跨级），参与日常缓存前缀；跨级断点只落在本段，身份块不受影响。
# - 动态【{USER_REFERENCE}正在…】：游戏环境快照持续变化；
# - 动态【本回合可用工具】：随环境动态组装（有无游戏环境决定 rmgame 工具）；
# - 动态【当前状态】：内心想法高频变化（模型每轮更新）；
# - 动态【本回合推进】：轮次计数每轮必变。
# 动态段若留在 system prompt 内，会在协议段与历史段之前切断缓存前缀
# （静态在前、动态在后），故整体后置；半动态段按变化频率介于两者之间。
# ---------------------------------------------------------------------------

def mid_static_sections(state: dict = None) -> list:
    """半动态段（mid_static 注入，静态 system 之后、中期记忆之前）：
    好感等级规则 + 已激活技能 world book。

    返回字符串列表（0~2 项），由调用方经 build_messages(mid_static=) 注入。
    变化频率低于合并条目（等级跨级才变 / 技能激活才变），故位置在静态段
    与中期记忆之间：日常请求逐字节稳定、参与缓存前缀；低频变化时断点只
    落在本段，静态身份块与历史不受影响。好感等级仅注入当前一级（契约
    字段优先 identity.json#affection_levels，自然语言，回退世界书条目）；
    技能段仅注入 fetish_analysis 的世界书（按 state.skills 精确注入）。
    输出经 llm_card._instantiate 实例化占位符（{{user}}/{{char}}/{{random::}}
    等）——契约文本可能含 SillyTavern 占位符，不实例化会原样泄漏给模型
    （见日志 213405：等级规则里出现字面 {{user}}）。
    """
    state = state or {}
    out = []
    aff = int(state.get("affection", INITIAL_STATE["affection"]))
    cur_level = level_for(aff)
    _, levels, skills = _categorize_book(get_character())
    level_text = None
    idt_levels = (_CHAR.identity_data or {}).get("affection_levels") or {}
    if isinstance(idt_levels, dict) and (idt_levels.get(cur_level) or "").strip():
        level_text = idt_levels[cur_level].strip()
    elif cur_level in levels:
        level_text = levels[cur_level]
    if level_text:
        out.append("【好感等级规则】\n" + level_text)
    # 已激活技能：按当前内容模式过滤后的激活技能注入对应世界书
    # （_categorize_book 的技能条目按技能名对应；SFW 下 nsfw_only 技能已被
    # _mode_allowed_skills 排除，残留激活状态不注入）
    if (_mode_allowed_skills() & set(state.get("skills") or [])) and skills:
        out.append("【已激活技能】\n" + "\n\n".join(skills))
    return [_instantiate(s) for s in out]


def state_section(state: dict = None) -> str:
    """当前状态段（动态尾部注入）：语义化状态叙述，好感度数值隐去。"""
    return "【当前状态】\n" + _semantic_state(state, with_affection=False)


# 自动识别名检测：库中名含版本号/引擎后缀/文件名残留（如 DemonsRoots1.1.1、
# 村輪辱紀行ver1.0.1、celesphonia_mv）时，引导角色基于已读内容调用
# rename_game 起规范名（自动发现入库的原始名通常不干净，改名是预期链路：
# 识别 → 读取 → rename_game → 之后用新名指代；改完名字变干净，提示自灭）。
_AUTO_NAME_RE = re.compile(
    r"(?:ver\s*\d|v?\d+[.]\d+|\d+[.]\d+(?:[.]\d+)?$|_[a-z0-9]+$|\.exe$)",
    re.I)


def _looks_auto_name(name: str) -> bool:
    """判断游戏名是否像自动识别名（含版本号/引擎后缀/文件残留）。"""
    return bool(name and _AUTO_NAME_RE.search(name))


def build_system_prompt(card: dict, state: dict = None, env: dict = None) -> str:
    """把角色卡原文组装为系统提示词（只含静态内容）。

    身份/性格/情境/记忆/行为协议。**输出与 state 等级段、env 无关**：
    好感等级规则与技能 world book 是半动态段（mid_static_sections，跨级/
    激活才变），游戏环境信息全部由 env_section 在动态尾部注入，
    game_context 的 world_book 与 GAME_RULES 全文随其【游戏点评规则】段——
    静态段因此完全字节稳定（缓存前缀不随等级/技能/游戏启停变化）。
    动态内容（【{USER_REFERENCE}正在…】/【本回合可用工具】/【当前状态】）由 env_section /
    tool_list_section / state_section 生成、经调用方在消息序列后部注入（见
    ContextManager.build_messages 的 pre_time 参数）；【本回合推进】由
    turn_section 生成、每轮尾部注入——避免在 system prompt 内出现高频
    变化文本而切断缓存前缀。
    原则（引用 README「架构」约定，不重复全文）：
    - 角色卡怎么写就怎么用——不删减、不改写、不做内容过滤（约定 8）。
    - 状态只以语义化叙述出现（session._semantic_state），LLM 看不到任何结构（约定 5）。
    - 输出协议：LLM 一切输出走原生工具调用（function calling，约定 6），
      字段权限在提示词中语义化说明。
    - 技能注入不依赖原生思考模式（setting/llm.ini 的 reasoning）：推理通道是
      think 工具（始终可用），fetish_analysis 在激活时照常注入。
    """
    return _instantiate("\n\n".join(_static_parts(card)))


def _static_parts(card: dict) -> list:
    """静态段列表（build_system_prompt 的同源拆分）：只依赖 card 与模块
    常量，与 state/env 无关（约定 4 静态在前）。供篇幅预算表 prompt_budget
    逐段测量——提示词吝啬（约定 7）的机器可执行形态。
    """
    parts = []

    if _CHAR.render_identity():
        # 【你的身份与世界观】来自 identity.json 契约（character/SCHEMA.md）——
        # 结构化字段 + 固定模板渲染，不再直灌角色卡 description 的 YAML 风格文本。
        parts.append("【你的身份与世界观】\n" + _CHAR.render_identity())

    if card.get("personality"):
        parts.append("【性格】\n" + _clean(card["personality"]))

    # 静态场景（程序级 SCENE 覆盖 > 契约 scenario.default）→ 唯一【情境】段
    # 场景为空间描写（舞台说明性质，第三人称、{{char}} 指代角色），不再以
    # 「你身处于」拼接；persona 与关系规则/常驻对象声明已移出——persona
    # 内容与 identity 契约（surface/relationships）重复，关系规则并入
    # 行为协议「对话边界」小节（契约见 character/SCHEMA.md）
    scene_text = (SCENE or _CHAR.scenario_default).strip()
    if scene_text:
        situation = scene_text
    else:
        situation = f"这里是角色与{USER_REFERENCE}聊天的空间，周围的环境与摆设由角色的设定决定。"
    parts.append("【情境】\n" + situation)

    # 记忆层：三层记忆的认知框架（短期=上下文原文 / 中期=合并摘要 / 长期=归档）。
    # 让 LLM 意识到自己的记忆分层，并引导「回忆不真切时主动查归档」，
    # 避免它把模糊记忆当成事实编造（query_archive 的用法详见工具 schema）。
    parts.append(
        "【记忆与回忆】\n"
        "你有三层记忆，由近及远：\n"
        "- 短期记忆：上方上下文中的最近对话原文，可直接看到。\n"
        "- 中期记忆：上下文顶部标注【中期记忆】的那条压缩摘要，"
        "涵盖更早但仍不遥远的过往。\n"
        "- 长期记忆：更早的对话经压缩后归档，通过 query_archive 工具查询"
        "（默认返回摘要；需核对逐字台词时设 detail=true 取原文）。\n"
        "当某个细节记得不真切、或需要追溯更早的对话时，"
        "先调用 query_archive 检索归档，不要凭空编造或含糊带过。"
    )

    # 条件层：世界书按用途分类注入（不再无条件全量）
    style, _levels, _skills = _categorize_book(card)
    if style and settings.app_get("mesugaki_style_block", False):
        # <Mesugaki> 风格块（2024 年旧模块，默认关闭）：当时 LLM 可能未内化
        # mesugaki 语言风格故单独注入；现模型已内化，靠人设段即可。
        # 启用时原样注入，但剥离与工具调用协议冲突的叙述教条（_adapt_style）。
        parts.append("【风格】\n" + "\n\n".join(_adapt_style(s) for s in style))
    # 好感等级与技能 world book 不进入静态 system prompt（半动态层
    # mid_static_sections 注入，见下）：等级规则跨级才变、技能激活才变，
    # 埋在静态块中部会在低频变化时切断缓存前缀（见日志 203145/203141
    # hit=1280 实测）；移出后静态段完全字节稳定。

    # 行为协议：台词与状态更新都走原生工具调用（function calling）。
    # 激活指令由 ContextManager 作为对话上下文之后的独立 system 消息附加。
    # 工具清单不在协议段逐条列出（随尾部【本回合可用工具】段动态注入，
    # 见 tool_list_section——清单随游戏环境变化，放尾部不切缓存前缀）；
    # 清单与 function schema 同源（唯一来源 tools.SPECS），由 SPECS 派生
    # 渲染（与 API 注册的 schema 同源，不会双源不一致）。
    # 技能清单：只列当前内容模式下可用的技能（SFW 下 nsfw_only 技能
    # 自动隐藏，见 content_mode._mode_allowed_skills）
    listed_skills = {k: v for k, v in SKILLS.items()
                     if k in _mode_allowed_skills()}
    if not settings.app_get("rmgame_enabled", True):
        listed_skills = {k: v for k, v in listed_skills.items()
                         if k != "game_context"}
    skills_desc = "、".join(
        f"「{k}」({v['desc']}；{v['trigger_hint']})" for k, v in listed_skills.items())
    proto = (
        "【每回合的行为方式】\n"
        "你的一切输出都必须通过工具完成，不得输出工具之外的文本：\n"
        "- 台词：调用 say 工具说出（1~3句、总长不超过60字，口语化，可以带表情和❤️；"
        "台词是你说出口的话本身，不得含时间标注，也不得用括号写旁白、动作、解释或"
        "内心想法——这些一律放进 update_state 工具的 inner_thought / emote 字段）。\n"
        "- 状态：通过调用 update_state 工具完成（而不是写进台词）：\n"
        "  · affection_delta：好感度变化趋势，整数 -5~+5；你无法直接改数值，由规则结算。\n"
        "  · inner_thought：你的内心想法（一句话），可自主更新。\n"
        "  · emote / bounce：情绪与动作。\n"
        "  · skills：技能开关。" + skills_desc + "，用不到就传空数组。\n"
        "- 思考：多轮推进时调用 think 工具记录分析框架与跨轮结论：开始前先写下"
        "计划/待验证要点，每轮查询得出关键结论后更新它"
        "（查询结果只保留最近两轮、更早的结果不再保留，需跨轮引用的结论必须经 "
        "think 保留）。你的完整思路链会以思考块呈现在对话中：块首是思考标记，"
        "块内按轮次累积 think 文本，工具调用以单行反引号追加在链内"
        "（如 `query_archive:关键词`）；块保持开放、不闭合——思考是否结束"
        "由你调用 say 工具交付来体现。think 仅本回合内可见、回合结束后不保留；"
        "区别于 update_state 的 inner_thought——那是持久状态，会保留到后续回合；"
        "长期记忆用 mora_notes。\n"
        "- 工具结果：查询结果以 tool 消息回传，只保留最近两轮的工具调用与结果；"
        "不要在结果可见前凭猜测作答。\n"
        "- 其他工具：本回合可用工具及完整用法见尾部【本回合可用工具】段（与函数"
        "schema 同源）；**查询类工具每轮最多调用一个**——多轮查证时逐轮推进、"
        "把结论写入 think，不要在同一轮批量调用多个查询。\n"
        "- 对话边界：对话中只有两个常驻对象：你（"
        f"{_CHAR.display_name}）与{USER_REFERENCE}（你称他「{_CHAR.user_ref}」）。"
        f"对话或游戏里出现的其他人名/角色名都是{USER_REFERENCE}讲述的故事或游戏中的角色，"
        "不是对话参与者——不要把它们当作你的对话对象。\n"
    )
    # 【游戏点评】GAME_RULES 不在协议段注入：全文随游戏环境段
    # （env_section 的【游戏点评规则】）注入，使协议段完全静态、
    # 不随游戏启停变化（缓存前缀稳定）。激活指令与技能 world_book
    # 只引用该段，不重复规则全文（单一事实来源 data.GAME_RULES）。
    proto += (
        f"- 环境缺失时的行为：提示词中没有游戏环境信息时，说明你不清楚{USER_REFERENCE}此刻"
        "在做什么——不要凭空猜测，直接说明暂时看不到"
        + ("（可用 discover_running 查看当前是否有游戏在运行；有游戏运行时，"
           "相关读取工具会出现在尾部【本回合可用工具】段）。\n"
           if settings.app_get("rmgame_enabled", True) else "。\n")
        + (f"- 启动游戏：{USER_REFERENCE}请求启动/打开某个 RPG Maker 游戏时（即使游戏当前未运行），"
           "调用 start_game 启动（game 用游戏库中的名称或 slug，可用 discover_running "
           "查看库中游戏）；启动成功后即可用 read_current_text 读取其文本。\n"
           if settings.app_get("rmgame_enabled", True) else "")
        + "响应示例（需要说话并更新状态时，参考以下并列结构）：\n"
        "同时调用 say 与 update_state 两个工具：\n"
        'say：text = "哦～？终于想通要来找我聊聊了吗？那就让我听听你的故事吧～"\n'
        'update_state：affection_delta = 1、inner_thought = "他总算愿意开口了"、'
        'emote = "得意"、bounce = true、skills = []' + "\n"
        "本回合内可连续多次调用工具自主推进工作（每轮最多一个查询工具；"
        "查询工具 / update_state 会立即执行并返回结果，你可据此继续）；"
        "调用 say 说出台词即结束本回合。\n"
        "【输出红线】\n"
        "- 台词必须通过 say 工具说出：不要直接输出文本，也不要在文本里写"
        "「say：xxx」之类的伪调用。\n"
        "- 工具参数只在调用时填写：不要把参数内容或旧 JSON 格式"
        "（如 {\"reply\": …}）当作文本输出。\n"
        # 内容模式并进【输出红线】块（不单独成段）：贴合红线格式
        # 「- 主体：约束。」只留可执行约束（NSFW：不允许回避；SFW：不允许
        # 输出），模式含义由激活指令首行标记与模型常识承载；静态提示词
        # 每回合重建，该行随 content_mode 翻转，切换后下一回合即生效。
        + content_mode_rule() + "\n"
    )
    parts.append(proto)

    # 统一实例化原型占位符（{{user}}/{{char}}/{{random::}}/{{roll::}}）后返回。
    # 动态段（【当前状态】/【{USER_REFERENCE}正在…】/【本回合推进】）已移出本函数，
    # 由 state_section / env_section / turn_section 生成、调用方在消息序列
    # 后部注入（缓存：静态在前、动态在后）；心理 COT 由 build_activation
    # 注入激活指令（<thinking_format> 包裹），思维链风格指令由 ContextManager
    # 注入第一条 user 消息（thinking_style_for，指向 think 工具通道）。
    return parts


# ---------------------------------------------------------------------------
# 提示词篇幅预算（工程级约定 7 的机器可执行形态）
# ---------------------------------------------------------------------------
# 目的：把「多轮重构后的健康状态」固化为回归基线，防开发迭代回潮——
# 提示词现状是多次针对性重构的结果（CHANGELOG「工程约定理顺」），不是
# 机制在约束；预算表让吝啬原则可执行：段超限 → selftest 红 → 工程师必须
# 压缩/引用式复用（约定 7 的 ①②③），或带理由改预算（一次可见 diff）。
# 基线：2026-08 实测（debug.py --prompt，静态 3972 字符）。预算 ≈ 实测
# × 1.3 取整；卡数据段（身份/性格/情境/等级/技能/环境）从宽，框架自持段
# （记忆框架/行为协议/指令附加）从严。运行期绝不截断——预算只做开发期断言。

_FRAMEWORK_STATIC_BUDGET = 3000  # 【记忆与回忆】+【每回合的行为方式】合计上限

_PROMPT_BUDGETS = {
    # —— 静态段（_static_parts / build_system_prompt）——
    "【你的身份与世界观】": 2100,   # 实测 1670；identity.json 契约渲染（卡数据）
    "【性格】": 600,                # 莫拉无此段，预留（其他角色卡注入）
    "【情境】": 300,                # 实测 206；SCENE / 契约 scenario
    "【记忆与回忆】": 300,          # 实测 225；框架自持
    "【风格】": 900,                # mesugaki 旧模块默认关闭，预留
    "【每回合的行为方式】": 2700,   # 实测 1865；含【输出红线】+ 内容模式行；框架自持
    # —— 半动态段（mid_static_sections）——
    "【好感等级规则】": 900,        # 契约注入，跨级才变（卡数据）
    "【已激活技能】": 4600,         # 实测 3511（fetish 世界书全量注入）；卡数据段从宽
    # —— 动态尾部段（pre_time）——
    "【游戏环境】": 1800,           # 环境快照 + GAME_RULES 全文（受数据体量制约）
    "【本回合可用工具】": 1500,     # tools.SPECS 派生清单，随工具数增长
    "【当前状态】": 500,            # 语义化状态叙述
    # —— 指令附加 ——
    "【本回合指令】": 2100,         # 实测 1603（含心理COT 注入）；内容模式裸标记 + 激活主体 + COT
    "【思维链风格】": 700,          # 思维模式要求 / 角色沉浸要求
    "【本回合推进】": 400,          # 轮次节奏段（每轮尾部注入）
}


def prompt_budget(state: dict = None, card: dict = None,
                  env: dict = None) -> list:
    """提示词篇幅预算表（约定 7 机器可执行形态，单一事实来源 _PROMPT_BUDGETS）。

    返回逐段行 {section, chars, budget, over}：静态段（_static_parts 同源）、
    半动态段（mid_static_sections）、动态尾部段（env_section /
    tool_list_section / state_section）、指令附加（build_activation /
    thinking_style_for / turn_section，按交付形态拼标题）。段来源与本模块
    docstring 的分段一致；未登记预算的段一律标记超限（新增段必须登记预算，
    防回潮）。运行期不做任何截断——仅供 selftest 断言与 debug.py --prompt
    打印。
    """
    card = card if card is not None else get_character()
    state = state if state is not None else dict(session.INITIAL_STATE)
    rows = []

    def _row(section: str, text) -> None:
        if not text or not str(text).strip():
            return
        chars = len(str(text))
        budget = _PROMPT_BUDGETS.get(section)
        rows.append({"section": section, "chars": chars, "budget": budget,
                     "over": budget is None or chars > budget})

    # 静态段：与 build_system_prompt 同源，按段首标题登记（实例化后测量）
    for part in _static_parts(card):
        _row(part.split("\n", 1)[0], _instantiate(part))
    # 半动态段 + 动态尾部段 + 指令附加：直接调用构建器
    for seg in mid_static_sections(state):
        _row(seg.split("\n", 1)[0], seg)
    _row("【游戏环境】", env_section(env))
    _row("【本回合可用工具】", tool_list_section(env))
    _row("【当前状态】", state_section(state))
    _row("【本回合指令】", "【本回合指令】\n" + build_activation(state, card))
    _row("【思维链风格】", "【思维链风格】\n" + thinking_style_for(state))
    _row("【本回合推进】", turn_section((1, 4)))
    return rows


def framework_static_budget() -> int:
    """框架静态段（【记忆与回忆】+【每回合的行为方式】）总预算字符数。"""
    return _FRAMEWORK_STATIC_BUDGET


def _framework_text(state: dict = None, card: dict = None,
                    env: dict = None) -> str:
    """框架自持文本拼接（不含对话历史）：静态段 + 半动态段 + 动态尾部段 +
    指令附加。供跨段「单一事实来源」类断言使用（如内容模式恰两处投放）。
    """
    card = card if card is not None else get_character()
    state = state if state is not None else dict(session.INITIAL_STATE)
    segs = [_instantiate(p) for p in _static_parts(card)]
    segs += list(mid_static_sections(state))
    for s in (env_section(env), tool_list_section(env), state_section(state)):
        if s:
            segs.append(s)
    segs.append("【本回合指令】\n" + build_activation(state, card))
    segs.append("【思维链风格】\n" + thinking_style_for(state))
    return "\n\n".join(segs)


# ---------------------------------------------------------------------------
# 离线自测（依赖真实角色包结构；settings.override / content_mode 全局切换
# 均 try/finally 恢复，防测试污染）
# ---------------------------------------------------------------------------

def selftest() -> None:
    card = get_character()
    st0 = dict(session.INITIAL_STATE)
    sys_prompt = build_system_prompt(card, state=st0)
    assert session._CHAR.self_ref in sys_prompt, "提示词缺少自称"
    assert session._CHAR.user_ref in sys_prompt, "提示词缺少角色对用户的称呼"
    # 状态段已移出 system prompt（动态尾部注入）：好感度数值隐去、只留等级；
    # 数值在工具结果回传里展示（session._semantic_state 默认 with_affection）
    assert "好感度 20/100" not in sys_prompt, "好感度数值应从 system 状态段隐去"
    assert "【当前状态】" not in sys_prompt, "状态段应移出 system prompt（动态尾部注入）"
    st_seg = state_section(st0)
    assert "「初遇」" in st_seg and "好感度 20/100" not in st_seg, st_seg
    assert "此刻是 " not in sys_prompt, "时间唯一锚点在消息组装层（【当前状态】不应注入时间）"
    assert "affection_delta" in sys_prompt, "工具调用协议缺失"
    assert "update_state" in sys_prompt, "工具调用协议缺失"
    assert "调用 say 工具说出" in sys_prompt, "行为说明缺失（台词应走 say 工具）"
    assert "不得输出工具之外的文本" in sys_prompt, "强制工具通道约束缺失"
    assert session._CHAR.display_name in sys_prompt, "提示词应含角色显示名"
    # 输出通道指令必须被剔除：SillyTavern 的 updata/getvar/setvar 由
    # 硬编码变量管理器替代，不得残留在提示词中与工具调用协议冲突
    for marker in ("<updata>", "{{getvar", "setvar:", "<StatusBlock>"):
        assert marker not in sys_prompt, f"提示词残留通道指令: {marker}"

    # 提示词结构：三个"情境"已合并；激活指令 + 条件注入
    for old in ("【场景】", "【当前情境】", "【你现在身处的情境】", "【对话规则】"):
        assert old not in sys_prompt, f"旧段名残留: {old}"
    for new in ("【情境】", "【记忆与回忆】"):
        assert new in sys_prompt, f"缺少新段: {new}"
    # 好感等级规则是半动态段（mid_static_sections，跨级才变），不进入静态段
    assert "【好感等级规则】" not in sys_prompt, \
        "等级规则应移出静态 system prompt（半动态层 mid_static_sections）"
    mid_static_def = mid_static_sections(st0)
    assert any("【好感等级规则】" in s for s in mid_static_def), \
        "mid_static 应含当前等级规则"
    assert "<Fetish_analysys>" not in "\n".join(mid_static_def), "默认无技能不注入"
    assert "{{" not in "\n".join(mid_static_def), \
        "mid_static 应实例化占位符（{{user}} 等不得泄漏，见日志 213405）"
    # 记忆分层认知：三层框架 + 主动检索引导（落在【记忆与回忆】段内）
    mem_body = sys_prompt.split("【记忆与回忆】", 1)[1]
    mem_seg = mem_body.split("\n\n【", 1)[0]   # 按段落边界截取，段内【中期记忆】不截断
    for _kw in ("短期记忆", "中期记忆", "长期记忆", "query_archive"):
        assert _kw in mem_seg, f"记忆段缺少关键词: {_kw}"
    assert "不要凭空编造" in mem_seg, "记忆段应含不编造约束"
    # 好感等级仅注入当前一级（初遇），其他等级条目不注入
    level_seg = next(s for s in mid_static_sections(st0)
                     if s.startswith("【好感等级规则】"))
    level_seg = level_seg.split("【好感等级规则】", 1)[1]
    assert level_seg.strip(), "好感等级段为空"
    assert "恋慕" not in level_seg and "亲爱" not in level_seg, "不应注入其他等级"
    # 技能默认不注入（LLM 未激活）
    assert "<Fetish_analysys>" not in sys_prompt, "技能默认不应注入"
    assert "kinsey" not in sys_prompt.lower(), "理论条目默认不应注入"
    # <Mesugaki> 风格块默认关闭（app.ini 的 mesugaki_style_block，热键，旧模块）：
    # LLM 已内化 mesugaki 语言风格，靠人设段即可；开启时原样注入并改写叙述教条
    assert "<Mesugaki>" not in sys_prompt, "mesugaki 风格块应默认关闭"
    assert "【风格】" not in sys_prompt, "风格块关闭时不应有【风格】段"
    _saved_ms = settings.app_get("mesugaki_style_block", False)
    try:
        settings.override("mesugaki_style_block", True)
        sp_ms = build_system_prompt(card, state=st0)
        assert "<Mesugaki>" in sp_ms and "【风格】" in sp_ms, "开关打开应注入风格块"
        # 风格叙述教条已剥离：括号描述 → 引导到工具字段（inner_thought / emote）
        assert "括号描述" not in sp_ms, "叙述教条应已剥离"
        assert "内心/神态表达：通过 inner_thought 与 emote 字段承载" in sp_ms, "教条改写缺失"
    finally:
        settings.override("mesugaki_style_block", _saved_ms)
    # 行为协议：台词走 say 工具、状态走 update_state 工具
    assert "可以带表情和❤️" in sys_prompt
    assert "通过调用 update_state 工具完成" in sys_prompt, "工具协议说明缺失"
    assert "调用 say 工具说出" in sys_prompt, "台词工具说明缺失"
    assert "总长不超过60字" in sys_prompt, "台词长度限制缺失"
    assert "不要凭空猜测" in sys_prompt, "无环境信息不猜测约束缺失"
    # think 推理草稿通道：通用工具（非点评专用），行为协议应有语义说明
    assert "调用 think 工具记录" in sys_prompt, "行为协议应说明 think 推理草稿通道"
    assert "仅本回合内可见" in sys_prompt and "回合结束后不保留" in sys_prompt, \
        "think 语义（回合内可见、回合后不保留）缺失"
    assert "那是持久状态" in sys_prompt, "think 应区别于 inner_thought（持久状态）"
    assert "只保留最近两轮" in sys_prompt and "更早的结果不再保留" in sys_prompt, \
        "think 引导应说明结果过期机制（跨轮引用必须 think 保留）"
    assert "先写下计划" in sys_prompt, "think 引导应有主动规划用法（先计划→逐轮更新结论）"
    assert "思考块" in sys_prompt and "反引号" in sys_prompt, \
        "行为协议应说明多轮循环的思考块呈现（think 文本累积 + 反引号工具调用）"
    assert "不闭合" in sys_prompt, "思考块应说明保持开放（增量式不闭合，防误解为思考已结束）"
    # 工具结果通道：结果以 tool 消息回传、只保留最近两轮
    assert "以 tool 消息回传" in sys_prompt, "行为协议应说明工具结果经 tool 消息回传"
    assert "只保留最近两轮的工具调用与结果" in sys_prompt, \
        "行为协议应说明工具结果只保留最近两轮"
    assert "凭猜测作答" in sys_prompt, "工具结果语义应禁止结果可见前猜测作答"
    # 查询限一：协议段应声明查询类工具每轮最多一个
    assert "每轮最多调用一个" in sys_prompt, "协议应声明查询类工具每轮最多一个"
    assert "「read_current_text」" not in sys_prompt and "参数：" not in sys_prompt, \
        "工具清单条目应移出静态段（尾部动态注入）"
    # 输出红线：禁令 + 替代（不展示完整错误范例，避免反向污染/重教旧协议）
    assert "【输出红线】" in sys_prompt, "输出红线缺失"
    assert "不要直接输出文本" in sys_prompt and "伪调用" in sys_prompt, "红线缺直出/伪调用禁令"
    assert "不要把参数内容或旧 JSON 格式" in sys_prompt, "红线缺参数/旧 JSON 禁令"
    # 工具清单（【本回合可用工具】尾部动态段）：名称+简述+参数+返回值，
    # 由 tools.SPECS 派生（单一来源，与 function schema 同源）；say/
    # update_state/think 已有专门行为条目不重复列出；discover_running /
    # start_game 常驻，其余 rmgame 工具仅在有游戏环境时注入
    tl_env = tool_list_section({"game": "猎妻迷宫", "map_name": "Map001"})
    assert tl_env and "【本回合可用工具】" in tl_env, "有 env 应生成工具清单段"
    for t in tools.specs(rmgame_enabled=settings.app_get("rmgame_enabled", True)):
        if t.name in ("say", "update_state", "think"):
            assert f"「{t.name}」" not in tl_env, f"{t.name} 有专门条目不进清单"
        else:
            assert f"「{t.name}」" in tl_env, f"清单缺少 {t.name}"
    assert "参数：" in tl_env and "返回：" in tl_env, \
        "清单应为名称+简述+参数+返回值格式"
    # 无 env（纯聊天回合）：读取/查询类 rmgame 工具不注入，发现与启动常驻
    tl_none = tool_list_section(None)
    assert tl_none and "「discover_running」" in tl_none and "「start_game」" in tl_none, \
        "无 env 时 discover/start 应常驻"
    assert "「query_wiki」" not in tl_none and "「read_current_text」" not in tl_none, \
        "无 env 时读取/查询类 rmgame 工具不应注入"
    assert "「query_archive」" in tl_none and "「mora_notes」" in tl_none, "常开工具应保留"
    assert "say：text =" in sys_prompt and "update_state：affection_delta = 1" in sys_prompt, \
        "示例格式应统一"
    # 防御性精简：冗余说明不重复
    assert "如果状态没有变化且无需查询" not in sys_prompt, "赘余收尾句应移除"
    assert "不要当作工具之外的文本输出" not in sys_prompt, "注意行（与红线重复）应移除"
    # 对输出无贡献的程序元数据（语音色等）不进提示词（GUI 由 profile.ini 驱动）
    assert "语音色" not in sys_prompt and "<font" not in sys_prompt, \
        "语音色/标签是 UI 元数据，不应注入"
    # 好感等级契约化：不注入原卡 YAML 结构（behavioral_patterns 等）
    assert "behavioral_patterns" not in sys_prompt and "dialogue_examples" not in sys_prompt, \
        "好感等级段不应暴露 YAML 字段名"
    # 时间唯一锚点：【当前状态】不含时间（时间由消息组装层注入，见 context.py）
    assert "此刻是 20" not in sys_prompt, "【当前状态】不应注入时间（时间唯一锚点在消息组装层）"
    # GAME_RULES 去重：静态 system prompt（含 env）一律不含 GAME_RULES；
    # 全文只在游戏环境段（env_section 的【游戏点评规则】）注入一次
    assert "必须做到先查再答" not in sys_prompt, "无游戏环境不应注入 GAME_RULES"
    assert f"【{USER_REFERENCE}正在…】" not in sys_prompt, "无 env 不应注入环境段"
    prompt_env = build_system_prompt(card, state=st0, env={
        "game": "猎妻迷宫", "map_name": "Map001", "scene": "Scene_Map",
        "text": "欢迎来到小镇……这里很安全。",
        "matched_text": "欢迎来到小镇……这里很安全。",
        "event_context": "- [Map001.40.0] 场景标题\n- [Map001.40.13] 人群吵吵嚷嚷…"})
    assert f"【{USER_REFERENCE}正在…】" not in prompt_env, "环境段应移出 system prompt（动态尾部注入）"
    assert prompt_env == sys_prompt, \
        "system prompt 输出应与环境无关（GAME_RULES 已移入环境段）"

    # 环境段系列：CDP 精确文本 / OCR 噪声过滤 / 战斗 / 菜单 / 通配 / 摘要不进段 / 改名触发
    e1 = env_section({
        "game": "猎妻迷宫", "map_name": "Map001", "scene": "Scene_Map",
        "text": "欢迎来到小镇……这里很安全。",
        "matched_text": "欢迎来到小镇……这里很安全。",
        "event_context": "- [Map001.40.0] 场景标题\n- [Map001.40.13] 人群吵吵嚷嚷…"})
    assert e1 and f"【{USER_REFERENCE}正在…】" in e1, "有 env 应生成环境段"
    assert "<game_environment>" in e1 and "</game_environment>" in e1, \
        "游戏环境段应用 <game_environment> 包裹"
    assert f"{USER_REFERENCE}似乎正在玩《猎妻迷宫》" in e1, e1
    assert "游戏外的观察者与点评者" in e1, "环境段应声明角色不在游戏内（映射句）"
    assert "当前地图：Map001" in e1 and "当前场景：Scene_Map" in e1
    assert "当前对话：「欢迎来到小镇……这里很安全。」" in e1
    assert "事件前文（原文开头）" in e1, "环境段应保留原文开头作位置锚定"
    assert "Map001.40.13" in e1, "事件上下文应含条目引用"
    assert "query_wiki" in e1, "应提示可用工具"
    assert "当前事件摘要：" not in e1, "摘要（可能剧透）不应注入环境段"
    assert "affection_delta" not in e1 and "update_state" not in e1, \
        "环境段不应暴露代码字段名"
    e_noise = env_section({"game": "猎妻迷宫", "text": "A FEW UD BR HE ER 噪声",
                           "source": "ocr"})
    assert e_noise and "A FEW UD BR HE ER" not in e_noise, "不应暴露 OCR 噪声"
    assert "画面文字未能识别" in e_noise, "应标注未能识别"
    e_cdp = env_section({"game": "猎妻迷宫", "text": "人群吵吵嚷嚷地聚集起来了。"})
    assert e_cdp and "当前对话：「人群吵吵嚷嚷地聚集起来了。」" in e_cdp, \
        "CDP 精确文本应直接显示"
    e_battle = env_section({
        "game": "猎妻迷宫", "scene": "Scene_Battle", "source": "cdp",
        "battle_troop": "中级眷族Ａ、中级眷族Ｂ、中级眷族Ｃ", "battle_phase": "input",
        "party_info": "谢拉(2972/2972 MP:100/100 TP:3/100)", "actor_info": "谢拉",
        "actor_commands": "攻击、技能、防御、道具",
        "skill_list": "红莲地狱:15mp、狂龙气息:45mp",
        "skill_current": "狂龙气息：对敌方单体造成大量伤害"})
    assert "战斗信息：敌方部队：中级眷族Ａ、中级眷族Ｂ、中级眷族Ｃ，战斗阶段：input" in e_battle, e_battle
    assert f"{USER_REFERENCE}队伍成员：谢拉(2972/2972 MP:100/100 TP:3/100)" in e_battle, e_battle
    assert "当前行动者：谢拉" in e_battle and "可用行动：攻击、技能、防御、道具" in e_battle, e_battle
    assert "技能表：红莲地狱:15mp、狂龙气息:45mp" in e_battle, e_battle
    assert "当前选中技能：狂龙气息：对敌方单体造成大量伤害" in e_battle, e_battle
    assert "战斗信息" not in e1, "Scene_Map 不应显示战斗信息"
    e_menu = env_section({
        "game": "猎妻迷宫", "scene": "Scene_Menu", "source": "cdp",
        "party_info": "谢拉(2972/2972 MP:100/100 TP:0/100)",
        "menu_commands": "物品、技能、装备", "menu_current": "物品"})
    assert "菜单命令：物品、技能、装备" in e_menu, e_menu
    assert "当前选中：物品" in e_menu, e_menu
    assert f"{USER_REFERENCE}队伍成员：谢拉(2972/2972 MP:100/100 TP:0/100)" in e_menu, e_menu
    assert "战斗信息" not in e_menu, "菜单不应显示战斗信息"
    e_wild = env_section({
        "game": "猎妻迷宫", "scene": "Scene_Glossary", "source": "cdp",
        "list_current": "等级1", "help_text": "显示角色等级成长"})
    assert "当前界面选中：等级1" in e_wild, e_wild
    assert "帮助文本：显示角色等级成长" in e_wild, e_wild
    assert "菜单命令" not in e_wild, "自定义界面不应显示菜单命令"
    e_sum = env_section({
        "game": "猎妻迷宫", "matched_text": "x", "match_id": "Map001.40.13",
        "event_summary": "剧情概要：开场。", "event_context": "- [Map001.40.0] 场景标题"})
    assert "事件前文（原文开头）" in e_sum, e_sum
    assert "当前事件摘要：" not in e_sum, "摘要（可能剧透）不应注入环境段，应由 read_current_text 提供"
    assert "剧情概要：开场。" not in e_sum, "摘要内容不应注入环境段"
    assert "【已激活技能】" not in sys_prompt, "无技能激活不应注入技能段（game_context 未激活）"
    assert "<Fetish_analysys>" not in prompt_env, "游戏环境不应注入性癖分析技能"
    assert "kinsey" not in prompt_env.lower(), "游戏环境不应注入性癖分析理论"
    assert env_section({}) is None and env_section(None) is None, "空 env 不应生成环境段"
    # 改名触发链路：自动识别名（版本号/引擎后缀/文件残留）→ 环境段附改名引导
    assert _looks_auto_name("DemonsRoots1.1.1") and _looks_auto_name("村輪辱紀行ver1.0.1")
    assert _looks_auto_name("celesphonia_mv") and not _looks_auto_name("猎妻迷宫")
    assert not _looks_auto_name("幽世村") and not _looks_auto_name("")
    e_auto = env_section({"game": "demonsroots1-1-1", "game_name": "DemonsRoots1.1.1",
                          "map_name": "m", "scene": "s", "text": "x", "source": "cdp"})
    assert e_auto and "自动识别名" in e_auto and "rename_game" in e_auto, "自动识别名应附改名引导"
    e_clean = env_section({"game": "猎妻迷宫", "game_name": "猎妻迷宫",
                           "map_name": "m", "scene": "s", "text": "x", "source": "cdp"})
    assert e_clean and "自动识别名" not in e_clean, "规范名不应附改名引导"
    print("  提示词结构: 情境合并 ✓ | 激活指令 ✓ | 条件注入 ✓ | 动态段（状态/环境/推进）尾部注入 ✓")

    # 占位符实例化 + 指称：{{user}}/{{char}} 已替换，叙述层统一使用
    # USER_REFERENCE（setting/user.ini 的 ref，缺省「对方」）
    assert "{{user}}" not in sys_prompt and "{{char}}" not in sys_prompt, "占位符未实例化"
    assert USER_REFERENCE in sys_prompt, "应使用统一指称"
    assert "只有两个常驻对象" in sys_prompt, "常驻二方关系声明缺失"
    assert session._CHAR.identity_data["identity"]["title"] in sys_prompt, \
        "角色身份词应注入（identity.title 实例化）"
    assert "你身处于" not in sys_prompt, "场景不应以「你身处于」拼接（空间描写无前缀）"
    assert "桌面宠物" not in sys_prompt and "悬浮" not in sys_prompt, \
        "情境段不应声明桌宠形态（展示层由 GUI 承担）"
    print(f"  占位符实例化: {{user}}/{{char}} → 指称[{USER_REFERENCE}] ✓")

    # 指令层常量：激活指令独立于 system，由消息组装附加；引用式指向游戏点评规则
    assert session._CHAR.identity in activation_instruction(), "激活指令应含角色身份"
    assert "的身份，对" in activation_instruction(), "激活指令主体格式异常"
    assert "先查再答" in activation_instruction() and "游戏点评" in activation_instruction(), \
        "激活指令应引用【游戏点评规则】（引用式）"
    assert "必须先调用 query_wiki" not in activation_instruction(), "激活指令不应重复 GAME_RULES 全文"
    assert "不得自行脑补设定细节" not in activation_instruction(), "激活指令不应重复 GAME_RULES 全文"
    assert "必须先调用 query_wiki" not in SKILLS["game_context"].get("world_book", ""), \
        "world_book 应为引用句，不再全文带 GAME_RULES"
    assert "先查再答" in SKILLS["game_context"].get("world_book", ""), "world_book 应含先查再答语义"
    assert "必须先调用 query_wiki" in GAME_RULES and "不得自行脑补设定细节" in GAME_RULES
    assert "必须做到先查再答" not in sys_prompt, "system prompt 不应注入 GAME_RULES（静态协议）"
    assert "必须做到先查再答" not in prompt_env, "system prompt（含 env）不应注入 GAME_RULES（移入环境段）"
    e_rules = env_section({
        "game": "猎妻迷宫", "map_name": "Map001", "scene": "Scene_Map",
        "text": "喂，那是……", "source": "cdp"})
    assert e_rules and "【游戏点评规则】" in e_rules, "环境段应含【游戏点评规则】"
    assert e_rules.count("必须做到先查再答") == 1, "GAME_RULES 全文应只注入一次（环境段）"
    assert e_rules.count("必须先调用 query_wiki") == 1, "query_wiki 条款全文仅出现一次"
    print("  先查再答规则: GAME_RULES 单一来源（随环境段【游戏点评规则】唯一全文，协议/世界书/激活指令引用式）✓")
    print("  指令层: 激活指令独立（随消息附加，引用式指向游戏点评规则）✓")

    # 场景与开场契约：场景进 system prompt（【情境】段），台词进上下文流
    scene = session._CHAR.scenario_default
    opening = session._CHAR.opening
    assert "书架" in scene or "图书馆" in scene, f"场景提取异常: {scene[:50]}"
    assert len(opening) >= 3, f"应提取 3 条以上台词，实际 {len(opening)}"
    assert all(x.strip() for x in opening), "开场台词流应全为非空"
    assert "【开场白】" not in sys_prompt, "开场白段应已移除"
    assert opening[0][:10] not in sys_prompt, "开场白台词不应残留在 system prompt 中"
    print(f"  开场契约: 场景 {len(scene)} 字符 | 台词 {len(opening)} 条 ✓")

    # 技能激活后注入：半动态层（mid_static_sections）包含 Fetish 分析内容，
    # 静态 system prompt 不含（保持字节稳定）
    st_skill = {"affection": 20, "inner_thought": "x", "skills": ["fetish_analysis"]}
    sys_prompt_skill = build_system_prompt(card, state=st_skill)
    assert "<Fetish_analysys>" not in sys_prompt_skill, \
        "技能 world book 应移出静态段（半动态层 mid_static_sections）"
    mid_skill = "\n".join(mid_static_sections(st_skill))
    assert "<Fetish_analysys>" in mid_skill, "技能激活后 mid_static 应注入分析内容"
    assert "【已激活技能】" in mid_skill, "缺少技能段"
    # 心理 COT：注入激活指令（build_activation），<thinking_format> 包裹，
    # 仅 fetish_analysis 激活；不依赖原生思考模式（reasoning）
    act_skill = build_activation(st_skill, card)
    assert act_skill.startswith("内容模式:NSFW\n现在，请以" + session._CHAR.identity), act_skill
    assert act_skill.rstrip().endswith("</thinking_format>"), "心理COT 应追加在激活指令末尾"
    cot_title, cot_body = llm_card._psych_cot(card)
    assert cot_title == "心理COT", cot_title
    assert "<thinking_format>" in act_skill and "在思考阶段按以下步骤完成心理分析推理" in act_skill, \
        "心理COT 正文应注入激活指令（标题不拼接，见 build_activation）"
    assert "list 3 possible psychological" in act_skill, "心理COT 应含原型步骤4"
    assert "Hypothesis Development" in act_skill, "心理COT 应含原型步骤5"
    assert "{{random" not in act_skill and "{{roll" not in act_skill, "占位符应已实例化"
    assert act_skill.count("<thinking_format>") == 1, act_skill
    assert act_skill.count("</thinking_format>") == 1, act_skill
    assert "<think>" not in act_skill, "不应使用 DeepSeek 思考输出标记包裹模板"
    assert "素材来源" in act_skill and "read_current_text" in act_skill, "应含素材获取引导"
    assert "收敛判断" in act_skill and "关闭性癖测试技能" in act_skill, "应含收敛/关闭引导"
    assert "0. 检查上回的堕落进度" in act_skill, "原型步骤应原样保留"
    assert "<thinking_format>" not in build_activation(st0, card), "默认激活指令不含心理COT"
    assert "<think>" not in sys_prompt_skill and "<thinking_format>" not in sys_prompt_skill, \
        "COT 已移出 system prompt"
    # 思维链风格指令：激活 fetish_analysis → 思维模式要求（推理式）；否则角色沉浸要求
    assert thinking_style_for(st0) == THINKING_STYLE_INSTRUCT["immersion"]
    assert "【角色沉浸要求】" in thinking_style_for(st0)
    assert thinking_style_for(st_skill) == THINKING_STYLE_INSTRUCT["logical"]
    assert "【思维模式要求】" in thinking_style_for(st_skill)
    assert "<think>标签内" not in THINKING_STYLE_INSTRUCT["immersion"] \
        and "<think>标签内" not in THINKING_STYLE_INSTRUCT["logical"], \
        "风格指令应指向 think 工具，而非原生 <think> 思考块"
    assert "think 工具记录" in THINKING_STYLE_INSTRUCT["immersion"] \
        and "think 工具记录" in THINKING_STYLE_INSTRUCT["logical"], \
        "风格指令应指向 think 工具记录的内容"
    imm = THINKING_STYLE_INSTRUCT["immersion"]
    assert "用括号包裹内心活动" not in imm, "沉浸式不应要求括号包裹（think 工具契约冲突）"
    assert "不要用（心想：…）" in imm, "沉浸式应明确禁止括号包裹"
    # 思考层不得先行委婉降级（防 think→say 双重收敛：实测原文「阳物」→think
    # 「巨物」→say「大东西」）；两种风格都须带「与所读文本同尺度」规则
    assert "不得在思考里先行委婉降级" in imm and "直白词照用" in imm, \
        "沉浸式思考应禁止先行委婉降级"
    assert "直白词照用" in THINKING_STYLE_INSTRUCT["logical"] \
        and "不得在思考里先行委婉降级" in THINKING_STYLE_INSTRUCT["logical"], \
        "推理式思考应禁止先行委婉降级"
    # 解耦验证：推理相关内容不依赖 setting/llm.ini 的 reasoning
    sp_any = build_system_prompt(card, state=st_skill)
    assert "fetish_analysis" in sp_any, "技能字段不依赖原生思考模式"
    assert "【已激活技能】" not in sp_any, "技能段应只在 mid_static 注入"
    assert "【已激活技能】" in "\n".join(mid_static_sections(st_skill))

    # 内容模式与提示词交互：红线行在【输出红线】块内（以「R18内容」尺度语承载，
    # 不带「内容模式:NSFW」前缀）、激活指令首行裸标记
    assert "【输出红线】" in sys_prompt and "不允许回避R18内容" in sys_prompt, \
        "内容模式应并入静态 system prompt 的【输出红线】块"
    assert sys_prompt.index("【输出红线】") < sys_prompt.index("不允许回避R18内容"), \
        "内容模式行应在【输出红线】块内"
    act_default = build_activation(st0, card)
    assert act_default.startswith("内容模式:NSFW\n"), act_default
    assert act_default.count("内容模式:NSFW") == 1, act_default
    _saved_mode = content_mode.CONTENT_MODE
    try:
        content_mode.CONTENT_MODE = "sfw"
        assert "<thinking_format>" not in build_activation(st_skill, card), \
            "SFW 下不应注入心理COT"
        sp_sfw = build_system_prompt(card, state=st_skill)
        assert "不允许输出R18内容" in sp_sfw and "不允许回避R18内容" not in sp_sfw, sp_sfw
        assert "「fetish_analysis」" not in sp_sfw, "SFW 下技能清单不应列出性癖分析技能"
        act_sfw = build_activation(st0, card)
        assert act_sfw.startswith("内容模式:SFW\n"), act_sfw
    finally:
        content_mode.CONTENT_MODE = _saved_mode
    # 恢复后 NSFW 链路可用（防测试污染）
    assert "「fetish_analysis」" in build_system_prompt(card, state=st0)

    # 动态尾部段（移出 system prompt）：状态/环境/工具清单/推进由独立函数生成
    ts1 = turn_section((1, 4))
    assert ts1 and "【本回合推进】" in ts1 and "最多可自主推进 4 轮" in ts1, ts1
    assert "每轮最多调用一个查询工具" in ts1, "推进段应声明查询限一"
    ts_mid = turn_section((3, 4))
    assert "已进行 2 轮" in ts_mid, ts_mid
    ts_end = turn_section((4, 4))
    assert "第 4/4 轮（上限）" in ts_end and "不要再调用其他工具" in ts_end, ts_end
    assert turn_section(None) is None
    assert "【本回合推进】" not in sys_prompt, "推进段应移出 system prompt（动态尾部注入）"

    # ---- 提示词篇幅预算（约定 7 机器可执行形态）：锁定量化基线防回潮 ----
    budget_rows = prompt_budget(st0, card)
    assert budget_rows, "预算表不应为空"
    for r in budget_rows:
        assert not r["over"], \
            f"提示词段超预算: {r['section']} {r['chars']} 字符 > 预算 {r['budget']}"
    fw_chars = sum(r["chars"] for r in budget_rows
                   if r["section"] in ("【记忆与回忆】", "【每回合的行为方式】"))
    assert fw_chars <= framework_static_budget(), \
        f"框架静态段超总预算: {fw_chars} > {framework_static_budget()}"
    # 技能激活态（心理COT 注入激活指令、技能世界书注入半动态段）同样不超预算
    for r in prompt_budget(st_skill, card):
        assert not r["over"], \
            f"提示词段超预算（技能激活态）: {r['section']} {r['chars']} > {r['budget']}"
    # 内容模式两处投放：红线行（「R18内容」尺度语）+ 激活指令首行裸标记；
    # 红线行不带「内容模式:」前缀，裸标记唯一在激活指令（见约定 7）
    fw = _framework_text(st0, card)
    assert fw.count("不允许回避R18内容") == 1, "NSFW 红线行应恰一处"
    assert fw.count("内容模式:") == 1, "内容模式裸标记应只在激活指令首行"
    print("[llm_prompt.selftest] 通过 ✓ 提示词结构 / 环境段 / GAME_RULES 单一来源 / 技能与 COT / 内容模式联动 / 动态段 / 篇幅预算")


if __name__ == "__main__":
    selftest()
