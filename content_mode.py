# -*- coding: utf-8 -*-
"""内容模式（NSFW / SFW）—— content_mode

从 llm.py 拆出（M1，见 docs/REFACTOR_DESIGN.md §4）：内容模式状态与
过滤规则的单一实现处。全仓架构约定以 README「架构」八条为唯一入口，
本 docstring 只声明模块定位、不重复约定全文。

- 开关：setting/app.ini 的 content_mode，进程启动时读取，可经
  set_content_mode 运行时切换（写回 app.ini，下回合生效）；
- 投放方式与依赖方向见 README「架构」约定 7/8（本模块为实现处）：
  两处投放 = content_mode_rule（【输出红线】一行）+ content_mode_directive
  （激活指令首行裸标记，贴近输出位置）；技能声明自身评级（data.SKILLS
  的 nsfw_only），开关按属性通用过滤（_mode_allowed_skills），不依赖
  任何具体技能名。
"""

import settings
from data import SKILLS


def _normalize_content_mode(raw) -> str:
    """content_mode 取值归一化：nsfw / sfw；非法值启动时直接报错（无静默兜底）。"""
    v = str(raw or "").strip().lower()
    if v in ("nsfw", "adult", "1", "true", "yes", "on"):
        return "nsfw"
    if v in ("sfw", "safe", "0", "false", "no", "off"):
        return "sfw"
    raise settings.ConfigError(
        f"setting/app.ini 的 content_mode 取值非法: {raw!r}（合法值 nsfw / sfw）")


CONTENT_MODE = _normalize_content_mode(settings.app_get("content_mode", "nsfw"))


def set_content_mode(mode: str) -> str:
    """运行时切换内容模式：更新内存值并写回 setting/app.ini（文本级，保留注释）。

    提示词在每回合组装时读取 CONTENT_MODE（build_system_prompt /
    build_activation 按调用时刻取值），切换后下一回合即生效。
    返回归一化后的模式名。
    """
    global CONTENT_MODE
    new = _normalize_content_mode(mode)
    CONTENT_MODE = new
    settings.set_app_value("content_mode", new)
    return new


def _mode_allowed_skills() -> set:
    """当前内容模式下可用的技能名集合（白名单过滤的单一来源）。

    SFW 下剔除声明 nsfw_only 的技能（如角色卡的性癖分析）；NSFW 全量。
    依赖方向：技能声明自身评级（data.SKILLS 的 nsfw_only），开关按属性
    过滤，不依赖任何具体技能名。
    """
    if CONTENT_MODE == "sfw":
        return {n for n, s in SKILLS.items() if not s.get("nsfw_only")}
    return set(SKILLS)


def content_mode_rule() -> str:
    """内容模式规则行（【输出红线】块内的一行，见 build_system_prompt）。

    贴合输出红线格式「- 主体：约束。」，只留可执行约束、不重复模式含义
    （NSFW/SFW 语义由标记与模型常识承载）。
    """
    if CONTENT_MODE == "sfw":
        return "- 内容模式:SFW：不允许输出。"
    return "- 内容模式:NSFW：不允许回避。"


def content_mode_directive() -> str:
    """本回合内容模式裸标记：`内容模式:NSFW`，激活指令首行（紧邻【本回合指令】）。

    模型有安全化倾向，贴近输出处重复强调模式（近因效应）；只写标记不赘字。
    """
    return "内容模式:NSFW" if CONTENT_MODE == "nsfw" else "内容模式:SFW"


def selftest() -> None:
    """内容模式离线自测（全局切换用 try/finally 恢复，防测试污染）。"""
    global CONTENT_MODE
    assert CONTENT_MODE in ("nsfw", "sfw"), CONTENT_MODE
    assert _normalize_content_mode("nsfw") == "nsfw" and _normalize_content_mode("SFW") == "sfw"
    try:
        _normalize_content_mode("banana")
        raise AssertionError("非法 content_mode 应抛 ConfigError")
    except settings.ConfigError:
        pass
    # 输出红线行：贴合格式「- 主体：约束。」，只留可执行约束（不重复模式含义）
    cm_nsfw = content_mode_rule()
    assert "- 内容模式:NSFW" in cm_nsfw and "不允许回避" in cm_nsfw, cm_nsfw
    assert "许可成人词汇" not in cm_nsfw, "NSFW 行不应重复模式含义（NSFW 已含此义）"
    # 依赖方向：技能声明自身评级（data.SKILLS 的 nsfw_only），开关逻辑不依赖
    # 任何具体技能名（模式相关文本不含技能名）
    assert SKILLS["fetish_analysis"].get("nsfw_only") is True, "性癖分析应声明仅 NSFW 可用"
    assert not SKILLS["game_context"].get("nsfw_only"), "game_context 不应声明 nsfw_only"
    assert "fetish_analysis" not in content_mode_rule() and \
        "fetish_analysis" not in content_mode_directive(), "开关逻辑不应依赖具体技能名"
    # SFW 模式：规则行翻转 + 通用过滤（白名单全链路禁用 nsfw_only 技能）
    _saved_mode = CONTENT_MODE
    try:
        CONTENT_MODE = "sfw"
        cm_sfw = content_mode_rule()
        assert cm_sfw != cm_nsfw and "- 内容模式:SFW" in cm_sfw and "不允许输出" in cm_sfw, cm_sfw
        assert "禁止成人词汇" not in cm_sfw, "SFW 行不应重复模式含义（SFW 已含此义）"
        assert _mode_allowed_skills() == {"game_context"}, _mode_allowed_skills()
        assert content_mode_directive() == "内容模式:SFW"
    finally:
        CONTENT_MODE = _saved_mode
    # 恢复后 NSFW 链路可用（防测试污染）
    assert _mode_allowed_skills() == set(SKILLS)
    print("[content_mode.selftest] 通过 ✓ 归一化 / 红线与裸标记 / SFW 通用过滤 / 依赖方向")


if __name__ == "__main__":
    selftest()
