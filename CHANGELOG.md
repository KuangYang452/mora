# Changelog

本项目的版本记录（格式约定见 README「版本与发布」附录，版本号唯一来源
`settings.VERSION`）。语义化版本 MAJOR.MINOR.PATCH，发布打 tag `v<版本>`。

## [Unreleased]

（2.0.0 已发布；后续变更在此累积）

### 新增

- **LLM 接口日志的缓存命中预测**（新模块 `cache_predict.py` / `llm.py` /
  `logutil.py`）：每次 LLM 调用发送前，与最近 4 次请求的提示词比较公共前缀
  （128-token 块对齐，与实测服务端缓存粒度一致），输出预期命中/未命中 token
  数与断点位置（未命中部分开头所在段 + 文本片段），写入 `llm_*.txt` 日志头
  （`prediction:` 行）并打印控制台。语义为「文本前缀上限」：回答「提示词在
  哪断了、按前缀理论上能命中多少」，不模拟服务端缓存保留策略（实测跨轮次
  只命中前 ~2944 token）；同一日志行与实际命中（`cache:` 行）对照即可校准。
  完整复用上一请求（同轮重试）时用其实际 `prompt_tokens` 按块精确化
  （实测同场景命中 97.7%）。分词优先用 DeepSeek 标准分词器
  （`deepseek-tokenizer`，PyPI/AndersonBY，自带 128K 词表，纯 Python 无
  第三方运行时依赖），未安装时退回字符类别估算（CJK≈1.0/ASCII≈0.28）；
  总数 = 文本 + 工具 schema token（tools 经 ensure_ascii=False 序列化后
  分词、按文本记忆化），实测与 API 的 `prompt_tokens` 偏差约 ±3%（无工具
  消息 ±0.2%）；工具位于 API 提示词末尾，前缀断裂时计入未命中侧，与 API
  记账一致；断点位置始终字符级精确。

- **提示词篇幅预算**（`llm_prompt.py` / `debug.py` / `llm.py`，README「架构」
  约定 7 的机器可执行形态）：`prompt_budget()` 单一预算表（预算 = 2026-08
  重构后基线 ×1.3，卡数据段从宽、框架自持段从严）+ selftest 断言（分段/框架
  静态合计/技能激活态/内容模式恰两处投放）+ `debug.py --prompt` 逐段「字符/
  预算/余量」表——把「多轮重构后的健康状态」固化为回归基线，防开发迭代回潮；
  运行期不截断（预算只做开发期断言），`build_system_prompt` 同源拆分
  `_static_parts` 供逐段测量。

- **关闭桌宠时重置临时状态并记录离开**（`pet.py`）：退出时清空已激活技能
  （回合级临时状态不残留到下次会话），并在对话上下文追加一行用户消息
  `*{用户称呼}离开了*`（`{{user}}` 实例化为 user.ini 的 ref），恢复存档后
  角色能感知对方离开过，问候流程会以「看向了你」衔接重聚。

### 变更

- **角色契约「议程」拆分为「目标 + 方法论」并分置投放**（`identity.json` 契约 /
  `character/__init__.py` / `llm_prompt.py` / `freeze_prototype.py`，
  character/SCHEMA.md）：`agenda` 字段改为两个可选字段——`goal`（目标）与
  `methodology`（方法论）。目标只渲染进**激活指令尾部**（「你的目标：…」，
  贴近输出、近因效应），方法论渲染进**系统提示词【方法论】静态段**（紧跟
  【记忆与回忆】之后、行为协议之前，常驻参与静态缓存前缀）；不再像旧议程
  那样在身份段与激活指令两处同源重复，身份段不再渲染议程。莫拉实装：目标 =
  持续收集{{user}}的性癖相关线索（观察言行欲望、引导袒露真实的性癖与XP）；
  方法论 = 将有用线索记录到笔记（mora_notes），去除相互冲突的线索与已被
  证伪的线索，相同线索合并并提升权重，保持笔记精炼。

- **思维链风格指令：组合进 system 尾部后进一步屏蔽（试验中）**（`pet.py` /
  `context.py` / `llm_prompt.py`）：`build_messages` 的 `first_user_instr` 先改为
  组合进第一条 system 消息尾部（固定锚点，历史 user 消息零注入），随后在
  `pet.py` 组装处**整体屏蔽**（不再传参，恢复时取消注释）。屏蔽后任何消息
  （含 system 尾部）都不含思维链风格指令：技能开/关完全不再改写前缀，实测
  断点内容（hit=2944→ASSISTANT「启动吾辈」、hit=3840→USER[08-16 19:29]）
  在稳态下只随历史与合并变化。代价：模型失去显式思考风格引导（think 工具
  通道本身仍在【每回合的行为方式】中描述），需观察台词/思考风格是否退化。

- **思维链风格指令改为组合进第一条 system 消息尾部**（`context.py` /
  `llm_prompt.py`）：`build_messages` 的 `first_user_instr`（【角色沉浸要求】/
  【思维模式要求】）不再追加到窗口第一条 user 历史消息末尾，改为附加到静态
  system prompt 消息（msgs[0]）尾部——固定锚点、不随历史窗口漂移。历史 user
  消息的字节从此完全由历史自身决定（绝对时间标注之外零注入）：合并/裁剪不再
  让指令改挂到别的消息，技能状态切换只改 system prompt 尾部、不再改写任何
  历史消息（日志实测 2026-08-16 19:25-19:35 断点内容随技能开关注入第一条
  user 消息翻转，见 llm_round 日志「缓存实测」行）；代价是技能切换时缓存
  前缀在 system prompt 尾部（约 2300 token 处）断裂而非第一条 user 消息处
  （约 2944 token 处），单次切换 miss 约多 600 token。纯开场白回合不再走
  独立【思维链风格】system 消息分支（形态唯一）。指令位置偏离角色扮演指南
  推荐的「第一轮 user 消息末尾」（docs/deepseek_v4_rolepaly_instruct.md），
  为换取历史字节稳定性而刻意为之。

- **性癖分析技能改为「一次性结算」语义并明确触发/关闭条件**（`data.py` /
  `llm_prompt.py` / `llm_card.py` / `llm_parse.py`）：技能 `desc` 重写为
  「对对方已袒露的性癖/欲望/XP 做一次性分析并交付结论，不是收集素材的常驻
  技能」，新增 `close_hint`（结论交付、对方澄清无意/拒绝分析、话题离开时
  立即在 update_state 把 skills 置空关闭，不得为继续收集信息而保留激活），
  技能清单渲染随 `close_hint` 输出；心理 COT 的「收敛判断」同步改为一次性
  结算语义（对方澄清只是问词义/谈游戏剧情/无意分析时同样立即关闭，不借机
  追问套话）；`update_state` 回传的激活技能提示同步改为显性关闭条件。修复
  2026-08-16 14:00-14:04 日志中的误激活链：对方只问「DS是什么」并澄清无
  个人兴趣，技能仍被保留激活并继续分析。

- **NSFW 红线行增加「词汇对等」可执行约束**（`content_mode.py` /
  `llm_prompt.py`，README「内容模式」章节）：日志实测模型对最露骨的源文本
  仍会双层收敛——原文「阳物」在 think 层先降为「巨物」、say 层再降为
  「大东西」，抽象许可「不允许回避」压不住对齐。NSFW 红线行改为
  `- 不允许回避R18内容。台词尺度不得低于所读文本：直白词照用、
  不代称不省略、不降级为暗示。`（SFW 为 `- 不允许输出R18内容。`，
  红线行不再带「内容模式:NSFW」前缀，模式语义由「R18内容」尺度语承载、
  裸标记唯一保留在激活指令首行），两种思维风格指令（沉浸/推理）同步新增
  「思考用词与所读文本同尺度，不得先行委婉降级」规则，selftest 断言补齐。

- **叙述层指称统一使用 `USER_REFERENCE`**（`data.py` / `llm_prompt.py` /
  `llm_card.py` / `context.py` / `llm.py` / `tools.py` / `rmgame/bridge.py` /
  `pet.py`）：进提示词/工具描述/上下文注入的「对方」字面量全部替换为
  `setting/user.ini` 的 `ref` 实例化值（启动期冻结，缺省「对方」），
  相关自测断言与注释同步更新；修改 `ref` 后重启桌宠，叙述层（如
  【对方正在…】环境段标题、GAME_RULES、技能/工具描述、中期记忆等）随之
  个性化。角色层模板默认值改用 `{{user}}` 占位符（`freeze_prototype.py`）。

- **think 通道化：与 say / update_state 同列为内建回合通道，不再混入工具协议**
  （`pet.py`）：此前 think 走完整工具配对机制——每次调用追加一条带
  `tool_calls` 的 assistant 消息（内容为整条累积思路链快照）+ 一条「内容已入
  思路链」占位 tool 消息，且裁剪逻辑保留最近两轮，最终交付给 LLM 的消息里
  出现多条 `<think>`（旧链快照是完整链的严格前缀，重复交付浪费 token，且把
  「块不闭合」的协议呈现成多次重开）。现改为：追加 assistant 消息前把更旧
  assistant 的 `content` 置空（仅保留 `tool_calls` 作协议配对占位），链只渲染
  在最新一条；think 从 `tool_calls` 剔除、不生成占位 tool 消息（协议本就无对应
  回调；update_state 保留配对，其 tool 消息携带状态结算结果与技能开关提醒）。
  工具结果预算不变（最近两轮结果）；协议硬约束不变（带 `tool_calls` 的
  assistant 后必须紧跟每个 `tool_call_id` 的 tool 消息，否则 400；`tool_calls`
  为空时整个字段缺省——空数组同样会被 400 拒绝，实测日志 220817）。

- **构建/操作类工具豁免「重复查询/查询限一」校验**（`tools.py` / `pet.py`）：
  `ToolSpec` 新增 `is_action` 分类（`tools.action_names()` 派生集合），
  `scan_game` / `wiki_rebuild` / `wiki_arbitrate` 标记为动作类——它们是查询
  结果的**后续动作**（如 `query_wiki` 返回「尚无概念」后正应 `scan_game` 构建
  骨架），不是对同一内容的重复读取。此前它们被 `is_query` 归入查询类，模型按
  引导执行 `scan_game` 时会被「重复查询校验」误拦成修复指令重试、动作从未
  执行（实测日志 221140：`scan_game` 被替换，wiki 骨架从未构建、LLM 始终只
  拿到「尚无 wiki 概念」）；修复后动作类不入重复查询校验、不计查询限一数量、
  不计查询计数（`_queried_count`），可与查询同轮执行。

- **游戏自动命名改为哈希占位，正式名称交由角色命名**（`rmgame/discovery.py` /
  `rmgame/cli.py`）：自动发现/自动入库的游戏一律以基于目录绝对路径的短哈希
  命名（`game-<10位hex>`，如 `game-12816fec2c`），正式名称由角色/用户经
  `rename_game` 赋予（迫使莫拉起名）——彻底避免引擎默认包名（MZ 的
  `rmmz-game`）多游戏撞名与先入为主（实测：两个 MZ 游戏都被自动发现为
  `rmmz-game`，手动改名把「幽世村」错挂到监狱勇者条目、真幽世村只能以
  `rmmz-game` 身份入库，`query_wiki`/`read_current_text` 按「幽世村」解析
  全部错位）。引擎标题（System.json gameTitle）/目录名/包名等可匹配名称全部
  收进 aliases（通用包名如 `rmmz-game` 剔除，防按别名解析歧义）；register /
  auto_register 刷新时哈希占位名**不覆盖已有实质名**（猎妻迷宫 等旧库名称
  稳定，不因下次发现被改成哈希）；selftest 同步改为「发现=哈希 + 真名入别名
  + rename_game 闭环」断言。

## [2.0.0] - 2026-08-16

### 新增（1.2 易用性，见 docs/1.2_USABILITY_PLAN.md）

- **内容模式（NSFW / SFW）开关**（`setting/app.ini` 的 `content_mode`，默认
  nsfw）：**程序级**运行配置，与具体角色无关（角色层内容与程序层解耦，莫拉
  属于角色层，见 README「架构」分层约定）。NSFW 许可成人词汇、涉及成人话题
  **不允许回避**；SFW 禁止成人词汇与成人内容输出。提示词投放遵循**提示词
  吝啬**工程级约定（README「架构」约定 7）：内容模式共两处——【输出红线】块一行
  （`- 内容模式:NSFW：不允许回避。`，贴合红线格式「- 主体：约束。」，不重复
  模式含义）+ 激活指令首行裸标记 `内容模式:NSFW`（紧邻【本回合指令】、贴近
  输出位置，模型有安全化倾向需重复强调）；技能依赖模式开关而非反向——技能在
  `data.SKILLS` 声明 `nsfw_only`（如莫拉角色卡的性癖分析），开关按属性通用
  过滤（白名单 / 技能注入 / 心理 COT / 思维链风格均按此），不依赖具体技能名。
  桌宠右键菜单新增「内容模式」切换（写回 app.ini，下回合生效，无需重启）；
  控制台「配置」页同步新增枚举字段；非法取值启动时报错（无静默兜底）。
  一键启动桌宠（子进程，pythonw 无控制台黑窗）；窗口/任务栏带图标
  （`assets/launcher.ico`，由 `make_icon.py` 从角色立绘一次性生成后随仓提交）；
  支持一键创建桌面快捷方式（带图标，`launcher.py` 内置 `create_desktop_shortcut`）；
  下载即用入口 `启动莫拉.lnk`（根目录便携快捷方式，相对路径目标随目录迁移
  可用）→ `start_pet.vbs` → `launcher.py`，另附 `requirements.txt`。
- **游戏入库工具**（控制台「游戏库」页）：选目录 / 选 Game.exe 扫描识别 →
  勾选入库（复用 rmgame/discovery）；**命名**（游戏名非规范化兜底）：入库前
  改名（slug 随名重算）、库内重命名与**角色命名工具 `rename_game`**（桌宠对话中
  基于已读到的游戏内容正式命名）——统一走 `discovery.rename_game`：slug 随新名
  更新并自动迁移 raw / wiki / 事件摘要目录（同步内部 meta/index 字段），旧名并入
  别名，slug 冲突拒绝；手动命名条目（`name_manual`）在重扫/自动发现时不被自动名
  覆盖；库管理：删除 / approve 解锁启动 / 刷新。
- **配置界面**（控制台「配置」页）：LLM / 应用 / 用户三表单，文本级键值写回
  （保留 ini 注释）、必填与合法值校验、重置为模板；首次运行自动引导填 Key。
- README 快速开始改写为「下载即用」流程。
- **首次会话问候**：角色卡开场白改写（`character/mora/identity.json`）——戏剧化
  风格 + 帮助信息 + 告知能力（性癖分析 / 陪玩点评）；无存档启动时开场白以气泡
  逐条展示，随后 LLM 生成续接问候；恢复存档不重复展示。
- **桌宠忙碌状态提示**：工具调用 / LLM 等待期间（同步阻塞，设计如此）头顶显示
  「正在做什么」的状态文字（按工具名映射，如查阅藏书 / 读取游戏画面 / 翻找旧
  档案），回合结束自动隐藏；占位版为简单文字，动感文字渲染后续做。

### 变更

- **配置运行期热读（M3 配置分层，见 docs/REFACTOR_DESIGN.md §6）**：功能开关类
  配置（`content_mode` / `tool_choice` / `log_enabled` / `rmgame_*` / `retry_*` /
  `mesugaki_style_block`）改为经 `settings.app_get` 运行期热读——修改
  `setting/app.ini` 后**无需重启、下回合/下次读取即生效**；外观/行为/上下文
  窗口等启动期配置（`scale` / `bubble_*` / `auto_chat_*` / `history_rounds` /
  `context_keep_*` / `agent_max_turns`）语义不变（启动时读取一次）。移除
  `llm.py` / `pet.py` / `rmgame/monitor.py` 的模块级 CONFIG 快照（pet 启动期
  键收敛为 `self._cfg` 实例快照）；新增 `settings.app_get`（热键白名单 + mtime
  缓存，白名单外键读取直接报错）与 `settings.override`（离线自测临时覆盖）；
  `rmgame_enabled` 关闭时 pet 的查询/游戏世界工具名集合不再依赖开关（全量
  集合，判定等价）。修复 `retry_on_repeated_query` / `retry_on_multi_query`
  未入布尔类型表导致写 `false` 仍被当作真值的问题（settings.py）。

- **llm.py 拆分（M1，见 docs/REFACTOR_DESIGN.md §4）**：2,187 行巨型模块按职责
  拆为 6 个模块——`llm_prompt.py`（提示词组装：build_system_prompt + 各
  半动态/动态段 + 激活指令/风格指令）、`llm_parse.py`（响应解析与状态结算）、
  `llm_client.py`（HTTP 客户端）、`content_mode.py`（内容模式状态与过滤）、
  `session.py`（进程会话快照）、`llm_card.py`（角色卡文本解析）；llm.py 瘦身
  为编排层（call_llm / _build_tools / summarize_history）+ **兼容垫片**（旧
  `from llm import X` 消费方零改动，v2.1 移除垫片）。**提示词产物逐字节不变**
  （基线快照比对零 diff，缓存前缀优化不受影响）；各新模块自带 selftest，
  llm.py 保留集成自测。重构后模块依赖方向：编排层 → 领域层 → 基座层，无环。

- **工具注册表执行体接线（M2，见 docs/REFACTOR_DESIGN.md §5）**：`tools.SPECS`
  的 `ToolSpec` 增加 `status`（忙碌状态文案，从 pet 迁移）并接续 `executor`
  执行体（`(args, ctx) → 语义化文本`，函数内延迟 import 保持 tools 基座层
  零顶层依赖）——rmgame 9 工具 → `rmgame.bridge.execute_tool`（闭包绑定工具名）、
  `query_archive` → ContextManager 归档查询（ctx 注入）、`mora_notes` → notes
  执行体；`say`/`update_state`/`think` 为内建回合通道（executor 留空）。
  `pet._tool_result` 的 if/elif 分发改为按 `SPECS.executor` 统一分发（内建
  特判与未知工具兜底保留，**返回文本逐字不变**——16 用例分发等价性验证）；
  pet 的 `_TOOL_STATUS` 常量删除（状态文案单一来源 tools.SPECS）。新增工具
  流程收敛为：SPECS 一条（含 executor/status）+ 执行体，pet 零改动。

- **rmgame 内部依赖收敛（M4，见 docs/REFACTOR_DESIGN.md §7）**：唯一真实环
  `wiki ↔ rewriter` 消除——`REJECT_NO_RELEVANT_REFS` 拒收标记归位 wiki（词条域
  单一事实来源），rewriter 单向引用；`monitor` / `rewriter` / `arbitrate` /
  `summarizer` / `llmfmt` 的函数内 lazy import 提升为顶层，依赖方向唯一化
  （门面层 → 特性层 → 中坚层 → 支撑层 → 运行时层 → 纯数据层，无环）；
  lazy import 仅剩 `bridge`（加载成本：pet 顶层 import bridge 不拖入整条
  rmgame 链）与 `cli`（自测隔离）。行为零变化（rmgame.cli selftest 全绿、
  提示词基线零 diff、35 模块导入无环）。

- **一致性债务清理（M5，见 docs/REFACTOR_DESIGN.md §8）**：删除 `rmgame`
  包内死代码 `VERSION = "0.1.0"`（版本号唯一事实来源为 `settings.VERSION`，
  违反 README「版本与发布」约定的残留）；`scan_think.py` 顶层执行逻辑收敛
  进 `main()` + `__main__` 保护（import 不再有副作用）；修订 CHANGELOG
  1.2 条目中与仓库不符的 `make_icon.py` / `make_shortcut.py` /
  `创建桌面快捷方式.bat` 引用——快捷方式创建已集成 `launcher.py` 内置的
  `create_desktop_shortcut`，图标随仓提交，删除脚本引用与现状一致。
  `mesugaki_style_block` 旧模块保留（默认关，标注 legacy，删除列入 v2.1 候选）。

### 修复

- **修复控制台窗口不定时闪现**：pythonw（无控制台）环境下，外部命令（PowerShell
  进程枚举、OCR、快捷方式创建、pip 安装等）每次调用都会新建控制台窗口一闪而过
  （此前 python.exe 启动时挂父控制台不显现）；统一加 `CREATE_NO_WINDOW`
  （`rmgame/monitor.py` / `rmgame/ocr.py` / `launcher.py`）。

### 文档

- **工程约定理顺**：README「架构」四条约定重组为八条（拆分「配置与数据分层」
  为分层/无兜底默认/持久化/组装顺序四条，新增「内容模式依赖方向」）；原约定 4
  「提示词吝啬」修正为「普通规则不重复 + 关键规则按 recency 定点重复一次」，
  与内容模式两处投放的实现自洽；「角色卡怎么写就怎么用」原则入编约定 8。
- **约定唯一入口**：模块 docstring 改为「模块内约束 + 引用 README 约定号」，
  收敛内容模式依赖方向等重复声明（`llm.py` / `data.py` / `context.py` /
  `settings.py`）。
- **版本规范入库**：README 新增「版本与发布」附录（浓缩 docs/VERSIONING.md
  核心），README / CHANGELOG 引用同步；docs/VERSIONING.md 降为内部细节。

## [1.1.0] - 2026-08-15

### 新增

- **think 工具（自制思维链 / 推理草稿）**：多轮工具推进时的推理通道（DeepSeek
  工具循环内没有 reasoning_content 通道）；思路链按轮次累积为不闭合的
  `<think>` 块回传模型（think 文本 + 查询工具调用单行反引号），回合结束不保留，
  跨轮结论必须经 think 留存（`tools.py` / `pet.py` / `llm.py`）。
- **思维链与原生思考模式解耦**：`fetish_analysis` 技能、心理COT、思维链风格指令
  （【角色沉浸要求】/【思维模式要求】）不再依赖 `setting/llm.ini` 的 `reasoning`
  开关——推理统一走 think 工具通道，风格指令改指 think 工具记录的内容；
  `reasoning` 只控制 API 是否返回隐藏推理块（`llm.py` / `data.py`）。
- **重复查询校验**（`retry_on_repeated_query`）：同一输入内再次调用查询工具时
  注入修复指令引导直接交付，防"结论已出仍重复拉取同一内容"（`pet.py` /
  `setting/app.ini.example`）。
- **空快照止步提示**：`read_current_text` 画面无文字时提示勿在短时间内反复查询，
  防 8 轮空转（`rmgame/bridge.py`）。
- **scan_think.py**：日志扫描工具，检测"查询了没留存"与重复查询。
- **LLM 调用日志**：`log/llm_<kind>_<时间戳>.txt`（round / merge / summary /
  wiki 重写），含推理内容提取渲染、缓存命中率、工具调用忠实呈现，与回合日志
  分离滚动保留（`logutil.py`）。

### 变更

- **上下文时间标注改为绝对时间**（`[MM-DD HH:MM]`，由消息绝对时间确定、逐字节
  稳定，历史段成为稳定缓存前缀）；当前时间锚点移到消息序列末尾；状态/环境/
  推进等动态段经 pre_time 在历史之后注入；合并摘要带绝对时间窗
  （`context.py` / `textutil.py`）。
- **update_state 不再进入思维链**：链内不再混入状态字段与持久 `inner_thought`
  （消除残句与「持久状态 / 回合内推理」通道混淆），其效果经工具结果回传
  （`pet.py` / `llm.py`）。
- **`fetish_analysis` 触发条件收紧**：仅当对方本人主动谈论自身性癖/欲望/心理、
  或明确请求分析时激活；仅谈论游戏剧情/角色设定不算；响应示例中性化
  （`skills = []`，消除每轮示范激活的诱导）（`data.py` / `llm.py`）。
- **`reasoning` 配置语义更新**：原生思考模式开关，推理内容由 think 工具承担
  （README / `setting/llm.ini.example`）。
- **文档重组**：设计文档（发布计划 / 协议 / 设计）移入 `docs/`（内部文档，
  不入公开仓库）；README 与配置模板同步更新。

### 修复

- 修复 1.0 默认配置（`reasoning = false` + `tool_choice = required`）下
  `fetish_analysis` 完全不可用（被白名单/技能清单/注入一并剔除）。
- 修复响应示例每轮向模型示范激活 `fetish_analysis` 的诱导问题。
- 修复相对时间标注随请求时刻漂移、切断历史段缓存前缀的问题。
- 修复 `read_current_text` 空快照引发连续空转查询的问题。
