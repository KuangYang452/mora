# -*- coding: utf-8 -*-
"""工具注册表 —— tools

全部 LLM 工具的唯一注册处（1.0 起，见 1.0_RELEASE_PLAN.md §5.4）：
- ToolSpec 聚合：工具名 / 语义化描述 / 原生 function schema / 分类标记
  （is_query 查询类 → 意向-动作校验；is_game_world 游戏世界类 →
  <game_data> 包裹）/ rmgame 标记（受 CONFIG["rmgame_enabled"] 开关）。

派生关系（消除手工同步点）：
- llm._build_tools()         → schema_list()（API 注册）
- build_system_prompt 清单   → prompt_names()（提示词只列名称）
- pet._QUERY/_GAME_TOOL_NAMES → query_names() / game_world_names()
- 新增工具 = 在 SPECS 加一条（+ pet._tool_result 有特殊分发时加一行特判）

说明：say / update_state / query_archive / mora_notes 的执行体在 pet.py /
context / notes.py（say、update_state 为内建，query_archive 需会话上下文），
不在此注册 executor —— 分发逻辑见 pet._tool_result。
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# ToolSpec
# ---------------------------------------------------------------------------


@dataclass
class ToolSpec:
    name: str                                   # 工具名（唯一）
    desc: str                                   # 语义化描述（文档/提示词辅助）
    schema: dict                                # function schema 的 parameters 体
    description: str = ""                       # function schema 的 description
    is_query: bool = False                      # 查询类（意向-动作校验）
    is_game_world: bool = False                 # 游戏世界类（<game_data> 包裹）
    rmgame: bool = False                        # RPG Maker 工具（受开关控制）
    executor: Optional[Callable] = None         # 可选执行体（args, ctx）→ 语义化文本


def _params(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required}


# ---------------------------------------------------------------------------
# 注册表（全部工具的唯一来源）
# ---------------------------------------------------------------------------

SPECS: list = [
    # ---- 台词与状态（内建，schema 定义见下）----
    ToolSpec(
        name="say",
        desc="台词通道：说出你的台词（1~3句、总长不超过60字，口语化，可以带表情和❤️）",
        description=(
            "说出你的台词（1~3句、总长不超过60字，口语化，可以带表情和❤️）。"
            "台词是你说出口的话本身：不得包含括号动作描写、旁白、内心想法"
            "或时间标注（这些一律放进 update_state 工具的 inner_thought / "
            "emote 字段）；需要查询游戏资料时先调用查询工具，"
            "工作完成后再用本工具说出最终台词结束本回合。"),
        schema=_params(
            {"text": {"type": "string",
                      "description": "你要说出的台词（1~3句、不超过60字，口语化）"}},
            ["text"]),
    ),
    ToolSpec(
        name="update_state",
        desc="状态更新：好感度变化趋势、内心想法、情绪表情、跳跃动作、技能开关",
        description=(
            "更新角色的内部状态：好感度变化趋势、内心想法、情绪表情、"
            "跳跃动作、技能开关。情绪表情与跳跃动作通过本工具的 "
            "emote / bounce 参数表达（它们不是独立工具，"
            "不要调用名为 emote 或 bounce 的工具）；"
            "台词请通过 say 工具说出，不要放进本工具。"),
        schema=_params(
            {
                "affection_delta": {
                    "type": "integer", "minimum": -5, "maximum": 5,
                    "description": "好感度变化趋势，-5 到 +5；0 表示不变"},
                "inner_thought": {"type": "string", "description": "你此刻的内心想法"},
                "emote": {
                    "type": "string",
                    "enum": ["none", "害羞", "得意", "生气", "愉悦", "戏谑", "撒娇"],
                    "description": "情绪表情（气泡前会显示对应符号）"},
                "bounce": {"type": "boolean", "description": "是否跳一下吸引对方注意"},
                "skills": {
                    "type": "array", "items": {"type": "string"},
                    "description": "要激活的技能名列表（空数组 = 全部关闭）；"
                                   "fetish_analysis 需自行激活；"
                                   "game_context 在游戏环境下自动激活，无需声明"},
            },
            ["affection_delta"]),
    ),
    # ---- RPG Maker 点评工具（rmgame/bridge 执行体，开关 rmgame_enabled）----
    ToolSpec(
        name="discover_running", rmgame=True, is_query=True, is_game_world=True,
        desc="查询当前运行中的游戏（进程枚举+引擎识别+入库状态）",
        description=(
            "查询当前正在运行的游戏（进程枚举+引擎识别）：返回每个游戏的"
            "名称/引擎/目录/入库状态（已入库 或 未入库候选）；"
            "新游戏未入库时提示确认路径（确认前不可启动）。"),
        schema=_params({}, []),
    ),
    ToolSpec(
        name="start_game", rmgame=True, is_game_world=True,
        desc="启动指定 RPG Maker 游戏（注入调试端口）",
        description=(
            "启动指定 RPG Maker 游戏（注入 CDP 调试端口），用于实时读取其文本；"
            "即使游戏当前未运行也可直接启动。参数 game 用游戏库中的名称或 slug"
            "（可用 discover_running 查看库中游戏）；自动发现（未人工确认）的游戏不可启动。"),
        schema=_params(
            {"game": {"type": "string", "description": "游戏库中的名称或 slug"}},
            ["game"]),
    ),
    ToolSpec(
        name="read_current_text", rmgame=True, is_query=True, is_game_world=True,
        desc="读取游戏的最新文本快照（当前地图/场景/对话 + 事件摘要）",
        description=(
            "读取游戏的最新文本快照（当前地图/场景/对话），并附当前事件全文摘要"
            "（含当前位置之前与之后的内容，可能剧透）；指定 game 时若无快照会主动实时读取。"),
        schema=_params(
            {"game": {"type": "string",
                      "description": "可选：游戏名称或 slug（不传则读当前快照）"}},
            []),
    ),
    ToolSpec(
        name="query_wiki", rmgame=True, is_query=True, is_game_world=True,
        desc="查询游戏的 wiki 概念条目（未构建的现场生成）",
        description=(
            "回答对方关于游戏角色、剧情、设定或地点的问题前，必须先调用本工具查询该游戏的 wiki 概念条目"
            "（角色/主题/地点/设定）；未构建的概念会现场生成；query 省略时返回概念列表。"
            "查询无结果时如实告知，不得自行编造设定。"),
        schema=_params(
            {"game": {"type": "string", "description": "游戏名称或 slug"},
             "query": {"type": "string",
                       "description": "可选：概念名或关键词；省略时返回现有概念列表"}},
            ["game"]),
    ),
    ToolSpec(
        name="scan_game", rmgame=True, is_query=True, is_game_world=True,
        desc="扫描游戏全部文本并重建 wiki 概念骨架",
        description=(
            "扫描某游戏的全部文本并重建 wiki 概念骨架（提取 raw + 概念发现）。"),
        schema=_params(
            {"game": {"type": "string", "description": "游戏名称或 slug"},
             "all": {"type": "boolean",
                     "description": "是否提取全部文本（默认 false 仅对话）"}},
            ["game"]),
    ),
    ToolSpec(
        name="read_raw_text", rmgame=True, is_query=True, is_game_world=True,
        desc="读取 raw 原文（按条目 id 核对逐字台词/事件全文）",
        description=(
            "读取某游戏 raw 原文（LLM 友好中间格式），兜底工具：仅当事件摘要/快照"
            "不足或需核对逐字台词时，按条目 id 读取该事件页完整上下文；"
            "关键词/概念查询请用 query_wiki，不要用本工具按关键词搜索。"),
        schema=_params(
            {"game": {"type": "string", "description": "游戏名称或 slug"},
             "query": {"type": "string",
                       "description": "条目 id（如 Map005.16.30，返回该事件同页完整上下文；"
                                      "两段式 Map005.16 返回整事件所有页、按页分组标注）；"
                                      "亦可地图名/关键词（关键词查询请优先 query_wiki）"},
             "limit": {"type": "integer",
                       "description": "条目数上限，默认 200"}},
            ["game"]),
    ),
    ToolSpec(
        name="wiki_arbitrate", rmgame=True, is_query=True, is_game_world=True,
        desc="剧情摘要与 wiki 词条冲突时以原文裁决",
        description=(
            "当剧情摘要（当前事件信息）与 wiki 词条内容冲突时，调用本工具以游戏原文（raw）"
            "为唯一权威裁决冲突责任方：词条素材不全（应补 refs 重建词条）/ 摘要视角局部 / "
            "原文多视角（叙述诡计）/ 双方信息不足。不修改任何数据，只返回裁决与建议。"),
        schema=_params(
            {"game": {"type": "string", "description": "游戏名称或 slug"},
             "concept": {"type": "string", "description": "冲突的概念名"},
             "event": {"type": "string",
                       "description": "可选：冲突事件的条目 id（如 Map005.15.44，"
                                      "通常取当前游戏文本的 match_id）"},
             "conflict": {"type": "string",
                          "description": "可选：冲突描述（摘要说了什么、词条说了什么）"}},
            ["game", "concept"]),
    ),
    ToolSpec(
        name="wiki_rebuild", rmgame=True, is_query=True, is_game_world=True,
        desc="强制重建 wiki 概念条目（以原文为准）",
        description=(
            "强制重建某游戏的 wiki 概念条目：以原文（raw）为准重新生成条目内容；"
            "可选提供 refs 补充素材（如仲裁 suggested_refs，仅收录 raw 中真实存在的条目 id）；"
            "仲裁裁决词条素材不全（wiki_biased）后应调用本工具落实重建。"),
        schema=_params(
            {"game": {"type": "string", "description": "游戏名称或 slug"},
             "concept": {"type": "string", "description": "要重建的概念名或 id"},
             "refs": {"type": "array",
                      "items": {"type": "string"},
                      "description": "可选：应补充进词条的 raw 条目 id 列表"
                                     "（如 ['Map013.6.39']，来自仲裁 suggested_refs）"}},
            ["game", "concept"]),
    ),
    # ---- 归档查询（context.ContextManager 执行体）----
    ToolSpec(
        name="query_archive",
        desc="查询角色与对方历史对话的归档（较早对话被压缩合并后归档于此）",
        description=(
            "查询角色与对方历史对话的归档（较早对话被压缩合并后归档于此）；"
            "需要回忆更早对话的细节（角色/话题/说过的话）时调用。"
            "默认只返回各归档的摘要（已足够回忆大概）；确需核对逐字台词时"
            "设 detail=true 取原文（原文较长，请配合较小的 limit）。"
            "关键词可空格拆成多个短词，任一命中即召回、命中词越多越靠前。"),
        schema=_params(
            {"query": {"type": "string",
                       "description": "可选：关键词（角色/话题/台词片段，可空格拆多词），"
                                      "省略则按时间返回最近归档"},
             "limit": {"type": "integer",
                       "description": "可选：返回归档记录数上限（按归档条数计，非消息条数），"
                                      "默认 5，最大 50"},
             "detail": {"type": "boolean",
                        "description": "可选：是否附上原始消息全文，默认 false（只看摘要）"}},
            []),
    ),
    # ---- 角色私有笔记（notes.py 执行体）----
    ToolSpec(
        name="mora_notes",
        desc="角色私有的笔记文本管理工具（专属文件夹内自由读写）",
        description=(
            "角色私有的笔记文本管理工具：在专属笔记文件夹（runtime/notes/）内"
            "自由读取、新建、修改（覆盖或末尾追加）、删除、列出笔记。"
            "笔记内容不限主题，完全由你自行决定记录什么（感想、观察、资料摘录、"
            "给未来自己的留言等），程序只负责存取。"),
        schema=_params(
            {"action": {"type": "string",
                        "enum": ["list", "read", "write", "append", "delete"],
                        "description": "操作：list=列出全部笔记；read=读取指定笔记；"
                                       "write=新建或覆盖指定笔记；append=在指定笔记末尾追加；"
                                       "delete=删除指定笔记"},
             "name": {"type": "string",
                      "description": "笔记名（不含路径与后缀，自动按 .txt 存取）；"
                                     "list 操作可省略"},
             "content": {"type": "string",
                         "description": "笔记文本内容（write / append 必填；"
                                        "list / read / delete 省略）"}},
            ["action"]),
    ),
]


# ---------------------------------------------------------------------------
# 派生函数
# ---------------------------------------------------------------------------

def specs(rmgame_enabled: bool = True) -> list:
    """全部 ToolSpec（rmgame 工具按开关过滤）。"""
    return [s for s in SPECS if not s.rmgame or rmgame_enabled]


def by_name(name: str) -> Optional[ToolSpec]:
    """按工具名查 SPECS；未注册返回 None。"""
    return next((s for s in SPECS if s.name == name), None)


def schema_list(rmgame_enabled: bool = True) -> list:
    """原生 function calling schema 列表（API 注册用）。"""
    out = []
    for s in specs(rmgame_enabled):
        out.append({"type": "function", "function": {
            "name": s.name, "description": s.description,
            "parameters": s.schema}})
    return out


def query_names(rmgame_enabled: bool = True) -> set:
    """查询类工具名集合（意向-动作校验判定）。"""
    return {s.name for s in specs(rmgame_enabled) if s.is_query}


def game_world_names(rmgame_enabled: bool = True) -> set:
    """游戏世界类工具名集合（结果用 <game_data> 包裹回传）。"""
    return {s.name for s in specs(rmgame_enabled) if s.is_game_world}


def prompt_names(rmgame_enabled: bool = True) -> list:
    """提示词工具清单（只列名称；say 已有专门条目不列入）。"""
    return [s.name for s in specs(rmgame_enabled) if s.name != "say"]


def selftest() -> None:
    """注册表一致性自测。"""
    names = [s.name for s in SPECS]
    assert len(names) == len(set(names)), "工具名重复"
    for s in SPECS:
        assert s.desc and s.description, f"{s.name} 缺描述"
        assert isinstance(s.schema, dict) and s.schema.get("type") == "object", s.name
    # 派生集合与开关过滤
    on = specs(True)
    off = specs(False)
    assert all(s.rmgame for s in on if s.name in
               {"start_game", "query_wiki", "read_current_text", "scan_game",
                "read_raw_text", "wiki_arbitrate", "wiki_rebuild", "discover_running"})
    assert len(off) == len(on) - 8, "rmgame 关闭时应剔除 8 个工具"
    assert "say" not in prompt_names(), "say 不列入提示词清单"
    assert query_names() <= game_world_names(), "查询类工具应属游戏世界类（<game_data> 包裹）"
    print(f"  tools 注册表: {len(SPECS)} 个工具（rmgame 8 + 内建/归档/笔记）"
          f" | 查询类 {len(query_names())} | 游戏世界类 {len(game_world_names())} ✓")


if __name__ == "__main__":
    selftest()
