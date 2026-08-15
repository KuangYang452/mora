# -*- coding: utf-8 -*-
"""进程全局会话快照 —— session

从 llm.py 拆出（M1，见 docs/REFACTOR_DESIGN.md §4）：进程启动时从唯一
配置入口与角色包读取一次的会话级常量/状态，运行期不变：
- USER_REFERENCE：setting/user.ini 的 ref（{{user}} 实例化值）
- _CHAR：character/ 包当前激活角色（card.json + profile.ini，含初始状态/
  好感等级/自称/称呼/身份/提示词片段），角色专属数据统一从这里取
- INITIAL_STATE：角色初始状态（好感度/内心想法/技能）
- level_for：好感度 → 等级映射（角色 profile 定义，LLM 无权直接指定）
- get_character()：当前角色卡深拷贝
- _semantic_state()：状态 → 语义化叙述（提示词状态段与工具结果回传共用，
  依赖本模块全部符号，故随会话快照同置）

角色运行期切换（set_character）为 M1 顺带能力：本次仅提供基础设施
（快照集中化），切换入口不在本重构范围。
"""

import copy

import settings
import character as character_mod

USER_REFERENCE = settings.user_ref()
_CHAR = character_mod.current()
INITIAL_STATE = _CHAR.initial_state()
level_for = _CHAR.level_for


def get_character() -> dict:
    """返回当前激活角色的角色卡（character/<slug>/card.json）。

    prototype/ 下的角色卡 JSON 仅作为参照原型，程序不直接依赖；
    运行时只读取由 freeze_prototype.py 生成、character 包加载的角色数据。
    返回深拷贝，防止调用方意外修改共享数据。
    """
    return copy.deepcopy(character_mod.current().card)


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


def selftest() -> None:
    """会话快照自测：依赖真实角色包（mora）结构，不验证人设内容。"""
    assert USER_REFERENCE, "用户指称不应为空"
    assert _CHAR.slug and _CHAR.display_name and _CHAR.identity, "角色快照异常"
    assert isinstance(INITIAL_STATE, dict) and "affection" in INITIAL_STATE \
        and isinstance(INITIAL_STATE["skills"], list)
    assert level_for(95) == "亲爱" and level_for(5) == "厌恶" and level_for(55) == "思慕", \
        level_for
    card = get_character()
    assert card and card.get("name"), "角色卡深拷贝异常"
    assert card is not get_character(), "get_character 应返回独立深拷贝"
    # 状态语义化：数值/等级/内心想法；with_affection=False 隐去数值
    ss = _semantic_state({"affection": 20, "inner_thought": "x"})
    assert "好感度 20/100" in ss and "「初遇」" in ss and "x" in ss, ss
    assert USER_REFERENCE in ss, "语义化应含用户指称"
    ss2 = _semantic_state({"affection": 20, "inner_thought": "x"}, with_affection=False)
    assert "好感度 20/100" not in ss2 and "「初遇」" in ss2, ss2
    # 缺省回退：状态缺字段时用初始状态
    ss3 = _semantic_state({})
    assert "好感度 " in ss3 and INITIAL_STATE["inner_thought"] in ss3, ss3
    print("[session.selftest] 通过 ✓ 会话快照 / 等级映射 / 状态语义化（数值隐去）")


if __name__ == "__main__":
    selftest()
