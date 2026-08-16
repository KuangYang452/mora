# -*- coding: utf-8 -*-
"""角色卡文本解析 —— llm_card

从 llm.py 拆出（M1，见 docs/REFACTOR_DESIGN.md §4）：SillyTavern 角色卡
文本的程序层解析/适配——清染色标签、剥通道块、开场拆解、占位符实例化
（{{user}}/{{char}}/{{random::}}/{{roll::}}）、世界书按用途分类（风格/
好感等级/技能）、风格块叙述教条改写、心理 COT 条目提取。
只被 llm_prompt 使用；依赖 session（指称/角色快照）与 character 包。

说明：设计文档 §4.1 原建议移入 character 包；实施时调整为独立模块——
character 包是角色层**纯内容契约**的加载器（character → settings 唯一
依赖），这些是程序层的文本处理逻辑，独立成模块可保持角色层边界不动摇。
"""

import re

import session
from session import USER_REFERENCE, _CHAR

# 心理 COT 适配段：原型步骤（0-6）是纯循环分析引擎，缺收敛出口与素材来源引导。
# 在原型原文之后追加本场景规则（不改写原型正文）：
# - 素材确认：游戏环境中{USER_REFERENCE}的言行常来自游戏内剧情，分析前先读游戏文本；
# - 收敛判断：信息足够时输出结论并关闭技能，避免无限追问。
COT_ADAPT = (
    f"\n\n分析适配（本场景规则，适用于上述步骤）：\n"
    f"· 素材来源（步骤 3~5 分析前）：若{USER_REFERENCE}的言行缺乏上下文线索——"
    f"尤其{USER_REFERENCE}在玩游戏时，言行常来自游戏内剧情——先调用 read_current_text / "
    f"query_wiki 获取游戏文本作为分析依据；区分{USER_REFERENCE}本人的行为与游戏内角色/剧情，"
    "不要混为一谈。\n"
    f"· 收敛判断（步骤 6 之后）：若已收集足够信息（{USER_REFERENCE}已回答核心问题、关键假设已有"
    "支持或反驳证据、追问已达合理轮次），结束测试——得出明确分析结论（供台词交付），"
    "并在 update_state 中把 skills 设为空数组关闭性癖测试技能；"
    "仅当关键信息仍缺失时才继续追问。"
)


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


def selftest() -> None:
    """角色卡解析自测：依赖真实角色包（mora）结构，验证解析契约。"""
    card = session.get_character()
    assert (card.get("description") or "").strip(), "角色卡描述读取失败"
    # 输出通道指令必须被剔除：SillyTavern 的 updata/getvar/setvar 由
    # 硬编码变量管理器替代，不得残留
    for marker in ("<updata>", "{{getvar", "setvar:", "<StatusBlock>"):
        assert marker not in (card.get("description") or "")
    # 开场拆解：场景 + 台词流
    scene, lines = extract_opening(card)
    assert isinstance(lines, list) and all(isinstance(x, str) for x in lines)
    # 世界书分类：三路输出结构正确
    style, levels, skills = _categorize_book(card)
    assert isinstance(style, list) and isinstance(levels, dict) and isinstance(skills, list)
    # 心理 COT 提取：原型条目标题（去 🎁 装饰）
    cot_title, cot_body = _psych_cot(card)
    assert cot_title == "心理COT", cot_title
    assert cot_body and "<thinking_format>" not in cot_body, "外壳应由调用方包裹"
    # 风格改写：教条剥离
    adapted = _adapt_style("括号描述:xxx\n- 其他规则")
    assert "括号描述" not in adapted and "inner_thought 与 emote 字段" in adapted, adapted
    # 占位符实例化：确定性 random/roll + 指称替换
    t = _instantiate("{{random::A::B}} {{roll:2d6+1}} {{user}} {{char}} {{char_self}}")
    assert "A" in t and "B" not in t and "{{" not in t, t
    print("[llm_card.selftest] 通过 ✓ 清标签 / 通道块剔除 / 开场拆解 / 世界书分类 / COT 提取 / 占位符实例化")


if __name__ == "__main__":
    selftest()
