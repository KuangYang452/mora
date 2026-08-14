# RPG Maker 游戏文本点评工具设计（RPG_MAKER_TOOL）

> 状态：**已实施（M0-M5 全部完成）** —— 本文档为设计依据；实现以代码为准。
> 目标：为桌宠增加"点评 RPG Maker 引擎游戏文本"的能力，含三个功能：
> ① 扫描硬盘中特定 RPG Maker 游戏的文本 → `raw/`；② 从 `raw/` 构建
> 桌宠可查询的 wiki → `wiki/`；③ 实时读取正在运行的 RPG Maker 实例的
> 当前文本与所在目录。
>
> **实施后新增（设计文档之外，见 README 专章）**：
> - 事件摘要 LLM（`rmgame/summarizer.py`，懒构建缓存 `runtime/event_summary/`）
> - LLM 任务日志（`rmgame/llmlog.py` → `log/llm_*.txt`，摘要/wiki 编写调用）
> - 事件完整上下文（matcher 命中后按 event_id 聚合；`read_raw_text` 按条目 id 取全量）
> - `read_at` 心跳（环境段新鲜度基于最后成功读取，画面静止也保持有效）
> - 纯 OCR 模式（`CONFIG["rmgame_cdp_enabled"]=False`，游戏拒绝调试参数时用；
>   OCR 引擎由 winsdk 改为 Tesseract + pytesseract）
> - 环境段摘要优先 + 匹配失败不显示 OCR 噪声 + 有摘要保留事件前文
> - `game_context` 技能（环境段自动激活，引导桌宠先读再答）
> - EV 占位事件名过滤（`is_noise_speaker`，`EV001` 类说话人不展示）

---

## 1. 已确认的设计决策（interview 结论）

| # | 决策点 | 结论 | 影响 |
|---|---|---|---|
| D1 | 目标引擎世代 | **新世代优先（MV/MZ）**，老世代（XP/VX/Ace）扫描器预留接口 | raw 提取走 JSON 解析；老世代走 Ruby Marshal 预留 |
| D2 | 实时读取路径 | **CDP 调试端口注入为主，屏幕 OCR 兜底** | 桌宠须具备**启动游戏**的能力（CDP 须启动前注入参数） |
| D3 | 集成形态 | **B：独立守护进程 + 工具桥接** | 扫描/wiki/监控是常驻服务；轻量入口暴露给桌宠工具通道 |
| D4 | 文本范围 | **默认仅对话**，通过参数可调取其他文本（物品/技能/角色名等） | 提取器分"对话提取器"与"全量提取器"两档 |

---

## 2. 总体架构

三层结构，全部落在工程根目录（`工程根目录`）内：

```
┌─ 接入层（桌宠侧，已有架构的扩展点）
│   data.py  TOOLS 注册新工具（start_game / read_current_text / query_wiki / scan_game）
│   pet.py   _run_tools 分发到 rmgame.bridge
│   llm.py   工具 schema 组装从"仅 update_state"扩展为多工具
│
├─ 服务层（新包 rmgame/，独立可运行）
│   cli.py        命令行入口（调试/手动触发，不依赖桌宠）
│   discovery.py  扫描硬盘 → 游戏库注册（games.json）
│   extract.py    Data/*.json → raw/（默认对话，可选全量）
│   rewriter.py   LLM 重写管线：概念发现（预构建）+ 逐概念重写（懒构建）
│   wiki.py       wiki 目录/索引/引用管理（结构与缓存层，不含 LLM 逻辑）
│   monitor.py    守护进程：CDP 连接 + OCR 兜底 → runtime/current.json
│   bridge.py     给桌宠的工具执行体（与 monitor 交互的轻客户端）
│
└─ 数据层（工程根目录下）
    raw/        提取的原始文本（按游戏组织，原文层，可溯源）
    wiki/       LLM 重写后的概念条目（按游戏组织，整理层，不含原文）
    runtime/    运行时状态（current.json、监控日志、games.json 游戏库）
```

进程模型：`monitor.py` 设计为**可独立运行的守护进程**（`python -m rmgame.monitor`），
与桌宠解耦 —— 桌宠退出不影响监控；`pet.py` 启动时可按配置拉起它。所有组件
同时保留 CLI 入口用于独立测试。

---

## 3. 目录结构（落地形态）

```
工程根目录\
├── rmgame/                # 新工具包
│   ├── __init__.py
│   ├── cli.py             # 命令行入口（scan/build/wiki/monitor/status）
│   ├── discovery.py       # 游戏发现与注册
│   ├── extract.py         # 文本提取 → raw/
│   ├── rewriter.py        # LLM 重写管线：概念发现 + 逐概念重写（懒构建）
│   ├── wiki.py            # wiki 目录/索引/引用管理（结构与缓存层）
│   ├── monitor.py         # 守护：CDP + OCR → runtime/current.json
│   └── bridge.py          # 桌宠工具执行体
├── raw/                   # <game_slug>/ 按游戏组织
├── wiki/                  # <game_slug>/ 按游戏组织
└── runtime/
    ├── games.json         # 游戏库注册表（唯一来源）
    └── current.json       # 当前运行游戏的最新文本快照
```

游戏唯一标识 `game_slug`：由游戏名生成（小写 + 连字符），全链路以它为键。

---

## 4. 模块设计

### 4.1 discovery —— 游戏发现与注册

**扫描规则**（新世代优先，老世代预留）：

| 世代 | 判定特征 | 说明 |
|---|---|---|
| MV | `Game.exe` + `www/` 或 `data/` + `package.json`（含 `"main": "www/index.html"` 等） | 明文 JSON 在 `www/data/` 或 `data/` |
| MZ | `Game.exe` + `data/`（小写）+ `package.json` + `js/rmmz_*.js` | 明文 JSON 在 `data/` |
| 老世代（预留） | `Game.exe` + `Data/*.rvdata2` / `.rvdata` / `.rxdata` | Ruby Marshal 二进制，仅登记不解析 |

**接口**：`discover(root_dir, recursive=True) -> list[GameInfo]`
`GameInfo`：`{slug, name, exe_path, dir, engine("mv"|"mz"|"legacy"), data_dir, version}`

**入库**：扫描结果写入 `runtime/games.json`（人工确认过的才入库 —— 见 §7 权限）。
重复扫描时按 slug 合并，不重复入库。

### 4.2 extract —— 文本提取 → raw/

**输入**：游戏 `data/` 下 JSON（`Map001.json` …、`CommonEvents.json`、`System.json`、
`Items.json`、`Skills.json` 等）。

**对话提取器（默认）**：遍历每张地图的事件命令流（`events[].pages[].list[]`），
提取 **code=401（文本）** 与 **code=405（文本换行）** 的命令，按事件连续拼接成
完整对话段落；**code=102（选项）** 提取选项文本并入条目。事件名（
`events[].name`）作为 speaker 线索。

**全量提取器（参数 `--all`）**：额外提取 `Items/Skills/Actors/System` 中的
描述文本（`description`、`message` 等字段），`type` 标记为
`item/skill/actor/system`。

**raw 格式**（每地图一个 JSON，ID 稳定供 wiki 引用）：

```json
// raw/<slug>/maps/Map001.json
{
  "map_id": 1,
  "map_name": "序章·小镇",
  "entries": [
    {
      "id": "Map001.3.12",          // mapId.eventId.commandIndex（稳定）
      "event_id": 3,
      "event_name": "村长",
      "type": "dialogue",
      "speaker": "村长",             // 事件名；无可解析时为 null
      "text": "欢迎来到小镇……",
      "choices": ["好的", "拒绝"],
      "branch_texts": [],            // 分支后的文本（按需可展开）
      "raw": [401, 405]              // 命令码序列（调试）
    }
  ]
}
```

**去重**：连续 `401/405` 合并；相同文本跨地图保留（不同 id），wiki 阶段按
文本去重展示。**编码**：MV/MZ 的 JSON 是 UTF-8，直接读写。

### 4.3 wiki —— LLM 重写管线（概念为中心）

**目标**：wiki 是桌宠（LLM）按需查询的**整理层资料库** —— 内容全部由 LLM
从 raw **纯重写**（不包含原文），**以概念而非事件为中心组织**，每个概念条目
携带对 raw 的引用链接（`raw://`）以便溯源核对。三个硬约束：
① 不整库注入（按需读单条目）；② 纯重写（整理层不存原文，原文在 raw 层）；
③ 可定位（概念索引 + raw 引用）。

**设计决策（interview）**：
- D5 内容形态：**纯重写，不含原文**；
- D6 构建时机：**混合（C）** —— 概念清单（骨架）预构建，条目正文按需懒构建。

**目录结构**：

```
wiki/<slug>/
├── index.md            # 总览页 + 概念清单（骨架，来自概念发现）
├── index.json          # 机器索引：概念 → 状态(built/pending/error) + raw 引用
└── concepts/
    ├── 禁忌之光.md     # 概念条目（懒构建产物；仅 built 的条目落盘）
    └── ...
```

**概念条目示例（concepts/禁忌之光.md）**：

```markdown
# 概念：禁忌之光

## 概述
（LLM 纯重写：小镇居民对"光"的恐惧源自……，不包含原文台词）

## 相关角色
- 神秘少女 —— 与光的显现直接相关（raw://demo/Map001.json#Map001.7.12）

## 相关地点
- 序章·小镇（raw://demo/Map001.json）

## 剧情线索
- 第一章 3 处提及"光"的意象（raw://demo/Map001.json#Map001.3.12 等）
```

**raw 引用链接格式**：`raw://<slug>/<map_file>.json#<entry_id>`；地图级引用
省略 `#entry_id`。引用只做寻址 —— 需要核对原文时由工具侧解析引用、读取
raw 对应条目返回；引用文本不注入 wiki 条目。

**管线两阶段**：

**阶段 1 —— 概念发现（预构建，低成本）**
- 输入：raw 索引摘要 —— 每地图 `{地图名, 事件名列表, 条目 head 列表}`
  （每地图约 200-500 字符，体量可控），按地图数分批喂给 LLM（受上下文限制）；
- 输出：概念清单 `[{id, title, kind, 理由, refs[entry_id]}]`，
  `kind ∈ character / relationship / theme / place / lore`；
- **限数**：首版每游戏概念上限 50 条（提示词要求 LLM 按重要性截断）；不足的
  概念可在查询未命中时二次发现（懒发现），不预建；
- 产出：`wiki/index.md` + `wiki/index.json`（全部 `status=pending`）；
- 成本：每次 scan 触发一次，LLM 调用数 = ⌈地图数 / 每批地图数⌉，通常个位数。

**阶段 2 —— 逐概念重写（懒构建，按需烧成本）**
- 触发：`query_wiki` 命中 `pending` 概念 → 现场构建并缓存；
- 输入：该概念 `refs` 指向的 raw 条目全文（收集后分批，受上下文限制）；
- 输出：`concepts/<title>.md`（纯重写 + raw 引用链接）；
- 缓存：写入后 `index.json` 置 `status=built`；文件已存在则跳过（断点续跑）；
- 失败：单次调用重试 N 次；仍失败置 `status=error`，下次查询再试。

**LLM 配置**：复用 `setting/llm.ini` 的 API 来源与密钥（settings 模块）；重写任务可经
`CONFIG["rmgame_llm"]` 覆盖 model / temperature（重写建议低温度保证稳定），
未覆盖字段继承 `setting/llm.ini`。实现复用 `llm.ChatClient(llm_cfg=覆盖配置)`，
不另起客户端。

**query_wiki 定位算法（更新版）**：
1. query 匹配概念 `title` / `kind` / 地图名 → 定位概念条目；
2. 条目 `pending` → 现场懒构建（等待 LLM 重写返回）→ 返回条目；
3. 条目 `built` → 直接读文件返回；
4. 需核对原文（点评引用台词）时，解析 `raw://` 引用返回对应原文片段；
5. 无命中 → 返回"该游戏无此概念"。

**增量与幂等**：
- raw 变化（新地图/文本更新）：scan 按 mtime 更新 raw → 相关概念条目置
  `stale` → 下次查询重建；
- 概念发现可重跑（幂等，按 id 合并，不重复建条目）；
- 大游戏**不全量重写** —— 懒构建天然按需，只有被查询的概念才烧 LLM 成本。

### 4.4 monitor —— 守护进程（CDP 主 + OCR 兜底）

**启动注入**：桌宠经 `start_game` 启动游戏时，以
`Game.exe --remote-debugging-port=<port>`（nw.js 参数）拉起；端口按游戏固定
分配（如 9222 + slug 哈希），记录到 `runtime/games.json`。
⚠️ 风险：部分游戏打包会拦截启动参数（见 §7 R2）。

**CDP 读取**（主路径）：连接 `http://127.0.0.1:<port>/json` 获取调试页面 →
`Runtime.evaluate` 周期性（1s）执行 JS 读取游戏内部状态：

```js
// 目标变量（MV/MZ 全局对象，版本间有差异，逐版本适配）
$gameMessage.allText()      // 当前消息文本
$gameMap.mapId()            // 当前地图 id
$gameMap.displayName()      // 地图显示名
$gameVariables.value(i)     // 变量（可配置关注列表）
SceneManager._scene.constructor.name  // 当前场景（如 Scene_Map / Scene_Battle）
```

**OCR 兜底**（CDP 不可用/连接失败时）：定位游戏窗口（按进程名/窗口标题）→
`PrintWindow` 截屏 → OCR 识别对话框区域文本。
引擎选型（M3 阶段定）：优先 **Windows.Media.Ocr**（系统 API，零依赖，中文
依赖系统语言包）；备选 PaddleOCR（中文精度高，依赖重）。

**输出**：每次成功读取后原子写入 `runtime/current.json`（语义化快照）：

```json
{
  "game": "slug",
  "dir": "D:/Games/xxx",
  "map_id": 1,
  "map_name": "序章·小镇",
  "scene": "Scene_Map",
  "text": "欢迎来到小镇……",
  "updated_at": "2026-08-12T13:45:00+08:00",
  "source": "cdp"             // cdp | ocr
}
```

新对话检测：与上一次快照文本不同 → 更新 `updated_at`（供桌宠判断"有新内容"）。

### 4.5 bridge —— 桌宠工具桥接

新增 4 个工具（`data.py` TOOLS 扩展 + `llm.py` schema 多工具组装）：

| 工具 | 参数 | 行为 |
|---|---|---|
| `start_game` | `game`（slug 或名称） | 注入 CDP 参数启动游戏；返回启动状态与端口 |
| `read_current_text` | 无 | 读 `runtime/current.json`，返回语义化快照（含新对话标记） |
| `query_wiki` | `game`、`query`（概念/角色/地点/主题关键词） | 定位概念条目；`pending` 则现场懒构建后返回；含 `raw://` 引用可回溯原文 |
| `scan_game` | `game`、`all`（可选，默认 false） | 触发 extract + 概念发现（骨架）；长任务异步，返回进度句柄 |

工具执行体在 `rmgame/bridge.py`；`pet.py` 的 `_run_tools` 增加分发；
`llm.py` 的 `_build_update_state_tool` 扩展为 `_build_tools()`（update_state +
上述 4 个），工具结果回传沿用现有 `tool_result_text` 机制（泛化为按工具名
格式化结果，状态类工具仍走语义化状态叙述）。

---

## 5. 数据流

**离线流（准备资料）**：

```
扫描(discovery) → games.json 入库(人工确认) → scan_game 工具/CLI
   → extract → raw/<slug>/  → 概念发现（预构建骨架）
   → wiki/<slug>/（index + 全部概念 status=pending）
```

**在线流（点评闭环）**：

```
桌宠调用 start_game → monitor 以调试端口拉起游戏 → CDP 轮询
   → runtime/current.json（新对话时更新）
桌宠调用 read_current_text → 拿到快照
   → query_wiki 查概念 → pending 则现场懒构建（LLM 重写条目并缓存）
   → 参考概念条目 + 必要时解析 raw:// 回溯原文 → 输出点评台词
   （点评走桌宠既有台词通道，气泡显示；不新增 UI）
```

**可选增强（标注为后续迭代，不在 M0-M4 范围）**：monitor 检测到新对话时
通过文件标记通知 pet.py，桌宠在空闲时（agent 循环内）自主决定是否开口点评
—— 涉及打扰频率控制，默认**按需拉取**，不做自动开口。

---

## 6. 与现有架构的对接点（实现改动清单）

| 文件 | 改动 |
|---|---|
| `data.py` | `TOOLS` 增加 4 个工具定义；`CONFIG` 增加 `rmgame_enabled`、`monitor_auto_start`、`ocr_engine` 等 |
| `llm.py` | `_build_update_state_tool` → `_build_tools()`（多工具 schema）；`parse_llm_response` 支持多工具结果提取；`tool_result_text` 泛化 |
| `pet.py` | `_run_tools` 分发到 `rmgame.bridge`；启动时按配置拉起 monitor 进程 |
| `README.md` | 新增工具说明章节（实施后更新） |

原则约束（延续项目既有架构）：业务数据硬编码（游戏库除外 —— 它是运行时
产生的数据，写 `runtime/games.json`）；提示词保持语义化（wiki 概念条目、
运行时快照均为语义化叙述，不暴露代码结构）；权限低于硬编码（桌宠只能
启动已入库游戏，不能任意执行命令）。

---

## 7. 权限与安全

- **启动游戏 = 外部副作用**：仅允许启动 `runtime/games.json` 中已入库的游戏
  （白名单），`start_game` 不接收任意路径。
- **入库需人工确认**：`discovery` 只做扫描发现，写入 games.json 前由操作者
  确认（CLI 交互或桌宠气泡确认），防止桌宠自动把任意目录当游戏。
- **写入边界**：所有输出只写 `raw/`、`wiki/`、`runtime/`（工程根目录内）。
- **OCR/CDP 只读**：monitor 只读进程状态与窗口，不做任何写游戏内存/存档
  的操作（点评场景不需要，明确排除）。

---

## 8. 技术风险与对策

| # | 风险 | 对策 |
|---|---|---|
| R1 | CDP 端口被占用 / 多实例 | 端口按 slug 固定分配；启动前探测占用，冲突则提示 |
| R2 | 游戏打包拦截 `--remote-debugging-port` 参数 | 探测启动后 CDP 是否可连；失败自动降级 OCR 兜底，并在日志标注"CDP 不可用" |
| R3 | MV/MZ 内部全局变量名版本差异（`$gameMessage` API 变化） | 逐版本适配表（monitor 内维护）；JS 探测失败也降级 OCR |
| R4 | OCR 中文精度不足 | 优先 Windows.Media.Ocr（系统语言包）；备选 PaddleOCR，可配置切换 |
| R5 | 老世代引擎（预留接口）无法用 JSON 提取 | 仅登记不入库解析；后续按需实现 Ruby Marshal 提取器（独立模块，不动主链路） |
| R6 | 大游戏全量提取耗时 | `scan_game` 异步执行 + 进度句柄；raw/wiki 按地图增量构建 |

---

## 9. 实施里程碑

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | `rmgame/` 骨架 + discovery + extract（对话提取器） | ✅ 完成 |
| M1 | rewriter 概念发现（预构建骨架）+ wiki 结构/索引 + query_wiki 定位 | ✅ 完成 |
| M2 | rewriter 逐概念重写（懒构建）+ 缓存/断点续跑 | ✅ 完成 |
| M3 | monitor CDP：启动注入 + 读取 current.json | ✅ 完成（补充：`read_at` 心跳、事件上下文、进程端口发现） |
| M4 | OCR 兜底 + 降级链路 | ✅ 完成（引擎定为 Tesseract；补充 `rmgame_cdp_enabled` 纯 OCR 模式） |
| M5 | bridge 工具接入桌宠 + 点评闭环 + README | ✅ 完成（补充：`read_raw_text` 工具、事件摘要 LLM、LLM 任务日志、`game_context` 技能） |

每阶段跑 `rmgame/cli.py` 离线自测 + `llm.py` selftest 回归（当前均全绿）。
离线自测覆盖：发现/提取（含公共/战斗事件）/ wiki 骨架与懒构建 / monitor 快照
与 OCR 降级 / 事件摘要缓存 / LLM 任务日志隔离（selftest 不污染真实 log/）。

---

## 10. 未决问题（实现前或对应里程碑确认）

1. **游戏库目录**：首版扫描默认从哪个根目录开始？（建议 `CONFIG["rmgame_scan_roots"]` 配置化，默认空 = 手动指定）
2. **OCR 引擎**：M3 前定 —— 优先 Windows.Media.Ocr（零依赖）还是直接上 PaddleOCR（中文精度高）？
3. **点评触发**：确认默认"按需拉取"（桌宠或对方主动要求才读文点评），不自动开口？（§5 可选增强是否要）
4. **CDP 端口段**：默认 9222-9322 段按 slug 分配是否可接受？
