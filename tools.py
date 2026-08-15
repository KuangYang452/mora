# -*- coding: utf-8 -*-
"""工具注册表 —— tools

全部 LLM 工具的唯一注册处（1.0 起，见 docs/1.0_RELEASE_PLAN.md §5.4）：
- ToolSpec 聚合：工具名 / 语义化描述 / 原生 function schema / 分类标记
  （is_query 查询类 → 意向-动作校验；is_game_world 游戏世界类 →
  <game_data> 包裹）/ rmgame 标记（受 settings.app_get("rmgame_enabled") 开关）。

派生关系（消除手工同步点）：
- llm._build_tools()         → schema_list()（API 注册）
- build_system_prompt 清单   → ToolSpec.prompt_entry()（名称+简述+参数+返回值）
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
    returns: str = ""                           # 返回内容简述（提示词清单用）
    is_query: bool = False                      # 查询类（意向-动作校验）
    is_game_world: bool = False                 # 游戏世界类（<game_data> 包裹）
    rmgame: bool = False                        # RPG Maker 工具（受开关控制）
    executor: Optional[Callable] = None         # 执行体（args, ctx）→ 语义化文本；
                                                # None = 内建回合通道（say/update_state/think）
    status: str = ""                            # 忙碌状态文案（pet 头顶「她在做什么」）

    def prompt_entry(self) -> str:
        """提示词清单条目：名称+简述+参数+返回值。

        参数从 schema 派生（必填/可选标记），返回值为 returns 字段；
        与 function schema 同源于本 SPECS（单一来源，避免双源不一致）。
        """
        req = set(self.schema.get("required") or [])
        params = [
            f"{pname}（{'必填' if pname in req else '可选'}）"
            for pname in (self.schema.get("properties") or {})
        ]
        param_s = "、".join(params) if params else "无"
        return f"「{self.name}」{self.desc}。参数：{param_s}。返回：{self.returns or '执行结果'}。"


def _params(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required}


# ---------------------------------------------------------------------------
# 执行体工厂（M2 接线，见 docs/REFACTOR_DESIGN.md §5）
#
# executor 签名：(args: dict, ctx: object) -> str。ctx 为调用方分发上下文
# （pet 传 self.ctx——ContextManager）；rmgame / notes 执行体不依赖 ctx。
# 统一用**函数内延迟 import**：tools 保持基座层零顶层依赖（不反向引入
# rmgame/notes），运行时才加载执行体模块。
# ---------------------------------------------------------------------------

def _make_rmgame_executor(name: str):
    """rmgame 工具执行体：闭包绑定工具名，转发 rmgame.bridge.execute_tool。"""
    def _exec(args: dict, ctx) -> str:
        from rmgame.bridge import execute_tool
        return execute_tool(name, args)
    return _exec


def _mora_notes_executor(args: dict, ctx) -> str:
    """mora_notes 执行体：转发 notes.execute（args 透传，含 action）。"""
    from notes import execute
    return execute(args.get("action"), args)


def _query_archive_executor(args: dict, ctx) -> str:
    """query_archive 执行体：需要会话上下文（ctx = ContextManager）。"""
    return ctx.query_archive(query=args.get("query"),
                             limit=args.get("limit", 5),
                             detail=args.get("detail", False))


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
        returns="—（台词经气泡展示，无返回文本）",
        status="正在开口…",   # 内建回合通道：executor 留空（回合结束语义，不进 _tool_result）
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
        returns="更新后的状态摘要（好感度等级/内心想法/技能）",
        status="正在调整状态…",   # 内建回合通道：executor 留空（pet 内建结算）
    ),
    # ---- 推理草稿（通用推理增强通道）----
    # DeepSeek API 工具调用与思维链互斥：工具循环内没有 reasoning_content 通道，
    # think 是本回合内的推理通道 —— 内容仅回合内可见（agent_msgs），回合结束
    # 随 agent_msgs 一并丢弃，不进入 ctx.history（通用工具，不限于点评模块）。
    ToolSpec(
        name="think",
        desc="推理草稿：多轮工具推进时的分析计划与跨轮结论（回合结束不保留）",
        description=(
            "多轮工具调用中保存分析结论的通道：查询/工具结果只保留最近两轮"
            "（更早的结果不再保留），需要跨轮引用的结论必须经本工具保留。"
            "用法：开始多轮推进前，先写下分析计划或待验证的假设；每轮调用"
            "得出关键结论后，更新本工具的结论要点（覆盖式，只留最新）；"
            "后续轮次直接引用，不必重新查询。"
            "本工具内容仅本回合内可见，回合结束后不保留；"
            "只写结论要点（不超过 200 字），不要写完整推理过程或内心独白。"
            "需要长期保留的内容请用 update_state 的 inner_thought（持久状态）"
            "或 mora_notes（长期记忆），不要写进本工具。"),
        schema=_params(
            {"content": {"type": "string",
                         "description": "本回合思考结论要点（≤200 字，供后续轮次引用）"}},
            ["content"]),
        returns="—（内容已入思路链，无独立返回）",
        status="正在整理思绪…",   # 内建回合通道：executor 留空（pet 内建占位）
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
        returns="运行中游戏列表（名称/引擎/目录/入库状态）",
        status="正在查看运行中的游戏…",
        executor=_make_rmgame_executor("discover_running"),
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
        returns="启动结果（成功/失败原因）",
        status="正在启动游戏…",
        executor=_make_rmgame_executor("start_game"),
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
        returns="文本快照（地图/场景/当前对话 + 事件摘要与原文样本）",
        status="正在读取游戏画面…",
        executor=_make_rmgame_executor("read_current_text"),
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
        returns="概念条目（摘要+正文）；未收录或概念列表时另有说明",
        status="正在查阅藏书…",
        executor=_make_rmgame_executor("query_wiki"),
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
        returns="扫描结果（文本提取量/概念骨架概况）",
        status="正在扫描游戏文本…",
        executor=_make_rmgame_executor("scan_game"),
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
        returns="条目 id 对应事件的完整上下文（按页分组）",
        status="正在翻看原文…",
        executor=_make_rmgame_executor("read_raw_text"),
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
        returns="裁决结论（责任方 + 建议动作）",
        status="正在核对原文裁决…",
        executor=_make_rmgame_executor("wiki_arbitrate"),
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
        returns="重建结果（新条目摘要/refs 变更）",
        status="正在重建词条…",
        executor=_make_rmgame_executor("wiki_rebuild"),
    ),
    ToolSpec(
        name="rename_game", rmgame=True,
        desc="给游戏起名/改名（基于已读到的游戏内容给出规范名称）",
        description=(
            "为游戏库中的游戏起名或改名：基于已读到的游戏文本/概念"
            "（read_current_text / query_wiki / read_raw_text 等）给出简洁、"
            "无版本/语言标记的正式名称，作为该游戏的显示名与匹配名。"
            "改名会自动迁移该游戏已提取的 raw / wiki / 事件摘要数据目录"
            "（标识随新名更新），旧名保留为别名，此后的对话直接用新名称指代即可。"),
        schema=_params(
            {"game": {"type": "string", "description": "游戏名称或 slug"},
             "name": {"type": "string",
                      "description": "新的正式名称（简洁、不含版本/语言标记）"}},
            ["game", "name"]),
        returns="改名结果（新名称/数据目录迁移情况）",
        status="正在为游戏起名…",
        executor=_make_rmgame_executor("rename_game"),
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
        returns="归档命中记录（默认摘要；detail=true 附原文）",
        status="正在翻找旧档案…",
        executor=_query_archive_executor,   # 需要会话上下文（pet 传 self.ctx）
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
        returns="操作结果（列表/笔记内容/写入或删除确认）",
        status="正在翻看笔记…",
        executor=_mora_notes_executor,
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
    """提示词工具名称清单（旧接口：只列名称，say 不列入）。

    提示词清单现用 ToolSpec.prompt_entry()（名称+简述+参数+返回值）；
    本函数保留供其他只取名称的用途。
    """
    return [s.name for s in specs(rmgame_enabled) if s.name != "say"]


def selftest() -> None:
    """注册表一致性自测。"""
    names = [s.name for s in SPECS]
    assert len(names) == len(set(names)), "工具名重复"
    for s in SPECS:
        assert s.desc and s.description, f"{s.name} 缺描述"
        assert s.returns, f"{s.name} 缺返回描述（提示词清单用）"
        assert isinstance(s.schema, dict) and s.schema.get("type") == "object", s.name
    # 提示词清单条目：名称+简述+参数（必填/可选）+返回值
    pe = by_name("read_current_text").prompt_entry()
    assert pe.startswith("「read_current_text」") and "参数：game（可选）" in pe \
        and "返回：" in pe, pe
    pe2 = by_name("start_game").prompt_entry()
    assert "参数：game（必填）" in pe2, pe2
    pe3 = by_name("discover_running").prompt_entry()
    assert "参数：无" in pe3, pe3
    # 派生集合与开关过滤
    on = specs(True)
    off = specs(False)
    assert all(s.rmgame for s in on if s.name in
               {"start_game", "query_wiki", "read_current_text", "scan_game",
                "read_raw_text", "wiki_arbitrate", "wiki_rebuild",
                "discover_running", "rename_game"})
    assert len(off) == len(on) - 9, "rmgame 关闭时应剔除 9 个工具"
    assert "say" not in prompt_names(), "say 不列入提示词清单"
    assert query_names() <= game_world_names(), "查询类工具应属游戏世界类（<game_data> 包裹）"
    # think 通用推理通道：非 rmgame（开关过滤不剔除）、非查询、非游戏世界
    assert "think" in {s.name for s in on} and "think" in {s.name for s in off}, \
        "think 不应受 rmgame 开关影响"
    assert "think" not in query_names() and "think" not in game_world_names(), \
        "think 既非查询类也非游戏世界类"
    th = by_name("think")
    assert th and th.schema.get("required") == ["content"], "think 应要求 content 参数"
    # M2 接线完备性：executor（内建三工具外全部非空）+ status（全部非空）
    for s in SPECS:
        assert s.status, f"{s.name} 缺 status（忙碌状态文案）"
        if s.name in ("say", "update_state", "think"):
            assert s.executor is None, f"{s.name} 为内建回合通道，不应注册 executor"
        else:
            assert s.executor is not None and callable(s.executor), \
                f"{s.name} 缺 executor（M2 接线）"
    # executor 行为抽查：query_archive 需要 ctx（ContextManager），mora_notes 不需要
    from context import ContextManager as _CM
    qa = by_name("query_archive").executor
    out_qa = qa({"query": None, "limit": 5}, _CM())
    assert out_qa == "归档为空。", out_qa
    mn = by_name("mora_notes").executor
    out = mn({"action": "list"}, None)
    assert isinstance(out, str) and out, "mora_notes 执行体应返回语义化文本"
    print(f"  tools 注册表: {len(SPECS)} 个工具（rmgame 9 + 内建/归档/笔记/think）"
          f" | 查询类 {len(query_names())} | 游戏世界类 {len(game_world_names())}"
          f" | executor 接线 {sum(1 for s in SPECS if s.executor)} + 内建 3 ✓")


if __name__ == "__main__":
    selftest()
