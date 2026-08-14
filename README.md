# LLM 桌面宠物框架（桌宠莫拉 v1.0）

一个 LLM 驱动的桌面宠物框架：角色常驻你的电脑桌面，通过 OpenAI 兼容 API
（DeepSeek / OpenAI / Ollama 等）驱动对话、状态演化与工具调用。

**角色数据由你自备**：仓库附带原创演示角色 `character/demo/`（图书管理员精灵
「娓娓」）用于开箱即跑；导入自己的角色卡（SillyTavern 兼容）见「角色」章节。
仓库不包含任何第三方版权角色或游戏数据。

## 架构

```
pet.py          桌面宠物 GUI：透明置顶、拖拽、气泡、动画、工具执行、agent 循环
llm.py          语义化提示词组装 + OpenAI 兼容客户端 + 原生工具调用解析
                （兼容旧文本 JSON 兜底）+ 状态结算；
                统一 LLM 调用入口 call_llm（唯一接口，自动写调用日志）；
                全部 LLM 调用经全局锁串行排队（同步阻塞，后续调用等待）
settings.py     配置加载器：setting/*.ini（app 运行配置 / llm 连接 / user 称呼）
                + 路径中心（唯一配置入口）
character/      角色包：character/<slug>/ 一个角色一个目录
                （card.json 角色卡 + identity.json 身份/世界观契约 + profile.ini
                集成元数据 + sprite.png 立绘 + prototype.json 原型副本）；
                load_character / current() 加载当前角色
tools.py        工具注册表（ToolSpec 单一来源：schema / 语义化清单 / 分类标记）
textutil.py     纯文本清洗（时间前缀 / 括号旁白剥离），无任何依赖
data.py         程序逻辑数据层：技能定义 / 游戏规则 / 好感度变化边界与 apply_delta
                （不含角色数据、不含运行配置、不含工具清单）
notes.py        角色私有笔记工具执行体：专属目录 runtime/notes/ 内文本 CRUD
context.py      上下文管理：会话历史 / 窗口裁剪 / 合并归档 / messages 组装
persist.py      状态与对话历史持久化（runtime/pet_session.json，原子写）
logutil.py      回合日志（最近 10 条）+ 通用 LLM 调用日志 llm_<kind>_*.txt
debug.py        调试工具：查看最终提示词 / LLM 配置 / 状态 / 日志
freeze_prototype.py  固化工具：SillyTavern 角色卡原型 → character/<slug>/
setting/        LLM 配置（llm.ini）/ 运行配置（app.ini）/ 用户称呼（user.ini），
                仓库只提交 *.example 模板，真实配置不入库
rmgame/         RPG Maker 点评工具包（discovery/extract/wiki/monitor/cdp/ocr/
                bridge/facade 等，见下方专章）
```

三条架构约定：

1. **配置与数据分层，各自只有一个生效位置**。运行配置（外观/行为/开关）在
   `setting/app.ini`、LLM 连接配置在 `setting/llm.ini`、用户称呼在
   `setting/user.ini`（`settings.py` 唯一加载入口）；角色数据（角色卡/初始状态/
   好感等级/自称/称呼/语音色/立绘）在 `character/<slug>/`（character 包加载）；
   程序逻辑规则（技能定义/游戏规则/好感度变化边界）在 `data.py`，工具注册表在
   `tools.py`。代码内不设任何兜底默认值，也不存在环境变量通道 —— 写错位置
   不会"悄悄不生效"，而是启动时直接报错。变量与对话历史持久化到
   `runtime/pet_session.json`（`persist.py`，路径由 `settings.save_dir()` 中心化）：
   每次回合结束与退出时保存（含 `saved_at` 与每条消息的时间标注），启动时恢复；
   存档缺失/损坏则回退初始状态与开场白。发往 LLM 的消息会剥离时间字段，但角色
   **具备时间感知**：每回合【当前状态】注入当前时间；恢复存档后重启，开场消息会
   附上"距上次对话 X 小时/天"的时间流逝描述。组装提示词时，上下文开头标注当前
   时间，历史消息标注相对时间 —— 前缀**只加在 user 消息上**（`[5分钟前]` 半角
   方括号元信息样式），assistant 消息保持纯台词，避免模型把「时间标注 + 台词」
   当成自己的发言格式模仿进输出。模型输出经 `strip_time_prefix` 硬清洗兜底。
   上下文过长时自动**合并归档**：未合并原文最多保留
   `CONFIG["context_keep_recent"] + CONFIG["context_keep_mid"]` 条（默认
   10 + 20 = 30）；超过后由 LLM 把最旧部分压缩为一条带时间标注的**新合并结果**，
   上下文回落到**最近 10 条原文 + 1 条合并结果**；被合并的原始消息与摘要归档
   （`archives`），可用 `query_archive` 工具查询。
2. **提示词完全语义化**。交付给 LLM 的系统提示词只有自然语言叙述：
   状态用「此刻你与对方的关系：好感度 20/100，处于『初遇』阶段……」这样的
   句子描述，不暴露任何代码结构。
3. **LLM 输出全部走原生函数调用，权限低于硬编码**。模型通过 OpenAI 兼容 API 的
   **原生 function calling** 通道输出：**台词也是工具**（调用 `say` 工具说出），
   状态更新通过调用 `update_state` 工具完成，模型没有任何不通过工具就能输出的
   通道（堵死括号旁白与"演的查询"）。它可以自主调整**动态数据**（内心想法、
   情绪、动作），但对**数值型状态**（好感度）只能给出变化趋势
   `affection_delta`（-5 ~ +5），最终数值由硬编码规则结算，无法直接写绝对值；
   技能激活同样只经工具声明，程序做白名单过滤。旧文本 JSON 路径仅作解析容错
   兜底（模型未走工具时降级处理，不崩溃）。

## 角色（character/）

**角色数据 = 用户自备的 SillyTavern 兼容角色卡**，程序不捆绑任何版权角色。
仓库附带原创演示角色 `character/demo/`（图书管理员精灵「娓娓」，内容中性、
原创、无版权关联），用于开箱即跑与自测；放入自己的角色：

1. 把 SillyTavern 角色卡（`*.json`）命名为 `prototype/card.json`、立绘命名为
   `prototype/sprite.png`（目录自建）；
2. 运行 `python freeze_prototype.py` —— 从原型提取人设生成
   `character/<slug>/`（slug 取自角色名），并生成 `identity.json` 契约模板
   （建议人工精化）与 `profile.ini`（首次生成后保留精调）；
3. 重启桌宠即可使用新角色（多角色并存时 `list_characters()` 全量加载）。

角色目录结构：

```
character/<slug>/
├── card.json        运行时角色卡（name/description/personality/scenario/
│                    first_mes/mes_example/character_book）
├── identity.json    身份/世界观契约（schema v2，见 character/SCHEMA.md）
├── profile.ini      集成元数据（自称/称呼/语音色/初始状态/好感等级/降级台词）
├── sprite.png       立绘
└── prototype.json   原型卡副本（只读溯源）
```

### 角色内容契约（identity.json）

提示词**内容段**的唯一输入标准：`character/<slug>/identity.json`（schema v2，
定义见 `character/SCHEMA.md`），涵盖三段：

- **【你的身份与世界观】**：identity + world + personality / background /
  relationships / abilities / traits / dialogue_style
- **【情境】静态场景**：`scenario.default`（程序级 `data.SCENE` 非空时覆盖）
- **开场预置上下文**：`opening`（台词流，作为上下文起始，随历史被窗口裁剪）

必填字段（角色名/头衔/世界观名与定义/`scenario.default`/`opening`）缺失时启动
直接报错（CharacterError）；`identity/v1` 旧版兼容加载（警告 + 回退 first_mes
提取）。外部角色卡内容经适配器进入标准：`freeze_prototype.py` 从原型
`description` 按顶层小节提取关键字段、从 `first_mes` 提取场景与开场台词，生成
模板；已有 `identity.json` 时保留人工精调、仅校验角色名一致性。

## 快速开始

```bash
pip install requests pillow            # 基本依赖（OCR 另需 pytesseract + Tesseract）
copy setting\llm.ini.example setting\llm.ini   # 填写 base_url / api_key / model
copy setting\app.ini.example setting\app.ini
copy setting\user.ini.example setting\user.ini
python llm.py                          # 离线自测（无需 API Key）
python pet.py                          # 启动桌宠
```

## 配置（setting/）

**运行配置与 LLM 连接配置当且仅有一个生效位置：`setting/` 目录**，由
`settings.py` 加载（唯一配置入口）。仓库只提交 `*.example` 模板；复制为正式
文件名（`llm.ini` / `app.ini` / `user.ini`）后填写。

### LLM 连接（setting/llm.ini）

```ini
[llm]
base_url = https://api.deepseek.com/v1
api_key = sk-你的密钥
model = deepseek-chat
temperature = 0.95
max_tokens = 1024
reasoning = false
reasoning_effort = medium
```

- `base_url`：任意 OpenAI 兼容端点（DeepSeek / OpenAI / Ollama 本地
  `http://localhost:11434/v1` / Kimi / 通义等）。
- `api_key`：必填。`base_url`、`api_key`、`model` 缺一不可，缺了启动时直接报错。
- 不用环境变量，不写代码 —— 写在任何其他位置的配置都不会生效。
- `reasoning`：思考模式开关（DeepSeek API 的 `thinking.type`）；关闭时自动适配：
  不注入思维链提示词，并**禁用 `fetish_analysis`**（心理分析依赖推理链）。
- `reasoning_effort`：推理强度，合法值 `none` / `minimal` / `low` / `medium` /
  `high` / `xhigh` / `max`。
- 注意：**思考模式（`reasoning: true`）与 `tool_choice: "required"` 互斥**
  （API 返回 "Thinking mode does not support this tool_choice"）；想用强制工具
  模式只能关思考（`reasoning: false`，`app.ini` 的 `tool_choice = required` 即
  默认配套）。
- 用 `python debug.py --llm` 查看当前生效的配置（密钥脱敏显示）。
- 本工具自身不做内容过滤；第三方 API 服务商可能有自己的内容政策。

## 调试与日志

```bash
python debug.py              # 打印最终交付给 LLM 的完整 system prompt
python debug.py --llm        # 当前生效的 LLM 配置（密钥脱敏）
python debug.py --state      # 当前硬编码状态（好感度/等级映射）
python debug.py --list       # 列出 log/ 日志
python debug.py --last 3     # 查看最近 3 条回合日志全文
python debug.py --clean      # 清空 log/
```

每回合对话都会在 `log/` 生成一条 `round_时间戳.txt` 日志（完整交付消息、推理
内容、原始响应、解析结果、状态变化），自动滚动保留最近 **10 条**（开关：
`setting/app.ini` 的 `log_enabled`）。所有 LLM 调用统一经 `llm.call_llm`，
自动生成 `log/llm_<类型>_时间戳.txt` 调用日志（kind = `round` / `merge` /
`summary` / `wiki_discovery` / `wiki_rewrite`），保留最近 **50 条**。所有
LLM 调用与工具调用的交付内容和返回内容都会实时输出到控制台。

## 运行

```
python pet.py
```

## 使用

| 操作 | 效果 |
|---|---|
| 单击立绘 | 弹出聊天输入框（回车发送 / Esc 关闭） |
| 拖拽立绘 | 移动位置（气泡会跟随） |
| 右键立绘 | 菜单：聊天 / 好感度 / 退出 |
| 气泡 | 显示角色的回复（语音色来自角色包，打字机效果，自动消失，点击即关） |

## LLM 输出协议（function calling）

调用走 OpenAI 兼容 API 的**原生工具调用**（`tools` 参数），不再依赖文本 JSON。
**一切输出都必须通过工具完成**（`CONFIG["speech_as_tool"]`）：

- **台词**：通过调用 `say` 工具说出（参数 `text`）—— 1~3 句、总长不超过
  60 字，口语化，气泡只显示它；台词是"说出口的话本身"，不得含时间标注，
  也不得用括号写旁白/动作/内心想法（这些一律放进 `update_state` 工具的
  `inner_thought` / `emote` 字段）。`message.content` 仅作降级路径。
- **状态更新**：模型通过调用 `update_state` 工具完成：
  - `affection_delta`：好感度变化趋势，整数 -5~+5（越权值由硬编码限幅）
  - `inner_thought`：内心想法，LLM 可自主更新
  - `emote` / `bounce`：情绪表情与跳跃动作（害羞😳 得意😏 生气😠 愉悦😊
    戏谑😼 撒娇🥺）；气泡前显示对应表情，`bounce` 让立绘跳一下
  - `skills`：技能开关（`fetish_analysis` / `game_context` 等），程序只做
    白名单过滤并按需注入对应世界书内容；`game_context` 由环境段**自动激活**
- **强制模式**（`CONFIG["tool_choice"]="required"`，默认开）：普通回合要求
  模型必须调用至少一个工具；端点不支持 `required` 时（4xx）自动回退 `auto`
  （首次探测后缓存）。
- **意向-动作校验**（`retry_on_vague_query`，默认开）：游戏环境下，若模型
  台词出现查询意向却未调用任何查询工具，追加修复指令重试一次。**防死循环**：
  每子轮最多重试 1 次，重试结果无条件接受、不递归校验。
- **工具收尾校验**（`force_say_to_finish`，默认开）：本回合已调过工具但最终
  响应未调任何工具、直接 content 输出时，追加修复指令重试一次，强制引导回
  台词工具通道；首轮 content 直出是合法降级路径不重试。
- **工具注册表**（`tools.py`）：新增工具只需在 `tools.SPECS` 加一条
  `ToolSpec`（name/desc/schema/分类标记），schema、提示词清单、查询类与
  游戏世界类名字集合全部自动派生（`_build_tools` / `prompt_names` /
  `query_names` / `game_world_names`）；`pet._tool_result` 有特殊分发时加一行
  特判。现有工具：`say` + `update_state` + RPG Maker 点评工具（开关
  `CONFIG["rmgame_enabled"]`）+ `query_archive` 归档查询 + `mora_notes` 私有笔记。
- **RPG Maker 点评工具**（`rmgame/bridge.py` 执行体）：`discover_running` /
  `start_game` / `read_current_text` / `query_wiki` / `scan_game` /
  `read_raw_text` / `wiki_arbitrate` / `wiki_rebuild`，详见下文专章。
- **私有笔记工具**（`notes.py` 执行体）：`mora_notes` 在专属文件夹
  `runtime/notes/` 内自由读写文本（list/read/write/append/delete），内容不限
  主题；笔记名严格校验（拒绝路径分隔符/越界），越权操作在机制上不可能发生。

解析容错：模型未调用工具时（纯文本 / 旧 JSON 格式），客户端自动降级，把内容
当作台词显示，不崩溃；空响应会自动重试一次。**括号旁白硬清洗**：台词经
`strip_paren_annotations` 剥离完整配对的 `（…）/ (…)` 括号内容（最多 3 层
嵌套），`context.py` 组装 messages 时对历史 assistant 消息做同样的展示层清洗。

**多轮自主（agent 循环）**：一次对方输入最多触发 `CONFIG["agent_max_turns"]`
轮（默认 8）自主调用。模型每轮可调用任意已注册工具，程序执行后把结果
（语义化叙述）作为 `tool` 角色消息回传，直到调用 `say` 工具说出最终台词
（回合结束）或到达轮数上限。中间轮次若有台词会即时显示并累积，回合结束时
与最终台词合并为一条 assistant 消息进历史。到达上限仍未结束时，气泡给出
"还在忙碌"提示。

## 程序逻辑数据（data.py 一览）

（不含角色数据与运行配置 —— 角色数据在 `character/<slug>/`，运行配置在
`setting/`，工具注册表在 `tools.py`）

- `SCENE`：静态场景覆盖（程序级，可替换）—— 非空时覆盖契约
  `scenario.default`；空 = 使用契约默认场景
- `SKILLS`：技能定义（LLM 通过 `skills` 工具参数管理激活，白名单过滤；
  `fetish_analysis` 需模型自行激活；`game_context` 由游戏环境段**自动激活**）
- `GAME_RULES`：游戏上下文读取规则（**单一事实来源**，改规则只需改这一处）
- `AFFECTION_MAX_DELTA = 5`：单回合好感度变化边界
- `apply_delta`：好感度 delta 限幅结算（LLM 只能给趋势，数值由规则结算）
- 好感度等级映射在角色 profile（`character/<slug>/profile.ini` 的
  `[affection_levels]`）—— LLM 无法直接指定等级
- 点评工具开关 `rmgame_enabled`、CDP/OCR 开关等：`setting/app.ini`

## RPG Maker 点评工具（rmgame）

为桌宠增加"点评 RPG Maker 引擎游戏文本"的能力。设计文档：`RPG_MAKER_TOOL.md`。
新世代（MV/MZ）优先，老世代（XP/VX/Ace）扫描器预留。三个功能：

1. **扫描提取**：识别硬盘上的 RPG Maker 游戏（`Game.exe` + `data/` JSON），
   解析事件命令流（对话/选项/分支，含公共事件/战斗事件）→ `raw/<游戏>/`；
2. **wiki 概念库**：LLM 从 raw（地图/公共事件/战斗事件）提炼"概念"
   （角色/关系/主题/地点/设定）骨架 → `wiki/<游戏>/`；条目正文**按需懒构建**
   （查询时 LLM 重写，纯重写不含原文，带 `raw://` 引用可回溯原文）；
3. **实时读取**：`start_game` 以 `--remote-debugging-port` 注入启动游戏 →
   CDP 读取 `$gameMessage` 等内部状态 → `runtime/current.json` 语义化快照；
   CDP 不可用时自动降级 OCR（Tesseract）。部分游戏打包拒绝调试参数 —— 此时把
   `setting/app.ini` 的 `rmgame_cdp_enabled` 设为 `False` 即**纯 OCR 模式**。
4. **事件感知**：OCR 文本经归一化匹配 raw 精确条目 → 携带**事件完整上下文**
   → 事件切换时后台 LLM 生成**事件摘要**（懒构建缓存到 `runtime/event_summary/`）
   → 环境段展示【对方正在…】当前对话 + 事件摘要 + 事件前文。

**CLI**（`python -m rmgame.cli`）：

```
scan <目录> [--yes]          # 发现游戏并入游戏库（需确认）
scan --running [--yes]       # 发现运行中的 RPG Maker 进程并入游戏库
scan --approve <游戏>        # 升级 trust auto→user（解锁启动）
extract <游戏> [--all]        # 提取文本 → raw/
wiki <游戏> [--model M]       # 概念发现（LLM）→ wiki/ 骨架
query <游戏> <关键词> [--force]  # 查概念（pending 现场懒构建）
start <游戏> [--dry-run]      # 启动游戏（注入调试端口 / 纯 OCR 模式普通启动）
monitor <游戏> [--rounds N] [--ocr-only]  # 轮询 → current.json
current <游戏>                # 显示当前快照
ocr <游戏>                    # 直接 OCR 窗口文本（调试）
selftest                      # 离线自测（全链路断言，不碰真实数据）
```

**权限与依赖**：只能操作游戏库已注册的游戏；入库分两级信任 —— 运行中的
未注册游戏由 monitor 守护线程**自动发现入库**（`trust=auto`，可读但**不能
启动**），用户在桌宠 UI 内确认后升级 `trust=user` 才可 `start_game`；写入
仅限 `raw/`、`wiki/`、`runtime/`；CDP/OCR 只读进程与窗口，不碰游戏存档。
OCR 依赖：Tesseract OCR（含 `chi_sim` 语言包）+ `pip install pytesseract`。

## 故障排查

- 启动报「缺少 LLM 配置文件」或「缺少必填字段」→ 检查 `setting/llm.ini`
  是否存在（从 `llm.ini.example` 复制）、`base_url` / `api_key` / `model`
  是否都已填写
- 气泡显示「API 返回 401/402」→ 密钥无效或余额不足，换 key 或充值
- 立绘没出现 → 确认 `character/<slug>/sprite.png` 存在；控制台会打印具体错误
- 想验证安装是否正常：`python llm.py`（离线自测，无需 API Key）
- 想确认最终交付给 LLM 的提示词：`python debug.py`

## 版权与数据说明

- 本仓库（代码 + 文档 + 演示角色）为 MIT 许可；演示角色「娓娓」为原创内容，
  无第三方版权关联。
- **角色数据自备**：`character/` 除演示角色外不入库（`.gitignore`），导入的
  角色卡与人设完全由使用者负责其版权。
- **游戏数据不入库**：`raw/`、`wiki/`、`runtime/`（含会话存档、日志）均在
  `.gitignore` 中，仓库不包含任何第三方游戏文本或衍生内容。
- `setting/*.ini` 真实配置不入库（含 API Key），只提交 `.example` 模板。
