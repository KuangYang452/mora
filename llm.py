# -*- coding: utf-8 -*-
"""LLM 客户端与语义化提示词 —— 桌宠角色

架构约定：
- 运行配置统一在 setting/*.ini（settings 模块，唯一配置入口）；角色数据在
  character/<slug>/（character 包）；本模块只读不改数据文件。
- 交付给 LLM 的提示词完全语义化：状态用自然语言叙述，不暴露代码结构。
- LLM 一切输出都通过原生工具调用（function calling）完成：台词走 say 工具、
  状态走 update_state 工具、查询走 rmgame/归档/笔记工具，由本模块解析；
  旧文本 JSON 路径仅作解析容错兜底（模型未走工具时降级处理）。
- LLM 对数据的调整权限低于硬编码：好感度只能给出 delta（趋势），
  最终值由 data.apply_delta 结算、等级由角色 profile 映射；身份类锚点不可修改。
- 对话历史由 pet.py 持有，经 persist.py 落盘（runtime/pet_session.json）。
"""

import json
import re
import threading
from pathlib import Path

try:
    import requests
except ImportError:  # 允许在无 requests 环境下做离线自测
    requests = None

import logutil  # noqa: E402  （通用 LLM 调用日志，无循环依赖）
import settings
import character as character_mod
from data import (GAME_RULES, SCENE, SKILLS, apply_delta)
# 纯文本清洗（从本模块拆出，避免 context → llm 顶层依赖，见 textutil 模块注释）
from textutil import strip_paren_annotations, strip_time_prefix
# 工具注册表（schema/清单/分类的唯一来源，见 tools 模块）
import tools

# 运行配置 / 用户指称 / 角色快照：进程启动时从唯一配置入口读取一次，运行期不变。
# - CONFIG：setting/app.ini（原 data.CONFIG 迁移，键名完全兼容）
# - USER_REFERENCE：setting/user.ini 的 ref（{{user}} 实例化值）
# - _CHAR：character/ 包当前激活角色（card.json + profile.ini，含初始状态/好感等级/
#   自称/称呼/身份/提示词片段），角色专属数据统一从这里取。
CONFIG = settings.app_config()
USER_REFERENCE = settings.user_ref()
_CHAR = character_mod.current()
INITIAL_STATE = _CHAR.initial_state()
level_for = _CHAR.level_for

# 全局串行锁：所有 LLM 调用（回合/合并/摘要/wiki 重写）排队执行，
# 任意时刻只有一个请求在途，后续调用阻塞等待（避免并发调用与上下文交错）。
_LLM_CALL_LOCK = threading.Lock()

# tool_choice="required" 的端点支持探测缓存：
# None=未探测；False=端点不支持（已 4xx 回退 auto，不再尝试）。
_TOOL_CHOICE_REQUIRED_OK = None


def load_llm_config() -> dict:
    """LLM 配置的唯一生效位置：setting/llm.ini（settings 模块）。

    不提供任何代码内兜底：文件缺失、格式损坏、必填字段（base_url / api_key /
    model）缺失时直接抛出 ChatError，避免「写了却不生效」的歧义。
    temperature / max_tokens / reasoning / reasoning_effort 缺失时使用默认值。
    """
    try:
        return settings.llm_config()
    except settings.ConfigError as exc:
        raise ChatError(str(exc))

# 原生思考模式（setting/llm.ini 的 reasoning → API thinking.type）只控制 DeepSeek
# 是否返回隐藏推理块，与推理内容解耦：工具循环内的推理走 think 工具通道
# （think 是通用工具，始终注册、不随 reasoning 变化），思维链风格指令 / 心理COT /
# fetish_analysis 技能因此不依赖原生思考模式，reasoning 开闭都不影响它们。

# ---------------------------------------------------------------------------
# 角色卡解析
# ---------------------------------------------------------------------------


def get_character() -> dict:
    """返回当前激活角色的角色卡（character/<slug>/card.json）。

    prototype/ 下的角色卡 JSON 仅作为参照原型，程序不直接依赖；
    运行时只读取由 freeze_prototype.py 生成、character 包加载的角色数据。
    返回深拷贝，防止调用方意外修改共享数据。
    """
    import copy
    return copy.deepcopy(character_mod.current().card)


def _clean(text: str) -> str:
    """去掉 SillyTavern 的 <font> 染色标签，保留文字内容。"""
    if not text:
        return ""
    return re.sub(r"<font[^>]*>", "", text).replace("</font>", "").strip()


_STATUS_BLOCK_RE = re.compile(r"<StatusBlock>.*?</StatusBlock>", re.S | re.I)


def _strip_status_block(text: str) -> str:
    """剥离 <StatusBlock> 通道块（STscript 变量示例，非语义内容）。

    状态管理由硬编码变量管理器承担（data.py + apply_state），此类块
    残留在提示词中会与工具调用协议冲突，统一剔除。
    """
    if not text:
        return ""
    return _STATUS_BLOCK_RE.sub("", text).strip()


_SPEECH_RE = re.compile(r"<font[^>]*>(.*?)</font>", re.S | re.I)
_QUOTE_RE = re.compile(r'^[\u201c"]+|[\u201d"]+$')


def extract_opening(card: dict):
    """把角色卡 first_mes 拆解为（场景描写, 开场台词列表）。

    开场白不再整体注入 system prompt，而是拆成两部分：
    - 场景描写：first_mes 去掉台词块后的剩余文本。静态、可替换（data.SCENE
      覆盖），进入 system prompt 的【场景】段。
    - 开场台词：<font> 包裹的角色发言。作为上下文文本流的起始（assistant
      消息）交给 ContextManager，随历史一起被窗口裁剪。
    """
    raw = _strip_status_block(card.get("first_mes") or "")
    lines = []
    for m in _SPEECH_RE.finditer(raw):
        line = _QUOTE_RE.sub("", m.group(1)).strip()
        if line:
            lines.append(line)
    scene = _SPEECH_RE.sub("", raw)          # 先剥台词块
    scene = _clean(scene)                    # 再清残余标签
    scene = re.sub(r"\n{3,}", "\n\n", scene).strip()
    return scene, lines


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
    if CONFIG.get("rmgame_enabled", True):
        text += (
            "对方正在玩 RPG Maker 游戏时，回答前先按随游戏环境段注入的"
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
        "【思维模式要求】在你的思考（think 工具记录）中，请遵守以下规则：\n"
        "1. 禁止使用圆括号包裹内心独白，例如\"（心想：……）\"或\"(内心OS：……)\"，"
        "所有分析内容直接陈述即可\n"
        "2. 禁止以角色第一人称描写内心活动，例如\"我心想\"\"我觉得\"\"我暗自\"等，"
        "请用分析性语言替代\n"
        "3. 思考内容应聚焦于心理分析：分析对方的言行与性癖线索、调用心理学与"
        "性学理论、形成可检验的假设、规划追问与引导方式，不要在思考中进行"
        "角色扮演式的内心戏表演"
    ),
    "immersion": (
        "【角色沉浸要求】在你的思考（think 工具记录）中，请遵守以下规则：\n"
        "1. 以角色第一人称直接陈述内心想法与感受，"
        "2. 用第一人称描写角色的内心感受，例如\"我心想\"\"我觉得\"\"我暗自\"等\n"
        "3. 思考内容应沉浸在角色中："
        + (_CHAR.instantiate(_CHAR.immersion_perspective)
           if _CHAR.immersion_perspective
           else "以角色的视角观察对方")
        + "，通过内心独白分析对方的言行与心思，并规划如何回应"
    ),
}


def thinking_style_for(state: dict = None) -> str:
    """按技能状态选择思维链风格指令：激活 fetish_analysis → 推理式，否则角色沉浸式。

    返回的指令文本注入到第一条 user 消息末尾（build_messages 的 first_user_instr）。
    指令作用于 think 工具记录的内容（工具循环内的推理通道），与原生思考模式
    （setting/llm.ini 的 reasoning → thinking.type）无关，始终注入。
    """
    state = state or {}
    if "fetish_analysis" in (state.get("skills") or []):
        return THINKING_STYLE_INSTRUCT["logical"]
    return THINKING_STYLE_INSTRUCT["immersion"]


# 心理 COT 适配段：原型步骤（0-6）是纯循环分析引擎，缺收敛出口与素材来源引导。
# 在原型原文之后追加本场景规则（不改写原型正文）：
# - 素材确认：游戏环境中对方的言行常来自游戏内剧情，分析前先读游戏文本；
# - 收敛判断：信息足够时输出结论并关闭技能，避免无限追问。
COT_ADAPT = (
    "\n\n分析适配（本场景规则，适用于上述步骤）：\n"
    "· 素材来源（步骤 3~5 分析前）：若对方的言行缺乏上下文线索——"
    "尤其对方在玩游戏时，言行常来自游戏内剧情——先调用 read_current_text / "
    "query_wiki 获取游戏文本作为分析依据；区分对方本人的行为与游戏内角色/剧情，"
    "不要混为一谈。\n"
    "· 收敛判断（步骤 6 之后）：若已收集足够信息（对方已回答核心问题、关键假设已有"
    "支持或反驳证据、追问已达合理轮次），结束测试——得出明确分析结论（供台词交付），"
    "并在 update_state 中把 skills 设为空数组关闭性癖测试技能；"
    "仅当关键信息仍缺失时才继续追问。"
)


def build_activation(state: dict = None, card: dict = None) -> str:
    """完整激活指令 = activation_instruction() [+ 心理 COT（<thinking_format> 包裹，仅激活时）]。

    心理 COT 内容取自原型「心理COT」世界书条目（_psych_cot），占位符已实例化；
    原型步骤后追加 COT_ADAPT（素材来源 + 收敛判断），以 <thinking_format> 标签包裹，
    明确推理只通过 think 工具记录（think 是工具循环内的推理通道——DeepSeek 工具
    调用与原生思维链互斥，工具循环内无 reasoning_content 通道），
    不写入 say 台词或其他工具参数。
    心理 COT 不依赖原生思考模式（setting/llm.ini 的 reasoning）：think 工具通道
    始终可用，fetish_analysis 激活即注入，reasoning 开闭不影响。
    """
    act = activation_instruction()
    state = state or {}
    if "fetish_analysis" in (state.get("skills") or []) and card:
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


def _instantiate(text: str) -> str:
    """实例化原型中的 SillyTavern 占位符。

    - {{user}} / {{User}} → USER_REFERENCE（"对方"，普通人聊天对象）
    - {{char}} / {{Char}} → 角色名
    - {{random::A::B::C}} → 首个选项（确定性，避免提示词运行时随机）
    - {{roll:NdM[+K]}} → 确定性最小值 N+K（骰子掷点不确定，取最小同哲学）
    """
    text = re.sub(r"\{\{random::(.*?)\}\}", lambda m: m.group(1).split("::")[0],
                  text, flags=re.S)
    text = re.sub(r"\{\{roll:(\d+)d\d+(?:\+(\d+))?\}\}",
                  lambda m: str(int(m.group(1)) + int(m.group(2) or 0)), text)
    text = text.replace("{{user}}", USER_REFERENCE).replace("{{User}}", USER_REFERENCE)
    text = text.replace("{{char}}", _CHAR.display_name).replace("{{Char}}", _CHAR.display_name)
    text = text.replace("{{char_name}}", _CHAR.display_name)
    text = text.replace("{{identity}}", _CHAR.identity)
    text = text.replace("{{char_self}}", _CHAR.self_ref)
    text = text.replace("{{user_ref}}", _CHAR.user_ref)
    return text


def _psych_cot(card: dict):
    """提取原型角色卡「心理COT」世界书条目，返回 (标题, 正文) 元组；找不到 ("", "")。

    标题取条目 comment（去 🎁 装饰，跟随原型）；正文为 <thinking_format> 原文，
    占位符已实例化、外壳去除。调用方负责以 <thinking_format>（原型标签）包裹并追加。
    """
    for e in (card.get("character_book") or {}).get("entries") or []:
        comment = e.get("comment") or ""
        content = e.get("content") or ""
        if "心理COT" in comment or content.lstrip().startswith("<thinking_format>"):
            body = _instantiate(content)
            body = body.replace("<thinking_format>", "", 1)
            body = re.sub(r"</thinking_format>\s*$", "", body, flags=re.S).strip()
            title = comment.replace("🎁", "").strip() or "心理COT"
            return title, body
    return "", ""


def _categorize_book(card: dict):
    """把世界书条目按用途分类，不再无条件全量注入。

    返回 (风格列表, 好感等级映射, 技能列表)：
    - 风格：角色卡风格块（<Mesugaki> 等，原样注入，恒定层）
    - 好感等级：按等级名 → 条目内容（条件注入，仅当前一级）
    - 技能：性癖分析（条件注入，仅当 LLM 激活 fetish_analysis 技能时注入对应角色卡条目）
    通道指令条目（updata/getvar/setvar）整体剔除。
    """
    CHANNEL_MARKERS = ("<updata>", "{{" + "getvar", "setvar:")
    style, levels, skills = [], {}, []
    entries = (card.get("character_book") or {}).get("entries") or []
    for e in entries:
        keys = " ".join(e.get("keys") or [])
        content = e.get("content") or ""
        if any(m in content for m in CHANNEL_MARKERS):
            continue
        content = _strip_status_block(content)
        if not content:
            continue
        # 好感等级条目：keys 形如「好感等级:初遇」
        m = re.match(r"好感等级[:：]\s*(.+)", keys)
        if m:
            levels[m.group(1).strip()] = content
            continue
        # 性癖分析技能条目（Fetish 分析指导 + 理论）
        if ("性癖" in keys or "分析" in keys) or "<Fetish" in content[:200]:
            skills.append(content)
            continue
        # 风格条目（角色卡风格块：雌小鬼对话规则等，按关键词与标签识别）
        if "雌小鬼" in keys or "<Mesugaki>" in content[:200]:
            style.append(content)
    return style, levels, skills


def _adapt_style(text: str) -> str:
    """弱化 <Mesugaki> 中与工具调用协议冲突的叙述教条。

    原型教条"括号描述"会引导模型把旁白写进
    台词；这里把该小节改写为"通过工具字段表达"（inner_thought / emote），
    保留其余风格规则原文。
    """
    return re.sub(
        r"括号描述:.*?(?=\n\s*[^\s\-]|\Z)",
        "内心/神态表达：通过 inner_thought 与 emote 字段承载，不要写进台词",
        text, flags=re.S)


def _semantic_state(state: dict, with_affection: bool = True) -> str:
    """把状态数据转成语义化叙述，不暴露任何代码结构。

    with_affection=True：含好感度数值（工具结果回传用——结算反馈需展示
    数值变化）；False：隐去数值只留等级（system 状态段用——数值逐次变化、
    模型无法直接读写，等级才是行为依据；隐去后该段仅在跨等级时变化，
    尽量静态）。
    不含当前时间：时间锚点由 ContextManager.build_messages 在消息序列
    后部（激活指令之前）以独立【当前时间】消息注入（唯一来源），
    状态段不重复，避免双时间源。
    """
    aff = int(state.get("affection", INITIAL_STATE["affection"]))
    level = level_for(aff)
    thought = state.get("inner_thought") or INITIAL_STATE["inner_thought"]
    rel = (f"好感度 {aff}/100，处于「{level}」阶段"
           if with_affection else f"处于「{level}」阶段")
    return (
        f"此刻你与{USER_REFERENCE}的关系：{rel}。\n"
        f"你的内心想法：{thought}"
    )


def _env_section(env: dict) -> str or None:
    """游戏环境快照 → 语义化叙述（动态环境层）；无有效信息返回 None。

    env 由调用方（pet.py）传入 runtime/current.json 的新鲜快照；
    本模块不读写数据文件（架构约定）。

    环境段整体以 <game_environment> 包裹：这段内容全部属于"游戏世界"，
    与角色（桌宠）所处的现实/图书馆世界在结构上隔离。段首映射句显式
    建立三方同一性：对方=玩家、对方队伍中的角色=对方在游戏中的化身、
    游戏角色≠角色（角色不在游戏内），防止模型把游戏内叙述（如谢拉的
    视角/台词）内化为自身经历。映射句不指名任何具体游戏或角色，换任何
    游戏都成立（避免过拟合到《猎妻迷宫》/谢拉）。
    """
    game = (env or {}).get("game_name") or (env or {}).get("game") or ""
    if not game:
        return None
    lines = [
        f"对方似乎正在玩《{game}》。",
        "以下环境信息全部描述游戏世界内部的情况：其中出现的角色"
        "（含对方队伍成员）都是游戏内的人物——对方操控的那一个，"
        "是对方在游戏中的化身，游戏内位置即对方所在位置；其余是"
        "对方同行的游戏角色。它们都与你（角色）无关：你不在游戏内，"
        "只是游戏外的观察者与点评者。对方若拿游戏角色与你的性格或"
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
    # 对方队伍成员（任何场景都有值：探索/菜单/战斗）。
    # 用"对方队伍"而非"我方"：环境段是对方（玩家）的视角，避免模型把
    # "我方"解读为角色一方、把游戏角色当成与角色同侧的存在。
    pi = (env.get("party_info") or "").strip()
    if pi:
        lines.append(f"对方队伍成员：{pi}")
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
    lines.append("你可以用 query_wiki 查询该游戏的资料，或点评对方的游戏进度。")
    return ("【对方正在…】\n<game_environment>\n"
            + "\n".join(lines) + "\n</game_environment>")


# ---------------------------------------------------------------------------
# 动态尾部段（移出 system prompt，由 pet.py 经 build_messages(pre_time=)
# 在历史之后、时间锚点之前注入）：
# - 【当前状态】：内心想法高频变化（模型每轮更新）；
# - 【对方正在…】：游戏环境快照持续变化；
# - 【本回合推进】：轮次计数每轮必变。
# 三者若留在 system prompt 内，会在协议段与历史段之前切断缓存前缀
# （静态在前、动态在后），故整体后置。
# ---------------------------------------------------------------------------

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
    if CONFIG.get("rmgame_enabled", True):
        rules = "【游戏点评规则】（对方正在玩 RPG Maker 游戏时生效）\n" + GAME_RULES
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


def turn_section(agent_turn: tuple) -> str or None:
    """本回合推进节奏段（动态尾部注入，多轮工具循环中每轮重建时带上），
    引导模型按「规划 → 推进 → 交付」自发收敛——首轮给完整纪律与轮次
    预算，后续轮逐步收紧，末轮强制交付。agent_turn=None 返回 None。
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
                  "待验证要点，再将查询工具一次性批量调用（可在同一轮同时"
                  "调用多个）；每轮得出关键结论后更新 think，"
                  "基于结果组织台词后，信息足够立即调用 say 交付；"
                  "不要零散地一次只查一项，也不要为查而查。")
    else:
        rhythm = (f"本回合已进行 {turn - 1} 轮。若信息已足够，"
                  "请直接调用 say 说出台词交付；"
                  "仅当关键信息仍缺失时再调用查询工具。")
    return "【本回合推进】\n" + rhythm


def build_system_prompt(card: dict, state: dict = None, env: dict = None) -> str:
    """把角色卡原文 + 语义化状态组装为系统提示词（只含静态内容）。

    身份/性格/情境/记忆/好感等级规则/技能/行为协议。**输出与 env 无关**：
    env 参数仅作契约占位（游戏环境信息全部由 env_section 在动态尾部注入，
    game_context 的 world_book 与 GAME_RULES 全文随其【游戏点评规则】段），
    自测以 prompt_env == sys_prompt 断言保护——缓存前缀不随游戏启停变化。
    动态内容（【当前状态】/【对方正在…】/【本回合推进】）由 state_section /
    env_section / turn_section 生成、经调用方在消息序列后部注入（见
    ContextManager.build_messages 的 pre_time 参数）——避免在 system prompt
    内出现高频变化文本而切断缓存前缀。
    原则：
    - 角色卡怎么写就怎么用——不删减、不改写、不做内容过滤。
    - 状态只以语义化叙述出现（_semantic_state），LLM 看不到任何结构。
    - 输出协议：LLM 一切输出走原生工具调用（function calling），
      字段权限在提示词中语义化说明。
    - 技能注入不依赖原生思考模式（setting/llm.ini 的 reasoning）：推理通道是
      think 工具（始终可用），fetish_analysis 在激活时照常注入。
    """
    state = state or dict(INITIAL_STATE)
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
        situation = "这里是角色与对方聊天的空间，周围的环境与摆设由角色的设定决定。"
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
    style, levels, skills = _categorize_book(card)
    if style and CONFIG.get("mesugaki_style_block", False):
        # <Mesugaki> 风格块（2024 年旧模块，默认关闭）：当时 LLM 可能未内化
        # mesugaki 语言风格故单独注入；现模型已内化，靠人设段即可。
        # 启用时原样注入，但剥离与工具调用协议冲突的叙述教条（_adapt_style）。
        parts.append("【风格】\n" + "\n\n".join(_adapt_style(s) for s in style))
    aff = int(state.get("affection", INITIAL_STATE["affection"]))
    cur_level = level_for(aff)
    # 好感等级：契约字段优先（identity.json#affection_levels，自然语言重写版，
    # 无 YAML 结构），回退世界书条目（原卡 YAML 风格，仅当前一级）。
    level_text = None
    idt_levels = (_CHAR.identity_data or {}).get("affection_levels") or {}
    if isinstance(idt_levels, dict) and (idt_levels.get(cur_level) or "").strip():
        level_text = idt_levels[cur_level].strip()
    elif cur_level in levels:
        level_text = levels[cur_level]
    if level_text:
        parts.append("【好感等级规则】\n" + level_text)
    active_skills = list(state.get("skills") or [])
    # 技能段注入：仅 fetish_analysis 的角色卡世界书（<Fetish_analysys>）
    # 进入静态 system prompt（按 state.skills 精确注入，不整表拖带）。
    # game_context 不在此注入：其 world_book 与 GAME_RULES 全文随游戏环境段
    # （env_section 的【游戏点评规则】）注入，使静态 system prompt 输出与
    # env 无关、不随游戏启停出现/消失（缓存前缀稳定）。
    if "fetish_analysis" in active_skills and skills:
        parts.append("【已激活技能】\n" + "\n\n".join(skills))

    # 行为协议：台词与状态更新都走原生工具调用（function calling）。
    # 激活指令由 ContextManager 作为对话上下文之后的独立 system 消息附加。
    # 工具清单与 function schema 同源（唯一来源 tools.SPECS）：rmgame 工具按
    # CONFIG["rmgame_enabled"] 过滤（schema 未注册的工具不得出现在提示词里，
    # 避免模型调用得到"未知工具"）。清单为「名称+简述+参数+返回值」，
    # 由 SPECS 派生渲染（与 API 注册的 schema 同源，不会双源不一致）；
    # say/update_state/think 已有专门行为条目不重复列出。
    listed_tools = tools.specs(rmgame_enabled=CONFIG.get("rmgame_enabled", True))
    listed_skills = SKILLS
    if not CONFIG.get("rmgame_enabled", True):
        listed_skills = {k: v for k, v in SKILLS.items() if k != "game_context"}
    skills_desc = "、".join(
        f"「{k}」({v['desc']}；{v['trigger_hint']})" for k, v in listed_skills.items())
    tool_lines = [t.prompt_entry() for t in listed_tools
                  if t.name not in ("say", "update_state", "think")]
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
        "（查询结果只保留最近一轮、更早的结果不再保留，需跨轮引用的结论必须经 "
        "think 保留）。你的完整思路链会以思考块呈现在对话中：块首是思考标记，"
        "块内按轮次累积 think 文本，工具调用以单行反引号追加在链内"
        "（如 `query_archive:关键词`）；块保持开放、不闭合——思考是否结束"
        "由你调用 say 工具交付来体现。think 仅本回合内可见、回合结束后不保留；"
        "区别于 update_state 的 inner_thought——那是持久状态，会保留到后续回合；"
        "长期记忆用 mora_notes。\n"
        "- 工具结果：查询结果以 tool 消息回传，只保留最近一轮的工具调用与结果；"
        "不要在结果可见前凭猜测作答。\n"
        "- 其他工具（调用即执行并返回结果）：\n"
        + "\n".join("  · " + line for line in tool_lines) + "\n"
        "- 对话边界：对话中只有两个常驻对象：你（"
        f"{_CHAR.display_name}）与对方（你称他「{_CHAR.user_ref}」）。"
        "对话或游戏里出现的其他人名/角色名都是对方讲述的故事或游戏中的角色，"
        "不是对话参与者——不要把它们当作你的对话对象。\n"
    )
    # 【游戏点评】GAME_RULES 不在协议段注入：全文随游戏环境段
    # （env_section 的【游戏点评规则】）注入，使协议段完全静态、
    # 不随游戏启停变化（缓存前缀稳定）。激活指令与技能 world_book
    # 只引用该段，不重复规则全文（单一事实来源 data.GAME_RULES）。
    proto += (
        "- 环境缺失时的行为：提示词中没有游戏环境信息时，说明你不清楚对方此刻"
        "在做什么——不要凭空猜测，直接说明暂时看不到"
        + ("（可调用 read_current_text 等工具确认）。\n"
           if CONFIG.get("rmgame_enabled", True) else "。\n")
        + ("- 启动游戏：对方请求启动/打开某个 RPG Maker 游戏时（即使游戏当前未运行），"
           "调用 start_game 启动（game 用游戏库中的名称或 slug，可用 discover_running "
           "查看库中游戏）；启动成功后即可用 read_current_text 读取其文本。\n"
           if CONFIG.get("rmgame_enabled", True) else "")
        + "响应示例（需要说话并更新状态时，参考以下并列结构）：\n"
        "同时调用 say 与 update_state 两个工具：\n"
        'say：text = "哦～？终于想通要来找我聊聊了吗？那就让我听听你的故事吧～"\n'
        'update_state：affection_delta = 1、inner_thought = "他总算愿意开口了"、'
        'emote = "得意"、bounce = true、skills = []' + "\n"
        "本回合内可连续多次调用工具（查询工具 / update_state / say）自主推进工作，"
        "查询工具与 update_state 会立即执行并返回结果，你可据此继续；"
        "调用 say 说出台词即结束本回合。\n"
        "【输出红线】\n"
        "- 台词必须通过 say 工具说出：不要直接输出文本，也不要在文本里写"
        "「say：xxx」之类的伪调用。\n"
        "- 工具参数只在调用时填写：不要把参数内容或旧 JSON 格式"
        "（如 {\"reply\": …}）当作文本输出。\n"
    )
    parts.append(proto)

    # 统一实例化原型占位符（{{user}}/{{char}}/{{random::}}/{{roll::}}）后返回。
    # 动态段（【当前状态】/【对方正在…】/【本回合推进】）已移出本函数，
    # 由 state_section / env_section / turn_section 生成、调用方在消息序列
    # 后部注入（缓存：静态在前、动态在后）；心理 COT 由 build_activation
    # 注入激活指令（<thinking_format> 包裹），思维链风格指令由 ContextManager
    # 注入第一条 user 消息（thinking_style_for，指向 think 工具通道）。
    return _instantiate("\n\n".join(parts))


# ---------------------------------------------------------------------------
# LLM 响应解析（JSON 容错）
# ---------------------------------------------------------------------------

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
    - 等级：由数值经 data.level_for 映射，LLM 无法直接指定。
    - skills：LLM 通过工具声明的技能开关（单独变量），程序只做白名单过滤；
      白名单不依赖原生思考模式（reasoning）：推理通道是 think 工具，始终可用，
      fetish_analysis 可正常激活。
    """
    new = dict(state)
    new["affection"] = apply_delta(int(state.get("affection", INITIAL_STATE["affection"])),
                                   parsed.affection_delta)
    if parsed.inner_thought:
        new["inner_thought"] = parsed.inner_thought[:100]
    if parsed.skills is not None:
        # 权限低于硬编码：只能激活 data.SKILLS 中定义的技能
        allowed = set(SKILLS)
        new["skills"] = [s for s in parsed.skills if s in allowed]
    return new


# ---------------------------------------------------------------------------
# LLM 客户端（OpenAI 兼容）
# ---------------------------------------------------------------------------

class ChatError(Exception):
    pass


class ChatClient:
    def __init__(self, llm_cfg: dict = None):
        llm_cfg = llm_cfg or load_llm_config()
        self.base_url = llm_cfg["base_url"].rstrip("/")
        self.api_key = llm_cfg["api_key"]
        self.model = llm_cfg["model"]
        self.temperature = float(llm_cfg.get("temperature", 0.95))
        self.max_tokens = int(llm_cfg.get("max_tokens", 1024))
        self.reasoning = bool(llm_cfg.get("reasoning", True))
        self.reasoning_effort = str(llm_cfg.get("reasoning_effort", "low"))

    @property
    def endpoint(self) -> str:
        base = self.base_url
        if not base.endswith("/v1"):
            base += "/v1"
        return base + "/chat/completions"

    def chat(self, messages: list, timeout: int = 180, tools: list = None,
             tool_choice: str = None) -> dict:
        """调用 OpenAI 兼容 API，返回完整响应 dict。

        tools 非空时携带原生 function calling 参数；tool_choice 覆盖默认
        tool_choice（None = 端点默认 auto）；模型可能同时输出 content 与
        tool_calls（say 台词 / update_state 状态 / rmgame 查询），
        由 parse_llm_response 解析。
        """
        global _TOOL_CHOICE_REQUIRED_OK
        if requests is None:
            raise ChatError("未安装 requests，无法调用 API：pip install requests")
        if not self.api_key:
            raise ChatError(
                "未配置 API Key。请在 setting/llm.ini 的 api_key 中填写。"
            )
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            # DeepSeek 思考模式开关：thinking.type 取 enabled/disabled
            # （顶层布尔 reasoning 不是 DeepSeek API 参数，会被静默忽略）
            "thinking": {"type": "enabled" if self.reasoning else "disabled"},
            "reasoning_effort": self.reasoning_effort,
            "stream": False,
        }
        if tools:
            if isinstance(tools, dict):
                tools = [tools]
            payload["tools"] = tools
            tc = tool_choice
            if tc == "required" and _TOOL_CHOICE_REQUIRED_OK is False:
                tc = "auto"          # 端点不支持 required（已探测），回退 auto
            payload["tool_choice"] = tc or "auto"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_err = None
        for attempt in range(2):
            try:
                resp = requests.post(self.endpoint, json=payload, headers=headers, timeout=timeout)
            except requests.exceptions.RequestException as exc:
                last_err = ChatError(f"网络请求失败: {exc}")
                continue
            if resp.status_code == 200:
                return resp.json()
            if (resp.status_code == 400 and payload.get("tool_choice") == "required"
                    and _TOOL_CHOICE_REQUIRED_OK is None):
                # 端点可能不支持 required：标记并降级 auto 重发一次（同请求内）
                _TOOL_CHOICE_REQUIRED_OK = False
                payload["tool_choice"] = "auto"
                try:
                    resp = requests.post(self.endpoint, json=payload,
                                         headers=headers, timeout=timeout)
                except requests.exceptions.RequestException as exc:
                    raise ChatError(f"网络请求失败: {exc}")
                if resp.status_code == 200:
                    return resp.json()
            if 400 <= resp.status_code < 500:
                raise ChatError(f"API 返回 {resp.status_code}: {resp.text[:300]}")
            last_err = ChatError(f"API 返回 {resp.status_code}: {resp.text[:300]}")
        raise last_err or ChatError("未知错误")


    def chat_with_retry(self, messages: list, timeout: int = 180,
                        tools: list = None, tool_choice: str = None) -> dict:
        """带自动重试的调用：响应既无台词也无工具调用时，追加修复指令重试一次。"""
        resp = self.chat(messages, timeout=timeout, tools=tools,
                         tool_choice=tool_choice)
        try:
            msg = resp["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ChatError(f"响应格式异常: {str(resp)[:300]}")
        if not (msg.get("content") or "").strip() and not msg.get("tool_calls"):
            retry_msgs = messages + [
                {"role": "user",
                 "content": "（你刚才没有给出任何回应。请调用 say 工具说出你的台词；"
                            "如需更新状态则调用 update_state 工具。）"}]
            resp = self.chat(retry_msgs, timeout=timeout, tools=tools,
                             tool_choice=tool_choice)
        return resp


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


def call_llm(messages: list, *, tools: list = None, kind: str = "round",
             retry: bool = False, timeout: int = 180,
             tool_choice: str = None,
             max_tokens: int = None, note: str = "",
             model: str = None, temperature: float = None,
             reasoning: bool = None) -> dict:
    """统一 LLM 调用入口 —— 所有 LLM 调用必须走此接口。

    - 配置默认唯一来源 setting/llm.ini（内部 ChatClient）；
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
        # 会话级默认：data.CONFIG["tool_choice"]（"auto" 保持旧行为）
        tool_choice = CONFIG.get("tool_choice", "auto")
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
    """全部工具 schema（唯一来源 tools.SPECS；rmgame 工具按开关过滤）。"""
    return tools.schema_list(rmgame_enabled=CONFIG.get("rmgame_enabled", True))


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
    # 避免技能残留到无关对话（性癖测试结束/离开话题时尤其需要）。
    if "fetish_analysis" in skills:
        text += (
            "\n提示：性癖测试技能（fetish_analysis）仍处于激活状态。"
            "测试结束或话题离开后，请在下次调用 update_state 时把 skills "
            "设为空数组以关闭它。"
        )
    return text


# ---------------------------------------------------------------------------
# 离线自测（无需 API Key）
# ---------------------------------------------------------------------------

def selftest() -> None:
    card = get_character()
    assert card.get("description", "").strip(), "角色卡描述读取失败"

    sys_prompt = build_system_prompt(card, state=dict(INITIAL_STATE))
    assert _CHAR.self_ref in sys_prompt, "提示词缺少自称"
    assert _CHAR.user_ref in sys_prompt, "提示词缺少角色对用户的称呼"
    # 状态段已移出 system prompt（动态尾部注入）：好感度数值隐去、
    # 只留等级；数值在工具结果回传里展示（_semantic_state 默认 with_affection）
    assert "好感度 20/100" not in sys_prompt, "好感度数值应从 system 状态段隐去"
    assert "【当前状态】" not in sys_prompt, "状态段应移出 system prompt（动态尾部注入）"
    st_seg = state_section(dict(INITIAL_STATE))
    assert "「初遇」" in st_seg and "好感度 20/100" not in st_seg, st_seg
    assert "此刻是 " not in sys_prompt, "时间唯一锚点在消息组装层（【当前状态】不应注入时间）"
    assert "affection_delta" in sys_prompt, "工具调用协议缺失"
    assert "update_state" in sys_prompt, "工具调用协议缺失"
    assert "调用 say 工具说出" in sys_prompt, "行为说明缺失（台词应走 say 工具）"
    assert "不得输出工具之外的文本" in sys_prompt, "强制工具通道约束缺失"
    assert _CHAR.display_name in sys_prompt, "提示词应含角色显示名"
    # 输出通道指令必须被剔除：SillyTavern 的 updata/getvar/setvar 由
    # 硬编码变量管理器替代，不得残留在提示词中与工具调用协议冲突
    for marker in ("<updata>", "{{getvar", "setvar:", "<StatusBlock>"):
        assert marker not in sys_prompt, f"提示词残留通道指令: {marker}"

    # 提示词结构：三个"情境"已合并；激活指令 + 条件注入
    for old in ("【场景】", "【当前情境】", "【你现在身处的情境】", "【对话规则】"):
        assert old not in sys_prompt, f"旧段名残留: {old}"
    for new in ("【情境】", "【好感等级规则】", "【记忆与回忆】"):
        assert new in sys_prompt, f"缺少新段: {new}"
    # 记忆分层认知：三层框架 + 主动检索引导（落在【记忆与回忆】段内）
    mem_body = sys_prompt.split("【记忆与回忆】", 1)[1]
    mem_seg = mem_body.split("\n\n【", 1)[0]   # 按段落边界截取，段内【中期记忆】不截断
    for _kw in ("短期记忆", "中期记忆", "长期记忆", "query_archive"):
        assert _kw in mem_seg, f"记忆段缺少关键词: {_kw}"
    assert "不要凭空编造" in mem_seg, "记忆段应含不编造约束"
    assert "今天想不想研究点新性癖呀" not in sys_prompt, "输出示例应移出 system"
    assert "skills" in sys_prompt, "技能工具字段缺失"
    # 好感等级仅注入当前一级（初遇），其他等级条目不注入；契约版为
    # 自然语言叙述（identity.json#affection_levels 或 profile 好感等级）
    level_seg = sys_prompt.split("【好感等级规则】")[1].split("【")[0]
    assert level_seg.strip(), "好感等级段为空"
    assert "恋慕" not in level_seg and "亲爱" not in level_seg, "不应注入其他等级"
    # 技能默认不注入（LLM 未激活）
    assert "<Fetish_analysys>" not in sys_prompt, "技能默认不应注入"
    assert "kinsey" not in sys_prompt.lower(), "理论条目默认不应注入"
    # <Mesugaki> 风格块默认关闭（CONFIG["mesugaki_style_block"]，旧模块）：
    # LLM 已内化 mesugaki 语言风格，靠人设段即可；开启时原样注入并改写叙述教条
    assert "<Mesugaki>" not in sys_prompt, "mesugaki 风格块应默认关闭"
    assert "【风格】" not in sys_prompt, "风格块关闭时不应有【风格】段"
    _saved_ms = CONFIG.get("mesugaki_style_block", False)
    try:
        CONFIG["mesugaki_style_block"] = True
        sp_ms = build_system_prompt(card, state=dict(INITIAL_STATE))
        assert "<Mesugaki>" in sp_ms and "【风格】" in sp_ms, "开关打开应注入风格块"
        # 风格叙述教条已剥离：括号描述 → 引导到工具字段（inner_thought / emote）
        assert "括号描述" not in sp_ms, "叙述教条应已剥离"
        assert "内心/神态表达：通过 inner_thought 与 emote 字段承载" in sp_ms, "教条改写缺失"
    finally:
        CONFIG["mesugaki_style_block"] = _saved_ms
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
    assert "只保留最近一轮" in sys_prompt and "更早的结果不再保留" in sys_prompt, \
        "think 引导应说明结果过期机制（跨轮引用必须 think 保留）"
    assert "先写下计划" in sys_prompt, "think 引导应有主动规划用法（先计划→逐轮更新结论）"
    assert "思考块" in sys_prompt and "反引号" in sys_prompt, \
        "行为协议应说明多轮循环的思考块呈现（think 文本累积 + 反引号工具调用）"
    assert "不闭合" in sys_prompt, "思考块应说明保持开放（增量式不闭合，防误解为思考已结束）"
    # 工具结果通道：结果以 tool 消息回传、只保留最近一轮，行为协议应有语义说明
    assert "以 tool 消息回传" in sys_prompt, "行为协议应说明工具结果经 tool 消息回传"
    assert "只保留最近一轮的工具调用与结果" in sys_prompt, \
        "行为协议应说明工具结果只保留最近一轮"
    assert "凭猜测作答" in sys_prompt, "工具结果语义应禁止结果可见前猜测作答"
    # 输出红线：禁令 + 替代（不展示完整错误范例，避免反向污染/重教旧协议）
    assert "【输出红线】" in sys_prompt, "输出红线缺失"
    assert "不要直接输出文本" in sys_prompt and "伪调用" in sys_prompt, "红线缺直出/伪调用禁令"
    assert "不要把参数内容或旧 JSON 格式" in sys_prompt, "红线缺参数/旧 JSON 禁令"
    # 工具清单（其他工具）：名称+简述+参数+返回值，由 tools.SPECS 派生
    # （单一来源，与 function schema 同源）；say/update_state/think 已有专门
    # 行为条目不重复列出；示例格式统一（工具名：参数）
    for t in tools.specs(rmgame_enabled=CONFIG.get("rmgame_enabled", True)):
        if t.name in ("say", "update_state", "think"):
            assert f"「{t.name}」" not in sys_prompt, f"{t.name} 有专门条目不进清单"
        else:
            assert f"「{t.name}」" in sys_prompt, f"清单缺少 {t.name}"
    assert "参数：" in sys_prompt and "返回：" in sys_prompt, \
        "清单应为名称+简述+参数+返回值格式"
    assert "say：text =" in sys_prompt and "update_state：affection_delta = 1" in sys_prompt, "示例格式应统一"
    # 防御性精简：冗余说明不重复（工具参数只在调用时填写 / 台词规范 只出现一次）
    assert "如果状态没有变化且无需查询" not in sys_prompt, "赘余收尾句应移除"
    assert "不要当作工具之外的文本输出" not in sys_prompt, "注意行（与红线重复）应移除"
    # 对输出无贡献的程序元数据（语音色等）不进提示词（GUI 由 profile.ini 驱动）
    assert "语音色" not in sys_prompt and "<font" not in sys_prompt, "语音色/标签是 UI 元数据，不应注入"
    # 好感等级契约化：契约字段优先（identity.json#affection_levels，自然语言），
    # 不注入原卡 YAML 结构（behavioral_patterns / dialogue_examples / 缩进列表）
    assert "behavioral_patterns" not in sys_prompt and "dialogue_examples" not in sys_prompt, \
        "好感等级段不应暴露 YAML 字段名"
    # 时间唯一锚点：【当前状态】不含时间（时间由消息组装层注入，见 context.py）
    assert "此刻是 20" not in sys_prompt, "【当前状态】不应注入时间（时间唯一锚点在消息组装层）"
    # GAME_RULES 去重：静态 system prompt（含 env）一律不含 GAME_RULES；
    # 全文只在游戏环境段（env_section 的【游戏点评规则】）注入一次，
    # 激活指令与技能 world_book 均为引用式。
    assert "必须做到先查再答" not in sys_prompt, "无游戏环境不应注入 GAME_RULES"
    # 环境层：已移出 system prompt（动态尾部注入，见 env_section）；
    # 无 env 时 system prompt 不含任何环境内容
    assert "【对方正在…】" not in sys_prompt, "无 env 不应注入环境段"
    prompt_env = build_system_prompt(card, state=dict(INITIAL_STATE), env={
        "game": "猎妻迷宫", "map_name": "Map001", "scene": "Scene_Map",
        "text": "欢迎来到小镇……这里很安全。",
        "matched_text": "欢迎来到小镇……这里很安全。",
        "event_context": "- [Map001.40.0] 场景标题\n- [Map001.40.13] 人群吵吵嚷嚷…"})
    assert "【对方正在…】" not in prompt_env, "环境段应移出 system prompt（动态尾部注入）"
    e1 = env_section({
        "game": "猎妻迷宫", "map_name": "Map001", "scene": "Scene_Map",
        "text": "欢迎来到小镇……这里很安全。",
        "matched_text": "欢迎来到小镇……这里很安全。",
        "event_context": "- [Map001.40.0] 场景标题\n- [Map001.40.13] 人群吵吵嚷嚷…"})
    assert e1 and "【对方正在…】" in e1, "有 env 应生成环境段"
    assert "<game_environment>" in e1 and "</game_environment>" in e1, \
        "游戏环境段应用 <game_environment> 包裹"
    assert "对方似乎正在玩《猎妻迷宫》" in e1, e1
    assert "游戏外的观察者与点评者" in e1, "环境段应声明角色不在游戏内（映射句）"
    assert "当前地图：Map001" in e1 and "当前场景：Scene_Map" in e1
    assert "当前对话：「欢迎来到小镇……这里很安全。」" in e1
    assert "事件前文（原文开头）" in e1, "环境段应保留原文开头作位置锚定"
    assert "Map001.40.13" in e1, "事件上下文应含条目引用"
    assert "query_wiki" in e1, "应提示可用工具"
    assert "当前事件摘要：" not in e1, "摘要（可能剧透）不应注入环境段"
    assert "affection_delta" not in e1 and "update_state" not in e1, \
        "环境段不应暴露代码字段名"
    # 匹配失败：不显示 OCR 噪声，标注未能识别（source=ocr 才走噪声过滤）
    e_noise = env_section({"game": "猎妻迷宫", "text": "A FEW UD BR HE ER 噪声",
                           "source": "ocr"})
    assert e_noise and "A FEW UD BR HE ER" not in e_noise, "不应暴露 OCR 噪声"
    assert "画面文字未能识别" in e_noise, "应标注未能识别"
    # CDP（source=cdp，默认）：text 是精确原文，直接显示
    e_cdp = env_section({"game": "猎妻迷宫", "text": "人群吵吵嚷嚷地聚集起来了。"})
    assert e_cdp and "当前对话：「人群吵吵嚷嚷地聚集起来了。」" in e_cdp, \
        "CDP 精确文本应直接显示"
    # 战斗场景：Scene_Battle 时展示敌方/阶段/玩家方/行动；非战斗不显示
    e_battle = env_section({
        "game": "猎妻迷宫", "scene": "Scene_Battle", "source": "cdp",
        "battle_troop": "中级眷族Ａ、中级眷族Ｂ、中级眷族Ｃ", "battle_phase": "input",
        "party_info": "谢拉(2972/2972 MP:100/100 TP:3/100)", "actor_info": "谢拉",
        "actor_commands": "攻击、技能、防御、道具",
        "skill_list": "红莲地狱:15mp、狂龙气息:45mp",
        "skill_current": "狂龙气息：对敌方单体造成大量伤害"})
    assert "战斗信息：敌方部队：中级眷族Ａ、中级眷族Ｂ、中级眷族Ｃ，战斗阶段：input" in e_battle, e_battle
    assert "对方队伍成员：谢拉(2972/2972 MP:100/100 TP:3/100)" in e_battle, e_battle
    assert "当前行动者：谢拉" in e_battle and "可用行动：攻击、技能、防御、道具" in e_battle, e_battle
    assert "技能表：红莲地狱:15mp、狂龙气息:45mp" in e_battle, e_battle
    assert "当前选中技能：狂龙气息：对敌方单体造成大量伤害" in e_battle, e_battle
    assert "战斗信息" not in e1, "Scene_Map 不应显示战斗信息"
    # 菜单界面：Scene_Menu 展示菜单命令/当前选中/对方队伍成员
    e_menu = env_section({
        "game": "猎妻迷宫", "scene": "Scene_Menu", "source": "cdp",
        "party_info": "谢拉(2972/2972 MP:100/100 TP:0/100)",
        "menu_commands": "物品、技能、装备", "menu_current": "物品"})
    assert "菜单命令：物品、技能、装备" in e_menu, e_menu
    assert "当前选中：物品" in e_menu, e_menu
    assert "对方队伍成员：谢拉(2972/2972 MP:100/100 TP:0/100)" in e_menu, e_menu
    assert "战斗信息" not in e_menu, "菜单不应显示战斗信息"
    # 通用通配：自定义界面（图鉴等）列表选中 + 帮助文本
    e_wild = env_section({
        "game": "猎妻迷宫", "scene": "Scene_Glossary", "source": "cdp",
        "list_current": "等级1", "help_text": "显示角色等级成长"})
    assert "当前界面选中：等级1" in e_wild, e_wild
    assert "帮助文本：显示角色等级成长" in e_wild, e_wild
    assert "菜单命令" not in e_wild, "自定义界面不应显示菜单命令"
    # 摘要（全文概述，可能剧透）不进环境段：即使有摘要，也只展示事件前文
    # 作位置锚定；摘要内容由 read_current_text 工具输出提供（rmgame.bridge._fmt_snapshot）
    e_sum = env_section({
        "game": "猎妻迷宫", "matched_text": "x", "match_id": "Map001.40.13",
        "event_summary": "剧情概要：开场。", "event_context": "- [Map001.40.0] 场景标题"})
    assert "事件前文（原文开头）" in e_sum, e_sum
    assert "当前事件摘要：" not in e_sum, "摘要（可能剧透）不应注入环境段，应由 read_current_text 提供"
    assert "剧情概要：开场。" not in e_sum, "摘要内容不应注入环境段"
    # 环境驱动技能：env 存在 → game_context 自动激活；其 world_book 与
    # GAME_RULES 全文随环境段（env_section）注入。静态 system prompt 输出
    # 与 env 无关（game_context 不再注入协议/技能段）——缓存前缀不随游戏
    # 启停变化。
    assert prompt_env == sys_prompt, \
        "system prompt 输出应与环境无关（GAME_RULES 已移入环境段）"
    assert "【已激活技能】" not in sys_prompt, "无技能激活不应注入技能段（game_context 未激活）"
    # 技能按名精确注入：游戏环境自动激活 game_context，不得拖带性癖分析技能
    assert "<Fetish_analysys>" not in prompt_env, "游戏环境不应注入性癖分析技能"
    assert "kinsey" not in prompt_env.lower(), "游戏环境不应注入性癖分析理论"
    prompt_env2 = build_system_prompt(card, state=dict(INITIAL_STATE), env={})
    assert "【对方正在…】" not in prompt_env2, "空 env 不注入"
    assert env_section({}) is None and env_section(None) is None, "空 env 不应生成环境段"
    # 改名触发链路：自动识别名（版本号/引擎后缀/文件残留）→ 环境段附改名引导；
    # 规范名不附（改完名提示自灭，链路闭环）
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

    # 占位符实例化 + 指称：{{user}}/{{char}} 已替换，统一使用「对方」
    assert "{{user}}" not in sys_prompt and "{{char}}" not in sys_prompt, "占位符未实例化"
    assert USER_REFERENCE in sys_prompt, "应使用统一指称"
    assert "只有两个常驻对象" in sys_prompt, "常驻二方关系声明缺失"
    assert _CHAR.identity_data["identity"]["title"] in sys_prompt, \
        "角色身份词应注入（identity.title 实例化）"
    # 场景为空间描写（舞台说明性质，{{char}} 指代），无桌宠形态声明
    assert "你身处于" not in sys_prompt, "场景不应以「你身处于」拼接（空间描写无前缀）"
    assert "桌面宠物" not in sys_prompt and "悬浮" not in sys_prompt, \
        "情境段不应声明桌宠形态（展示层由 GUI 承担）"
    print(f"  占位符实例化: {{user}}/{{char}} → 指称[{USER_REFERENCE}] ✓")

    # 指令层常量：激活指令独立于 system，由消息组装附加
    assert _CHAR.identity in activation_instruction(), "激活指令应含角色身份"
    assert "的身份，对" in activation_instruction(), "激活指令主体格式异常"
    # 激活指令引用式：指向游戏环境段的【游戏点评规则】，但不重复 GAME_RULES 全文
    assert "先查再答" in activation_instruction() and "游戏点评" in activation_instruction(), \
        "激活指令应引用【游戏点评规则】（引用式）"
    assert "必须先调用 query_wiki" not in activation_instruction(), "激活指令不应重复 GAME_RULES 全文"
    assert "不得自行脑补设定细节" not in activation_instruction(), "激活指令不应重复 GAME_RULES 全文"
    # 先查再答规则单一事实来源：GAME_RULES 一处定义（data.py），全文只随
    # 游戏环境段（env_section 的【游戏点评规则】）注入一次；行为协议、
    # world_book 与激活指令均为引用式——静态 system prompt 不含 GAME_RULES，
    # 不随游戏启停变化（缓存前缀稳定）
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
    # （identity.json 的 scenario.default / opening，见 character/SCHEMA.md）
    scene = _CHAR.scenario_default
    opening = _CHAR.opening
    assert "书架" in scene or "图书馆" in scene, f"场景提取异常: {scene[:50]}"
    assert len(opening) >= 3, f"应提取 3 条以上台词，实际 {len(opening)}"
    assert all(x.strip() for x in opening), "开场台词流应全为非空"
    assert "【开场白】" not in sys_prompt, "开场白段应已移除"
    assert opening[0][:10] not in sys_prompt, "开场白台词不应残留在 system prompt 中"
    print(f"  开场契约: 场景 {len(scene)} 字符 | 台词 {len(opening)} 条 ✓")

    # LLM 配置唯一生效位置：setting/llm.ini（无代码内兜底、无环境变量通道）
    import data as data_mod
    assert not hasattr(data_mod, "DEFAULT_LLM_CONFIG"), "data.py 不应再有 LLM 默认配置"
    assert not hasattr(data_mod, "resolve_api_key"), "环境变量通道应已移除"
    assert not hasattr(data_mod, "CONFIG"), "data.py 不应再有运行配置（已迁 setting/app.ini）"
    # 用临时 ini 验证配置加载（公开版无 setting/llm.ini；文件缺失 → 明确报错）
    import tempfile
    _td = tempfile.mkdtemp(prefix="llm_selftest_")
    _tmp_ini = Path(_td) / "llm.ini"
    _tmp_ini.write_text(
        "[llm]\nbase_url = https://example.com/v1\napi_key = k\nmodel = m\n"
        "temperature = 0.95\nmax_tokens = 1024\nreasoning = true\n"
        "reasoning_effort = low\n",
        encoding="utf-8")
    cfg = settings.llm_config(path=_tmp_ini)
    assert cfg["base_url"].startswith("http"), cfg
    assert cfg["model"], cfg
    assert 0 < float(cfg["temperature"]) <= 2, cfg
    # 文件缺失 → 明确报错，不静默兜底（settings.llm_config 支持 path 覆盖）
    try:
        settings.llm_config(path=Path(__file__).resolve().parent / "llm_config_不存在.ini")
        raise AssertionError("缺失配置文件应抛出 ConfigError")
    except settings.ConfigError as exc:
        assert "LLM 配置文件" in str(exc), exc
    print("  LLM 配置唯一来源: setting/llm.ini ✓（无兜底/无环境变量）")

    # 推理参数：配置读取→客户端传递一致（值跟随 setting/llm.ini，不锁具体值；
    # reasoning 可合法关闭、effort 可配 low/medium/high）；缺省值逻辑独立验证。
    assert isinstance(cfg["reasoning"], bool), cfg
    assert isinstance(cfg["reasoning_effort"], str) and cfg["reasoning_effort"], cfg
    client = ChatClient(llm_cfg=cfg)
    assert client.reasoning == cfg["reasoning"], "reasoning 应原样传递到客户端"
    assert client.reasoning_effort == cfg["reasoning_effort"], \
        "reasoning_effort 应原样传递到客户端"
    # 缺省值：reasoning 缺省 true、reasoning_effort 缺省 low（不依赖用户配置）
    import tempfile
    with tempfile.TemporaryDirectory() as _td:
        _tmp_ini = Path(_td) / "llm.ini"
        _tmp_ini.write_text(
            "[llm]\nbase_url = https://example.com/v1\napi_key = k\nmodel = m\n",
            encoding="utf-8")
        _dflt = settings.llm_config(path=_tmp_ini)
        assert _dflt["reasoning"] is True and _dflt["reasoning_effort"] == "low", _dflt
    assert extract_reasoning({"choices": [{"message": {"content": "x"}}]}) == ""
    assert extract_reasoning({"choices": [{"message": {"reasoning_content": "  思考中  "}}]}) == "思考中"
    assert extract_reasoning({"choices": [{"message": {"reasoning": "r"}}]}) == "r"
    assert extract_reasoning({}) == "" and extract_reasoning("bad") == ""
    # 缓存命中统计：主字段 / 兼容字段 / 缺失字段
    cs = _cache_stats({"usage": {"prompt_tokens": 100,
                                 "prompt_cache_hit_tokens": 30,
                                 "prompt_cache_miss_tokens": 70}})
    assert cs == "缓存命中: hit=30 miss=70 命中率=30.0% (总 100)", cs
    cs2 = _cache_stats({"usage": {"prompt_tokens": 100,
                                  "prompt_tokens_details": {"cached_tokens": 25}}})
    assert cs2 == "缓存命中: hit=25 miss=75 命中率=25.0% (总 100)", cs2
    assert _cache_stats({"usage": {"prompt_tokens": 50}}) is None, "无缓存字段不应记录"
    assert _cache_stats({}) is None and _cache_stats("bad") is None
    assert _cache_stats(None) is None
    # 输出清洗（时间标注/括号旁白）：实现与断言已随 textutil 模块迁移
    from textutil import selftest as textutil_selftest
    textutil_selftest()
    # 请求 payload 键（DeepSeek 文档）：thinking.type 控制思考模式开关，
    # 顶层布尔 reasoning 不是 API 参数；用 fake requests 捕获实际发送的 payload
    captured = {}

    class _FakePost:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "x"}}]}

    class _FakeRequests:
        @staticmethod
        def post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            return _FakePost()

    saved_requests = globals().get("requests")
    globals()["requests"] = _FakeRequests
    try:
        # 用构造配置独立验证 payload 映射（不依赖用户 setting/llm.ini 的具体值）：
        # reasoning=true→thinking.enabled、false→disabled、effort 原样透传
        ChatClient(llm_cfg={**cfg, "reasoning": True,
                            "reasoning_effort": "medium"}).chat(
            [{"role": "user", "content": "hi"}])
        p = captured["payload"]
        assert "reasoning" not in p, "顶层布尔 reasoning 不是 DeepSeek 参数，应删除"
        assert p["thinking"] == {"type": "enabled"}, p
        assert p["reasoning_effort"] == "medium", p
        ChatClient(llm_cfg={**cfg, "reasoning": False}).chat(
            [{"role": "user", "content": "hi"}])
        assert captured["payload"]["thinking"] == {"type": "disabled"}, \
            captured["payload"]
        ChatClient(llm_cfg={**cfg, "reasoning_effort": "low"}).chat(
            [{"role": "user", "content": "hi"}])
        assert captured["payload"]["reasoning_effort"] == "low", captured["payload"]
        # tool_choice：required 透传；auto 默认
        client.chat([{"role": "user", "content": "hi"}],
                    tools=[{"type": "function", "function": {"name": "say"}}],
                    tool_choice="required")
        assert captured["payload"]["tool_choice"] == "required", captured["payload"]
        client.chat([{"role": "user", "content": "hi"}],
                    tools=[{"type": "function", "function": {"name": "say"}}],
                    tool_choice="auto")
        assert captured["payload"]["tool_choice"] == "auto", captured["payload"]
    finally:
        if saved_requests is None:
            globals().pop("requests", None)
        else:
            globals()["requests"] = saved_requests
    print("  推理参数: payload thinking=enabled/disabled（按 reasoning 映射，无顶层 reasoning）✓ | "
          "effort 透传 ✓ | extract_reasoning ✓ | "
          "tool_choice=required/auto 透传 ✓")

    # function calling：工具 schema 定义（唯一来源 tools.SPECS）
    tool = tools.schema_list(True)[1]
    assert tool["type"] == "function"
    fn = tool["function"]
    assert fn["name"] == "update_state"
    props = fn["parameters"]["properties"]
    assert props["affection_delta"]["minimum"] == -5
    assert props["affection_delta"]["maximum"] == 5
    assert props["emote"]["enum"] == ["none", "害羞", "得意", "生气", "愉悦", "戏谑", "撒娇"]
    assert fn["parameters"]["required"] == ["affection_delta"]
    print("  工具 schema: update_state（delta 限幅 / emote 枚举 / skills 数组）✓")

    # 多工具 schema：rmgame 点评工具（开关 CONFIG["rmgame_enabled"]）+ 归档查询 + 私有笔记
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
    # 注册表与 function schema 一致（tools.SPECS 单一来源）：
    # emote / bounce 不是独立工具（它们是 update_state 的参数，不得列入）
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
    _saved_enabled = CONFIG.get("rmgame_enabled", True)
    try:
        CONFIG["rmgame_enabled"] = False
        names_off = [t["function"]["name"] for t in _build_tools()]
        assert "query_wiki" not in names_off and "start_game" not in names_off, names_off
        assert {"say", "update_state", "query_archive", "mora_notes"} <= set(names_off), names_off
        sp_off = build_system_prompt(card, state=dict(INITIAL_STATE))
        assert "query_wiki" not in sp_off and "start_game" not in sp_off, \
            "rmgame 关闭后提示词不应列出 rmgame 工具"
        assert "query_archive" in sp_off and "mora_notes" in sp_off, "常开工具应保留"
        assert "可调用 read_current_text" not in sp_off, "开关关闭后不应提示调用 rmgame 工具"
        assert "【对方正在…】" not in sp_off
        assert "game_context" not in sp_off, "开关关闭后技能清单不应含 game_context"
        act_off = activation_instruction()
        assert "先查再答" not in act_off, "开关关闭后激活指令不应含 GAME_RULES"
    finally:
        CONFIG["rmgame_enabled"] = _saved_enabled
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
    out = summarize_history(merge_msgs, old_merge={"summary": "旧摘要"}, llm_responder=fake_resp)
    assert out and "3 小时前" in out, out
    assert "对话历史" in seen["prompt"] and "不要标注时间段" in seen["prompt"], \
        "合并提示词应要求不标注时间（时间窗由程序换算）"
    assert "不要使用 Markdown 符号" in seen["prompt"], "合并提示词应要求纯文本（无 Markdown）"
    assert "旧摘要" in seen["prompt"], "合并提示词应延续旧合并摘要"
    assert summarize_history(merge_msgs, llm_responder=lambda p: "") is None, "过短应返回 None"
    print("  历史合并: summarize_history（绝对时间输入 / 旧摘要延续 / 失败降级）✓")

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
    rst = tool_result_text(dict(INITIAL_STATE))
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
    st = dict(INITIAL_STATE)  # 20
    st = apply_state(st, parse_llm_reply('{"reply": "x", "affection_delta": 99}'))
    assert st["affection"] == 25, st  # 20 + 5（硬编码上限）
    assert level_for(st["affection"]) == "初遇", level_for(st["affection"])
    st = apply_state(st, parse_llm_reply('{"reply": "x", "affection_delta": -99}'))
    assert st["affection"] == 20, st  # 25 - 5
    st = apply_state(st, parse_llm_reply('{"reply": "x", "affection": 100}'))
    assert st["affection"] == 20, st  # LLM 想直接写绝对值 → 无效，无 delta 则不变
    st = apply_state(st, parse_llm_reply('{"reply": "x", "inner_thought": "想摸鱼"}'))
    assert st["inner_thought"] == "想摸鱼", st
    assert level_for(95) == "亲爱" and level_for(5) == "厌恶" and level_for(55) == "思慕"

    # 技能管理：LLM 通过工具声明；程序白名单过滤；未声明保留原状态
    # 白名单与原生思考模式解耦（推理通道是 think 工具，始终可用）
    st2 = dict(INITIAL_STATE)
    st2 = apply_state(st2, parse_llm_reply('{"reply": "x", "skills": ["fetish_analysis"]}'))
    assert st2["skills"] == ["fetish_analysis"], st2
    st2 = apply_state(st2, parse_llm_reply('{"reply": "x", "skills": ["未知技能"]}'))
    assert st2["skills"] == [], st2  # 白名单过滤：未知技能被剔除
    st2 = apply_state(st2, parse_llm_reply('{"reply": "x"}'))
    assert st2["skills"] == [], st2  # 未声明 → 保留（已为 []）
    # 技能激活后注入：提示词包含 Fetish 分析内容
    sys_prompt_skill = build_system_prompt(card, state={"affection": 20,
                                                        "inner_thought": "x",
                                                        "skills": ["fetish_analysis"]})
    assert "<Fetish_analysys>" in sys_prompt_skill, "技能激活后应注入分析内容"
    assert "【已激活技能】" in sys_prompt_skill, "缺少技能段"
    # 心理 COT：注入激活指令（build_activation），<thinking_format> 包裹，
    # 仅 fetish_analysis 激活；不依赖原生思考模式（reasoning）
    act_skill = build_activation({"affection": 20, "skills": ["fetish_analysis"]}, card)
    assert act_skill.startswith(f"现在，请以{_CHAR.identity}"), "激活指令主体缺失"
    assert act_skill.rstrip().endswith("</thinking_format>"), "心理COT 应追加在激活指令末尾"
    cot_title, cot_body = _psych_cot(card)
    assert cot_title == "心理COT", cot_title   # 原型「心理COT」条目标题（去 🎁 装饰）
    assert "<thinking_format>" in act_skill and "在思考阶段按以下步骤完成心理分析推理" in act_skill, \
        "心理COT 正文应注入激活指令（标题不拼接，见 build_activation）"
    assert "list 3 possible psychological" in act_skill, "心理COT 应含原型步骤4"
    assert "Hypothesis Development" in act_skill, "心理COT 应含原型步骤5"
    assert "{{random" not in act_skill and "{{roll" not in act_skill, "占位符应已实例化"
    # 外壳用原型标签 <thinking_format>（不是 DeepSeek 思考输出标记 <think>，
    # 避免模型把模板当成已完成的推理）；程序统一包一层，不残留双层壳
    assert act_skill.count("<thinking_format>") == 1, act_skill
    assert act_skill.count("</thinking_format>") == 1, act_skill
    assert "<think>" not in act_skill, "不应使用 DeepSeek 思考输出标记包裹模板"
    # COT 适配段：素材来源判断 + 收敛判断（关闭技能），原型原文保留
    assert "素材来源" in act_skill and "read_current_text" in act_skill, "应含素材获取引导"
    assert "收敛判断" in act_skill and "关闭性癖测试技能" in act_skill, "应含收敛/关闭引导"
    assert "0. 检查上回的堕落进度" in act_skill, "原型步骤应原样保留"
    assert "<thinking_format>" not in build_activation(dict(INITIAL_STATE), card), "默认激活指令不含心理COT"
    # system prompt 不再承担 COT：技能激活的 system 也不含任何 COT 标签
    assert "<think>" not in sys_prompt_skill and "<thinking_format>" not in sys_prompt_skill, "COT 已移出 system prompt"
    # 思维链风格指令：激活 fetish_analysis → 思维模式要求（推理式）；否则角色沉浸要求；
    # 与原生思考模式解耦（指令指向 think 工具记录的内容），始终注入
    assert thinking_style_for(dict(INITIAL_STATE)) == THINKING_STYLE_INSTRUCT["immersion"]
    assert "【角色沉浸要求】" in thinking_style_for(dict(INITIAL_STATE))
    assert thinking_style_for({"skills": ["fetish_analysis"]}) == \
        THINKING_STYLE_INSTRUCT["logical"]
    assert "【思维模式要求】" in thinking_style_for({"skills": ["fetish_analysis"]})
    # 指令指向 think 工具通道：不再引用原生 <think> 思考块（工具循环内没有
    # reasoning_content 通道，原生思考与 tool_choice=required 也互斥）
    assert "<think>标签内" not in THINKING_STYLE_INSTRUCT["immersion"] \
        and "<think>标签内" not in THINKING_STYLE_INSTRUCT["logical"], \
        "风格指令应指向 think 工具，而非原生 <think> 思考块"
    assert "think 工具记录" in THINKING_STYLE_INSTRUCT["immersion"] \
        and "think 工具记录" in THINKING_STYLE_INSTRUCT["logical"], \
        "风格指令应指向 think 工具记录的内容"
    # 括号旁白禁令：think 工具契约要求"不要写内心独白"，沉浸式指令不得再
    # 要求括号包裹（曾导致模型把（心想：…）写进 think 内容，见日志 090641）
    imm = THINKING_STYLE_INSTRUCT["immersion"]
    assert "用括号包裹内心活动" not in imm, "沉浸式不应要求括号包裹（think 工具契约冲突）"
    assert "不要用（心想：…）" in imm, "沉浸式应明确禁止括号包裹"
    # 解耦验证：推理相关内容不依赖 setting/llm.ini 的 reasoning（无 thinking 参数
    # 与分支；以上断言即默认配置 reasoning=false 下的行为）
    st_any = apply_state(dict(INITIAL_STATE),
                         parse_llm_reply('{"reply": "x", "skills": ["fetish_analysis"]}'))
    assert st_any["skills"] == ["fetish_analysis"], "fetish_analysis 白名单不依赖原生思考模式"
    sp_any = build_system_prompt(card, state={"affection": 20, "inner_thought": "x",
                                              "skills": ["fetish_analysis"]})
    assert "fetish_analysis" in sp_any, "技能清单/已激活技能不依赖原生思考模式"
    assert "【已激活技能】" in sp_any
    # 动态尾部段（移出 system prompt）：状态/环境/推进由独立函数生成
    ts1 = turn_section((1, 4))
    assert ts1 and "【本回合推进】" in ts1 and "最多可自主推进 4 轮" in ts1, ts1
    ts_mid = turn_section((3, 4))
    assert "已进行 2 轮" in ts_mid, ts_mid
    ts_end = turn_section((4, 4))
    assert "第 4/4 轮（上限）" in ts_end and "不要再调用其他工具" in ts_end, ts_end
    assert turn_section(None) is None
    assert "【本回合推进】" not in sys_prompt, "推进段应移出 system prompt（动态尾部注入）"
    print("  技能管理: 白名单过滤 → 按需注入 → 心理COT（激活指令 <thinking_format>）→ 思维链风格二选一（指向 think 工具）✓ | 与原生思考解耦 ✓")
    print("  动态尾部段: state_section（好感度隐去）/ env_section / turn_section ✓")

    print("[selftest] 全部通过 ✓")
    print(f"  系统提示词长度: {len(sys_prompt)} 字符（仅静态段）")
    print("  语义化状态叙述: 等级「初遇」注入尾部段，数值隐去 | 工具调用协议 ✓ | delta 限幅 ✓")


if __name__ == "__main__":
    selftest()
