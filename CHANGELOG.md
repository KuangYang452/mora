# Changelog

本项目的版本记录（格式约定见 docs/VERSIONING.md，版本号唯一来源
`settings.VERSION`）。语义化版本 MAJOR.MINOR.PATCH，发布打 tag `v<版本>`。

## [Unreleased]

### 新增（1.2 易用性，见 docs/1.2_USABILITY_PLAN.md）

- **启动器控制台**（`launcher.py`）：依赖自检（缺失一键 pip 安装）、LLM 配置检查、
  一键启动桌宠（子进程，pythonw 无控制台黑窗）；窗口/任务栏带图标
  （`assets/launcher.ico`，`make_icon.py` 从角色立绘生成）；支持一键创建桌面
  快捷方式（带图标）；下载即用入口 `启动莫拉.lnk`（根目录便携快捷方式，
  `make_shortcut.py` 生成，相对路径目标随目录迁移可用）→ `start_pet.vbs` →
  `launcher.py`，另附 `创建桌面快捷方式.bat` + `requirements.txt`。
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

### 修复

- **修复控制台窗口不定时闪现**：pythonw（无控制台）环境下，外部命令（PowerShell
  进程枚举、OCR、快捷方式创建、pip 安装等）每次调用都会新建控制台窗口一闪而过
  （此前 python.exe 启动时挂父控制台不显现）；统一加 `CREATE_NO_WINDOW`
  （`rmgame/monitor.py` / `rmgame/ocr.py` / `launcher.py` / `make_shortcut.py`）。

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
