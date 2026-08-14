# 新游戏入库可操作性设计（GAME_ONBOARDING_DESIGN）

> 状态：**已实施（M6a-M6f 完成）** —— 本文档为方案决策与落地依据；实现以代码为准。
> 关联：`RPG_MAKER_TOOL.md`（M0-M5 设计依据）、`rmgame/discovery.py`、
> `rmgame/monitor.py`、`rmgame/bridge.py`、`rmgame/cli.py`、`pet.py`。

---

## 1. 背景与问题

**现状**：游戏入库只有一条路径 —— 人工跑
`python -m rmgame.cli scan <目录>`，交互确认后写入 `runtime/games.json`
（唯一来源）。桌宠侧 7 个工具（`start_game` / `read_current_text` /
`query_wiki` / `scan_game` / `read_raw_text` / `wiki_arbitrate` /
`wiki_rebuild`）全部先经 `_resolve_game()` 查 `games.json`，未注册一律拒绝。

**痛点**（实测）：**用户手动运行一个未入库的新游戏，桌宠完全无感知**——
原因链：

1. `pet.py::_maybe_start_monitor()` 启动的守护线程 `monitor_loop_all(games)`
   只轮询 **games.json 中已注册**的游戏；新游戏不在列表里，没人给它写
   `runtime/current.json` 快照；
2. 桌宠调 `read_current_text` 读到的要么是「无快照」，要么是**上一个被
   监控游戏的旧画面**（当前为猎妻迷宫）—— 屏幕上是新游戏，桌宠看到的
   却是旧房间；
3. 桌宠工具表里没有「发现/注册」类工具，也没有对任意窗口的 OCR 通道；
4. `monitor.py` 其实**已经能枚举运行中的 `Game.exe` 进程**
   （`discover_running_ports()`，PowerShell 枚举 + 解析 CDP 端口），但只
   用来匹配已注册游戏的端口，从没用它发现新游戏。

**目标**：让「新游戏入库」可操作 —— 用户正在玩的新游戏，桌宠应能
**自动感知**，并以**最小人工动作**完成入库，进而获得完整点评能力。

---

## 2. 设计目标与约束

| # | 目标 | 说明 |
|---|---|---|
| G1 | 自动感知 | 运行中的 RPG Maker 游戏（`Game.exe` + 引擎特征）自动被桌宠看到，无需先入库 |
| G2 | 最小确认 | 人工动作收敛到「解锁启动」一个点；确认途径 ≥ 1 条（CLI 必做，气泡可选） |
| G3 | 权限不倒退 | 沿 §7 原则：执行外部程序（`start_game`）必须人工确认；自动发现不产生任意路径执行 |
| G4 | 兼容旧库 | 现有 `games.json`（无 trust 字段）读取后语义不变，不丢字段 |
| G5 | 零额外轮询开销 | 自动发现复用 monitor 已有的进程枚举轮（`PORT_REFRESH_ROUNDS`），不新增 PowerShell 调用 |

**架构约定**（延续项目既有三层）：`runtime/games.json` 仍是游戏库唯一来源；
自动发现写入的也是它；`raw/`、`wiki/`、`runtime/` 仍是全部写入边界。

---

## 3. 方案决策

候选三条路径，评审结论如下：

| 方案 | 做法 | 评价 |
|---|---|---|
| **A. 自动感知 + 信任分级**（**采用**） | monitor 守护线程自动发现运行中的新游戏 → 自动入库（`trust=auto`，只读能力）→ 人工确认后升级 `trust=user`（解锁启动） | 可操作性最强；安全边界精确收敛到「执行 exe」唯一动作；改动集中在现有轮询循环内 |
| B. 纯 CLI 半自动 | 只加 `scan --running` 列出运行中候选，人工确认入库 | 改动最小，但桌宠在入库前仍看不到新游戏，痛点没解决 |
| C. 桌宠工具直接注册 | 新增 `register_game` 工具，LLM 可写 `games.json` | 违反 §7「入库需人工确认」；LLM 幻觉/诱导风险不可控，**否决** |

**核心洞察**：原设计「入库需人工确认」的**真实保护对象是"启动任意程序"**，
不是"读取运行中游戏的文本"。自动发现的对象是**用户本人正在运行的
`Game.exe` 且目录通过引擎硬特征判定**（`package.json` + `data/` +
`js/rmmz_*.js` 等），误判概率极低；自动入库只解锁**只读/写工程根目录**的
能力，`start_game`（唯一执行外部 exe 的动作）仍锁在人工确认之后。

**信任分级**（`GameInfo.trust`）：

| trust | 来源 | 桌宠能力 | 启动 |
|---|---|---|---|
| `auto` | monitor 自动发现运行中游戏 | `read_current_text` / `scan_game` / `query_wiki` / `read_raw_text` / `wiki_arbitrate` / `wiki_rebuild`（全只读/写工程根目录） | **禁止** `start_game` |
| `user` | CLI `scan` 交互确认 / `--yes` / `scan --approve` | 全部能力 | 允许 |

旧库无 `trust` 字段 → 反序列化默认 `user`（历史记录均为人工入库，安全）。

---

## 4. 总体架构

感知链路复用现有守护线程，四阶段闭环：

```
┌─ 感知（monitor 守护，已有轮询循环内扩展）
│   枚举运行中 Game.exe（一次 PowerShell）→ 目录引擎判定
│   → 未入库的 → 自动写入 games.json（trust=auto, last_seen=now）
│   → 轮询列表动态刷新（含新游戏）→ 写 runtime/current.json
│
├─ 识别（桌宠新工具 discover_running）
│   运行中游戏清单：名称/引擎/目录 + 入库状态（已入库/候选）
│   → 桌宠可主动感知「有个新游戏在跑」
│
├─ 确认（人工，最小动作）
│   CLI: python -m rmgame.cli scan --approve <slug>   （auto → user，解锁启动）
│   CLI: scan <目录> --yes 仍是全量人工入库路径（trust=user）
│   （可选 M 阶段）pet 气泡确认：检测到新游戏时弹气泡，点击升级
│
└─ 消费（既有能力）
    trust=user 后：桌宠 start_game 高质量 CDP 启动 → 完整点评闭环
```

**进程模型不变**：守护线程在 `pet.py` 内（`_maybe_start_monitor`），异常
不影响桌宠；自动发现随守护线程运行，桌宠退出即停。

---

## 5. 模块设计

### 5.1 discovery.py —— 数据模型与注册

**`GameInfo` 新增字段**：

```python
trust: str = "user"    # "auto" | "user"；旧库缺省 "user"
last_seen: str = ""    # 运行中发现时间（ISO）；人工入库可空
```

`from_dict` 兼容：`kwargs["trust"] = d.get("trust") or "user"`（同 launch_mode
的缺省模式）。

**新增接口**：

- `discover_dir(exe_path) -> GameInfo | None`：对**单个** `Game.exe` 所在目录做
  引擎判定（复用 `_is_engine`），供进程枚举侧调用；不扫描、不写库。
- `auto_register(info: GameInfo) -> bool`：仅当 slug 未入库时写入
  `trust=auto` + `last_seen=now`（幂等，按 slug 合并，与 `register` 同原子写）。
  已存在（不论 trust）→ 不覆盖、不降级，返回 False。

`register()`（人工路径）不变：交互确认后入库，trust 保持 `user`。

### 5.2 monitor.py —— 自动发现与动态轮询

**`discover_running_ports()` 泛化 → `enumerate_running_games()`**：

```
一次 PowerShell：Get-CimInstance Win32_Process -Filter "Name='Game.exe'"
  取 ExecutablePath | CommandLine | ProcessId
Python 侧：按 exe_path 去重 → discover_dir() 判定 → 返回 list[GameInfo]
（CommandLine 中的 --remote-debugging-port=N 仍解析为端口表，供连接）
```

进程枚举函数可注入（`_enum_fn` 参数，离线自测 mock，风格同现有
`evaluator` / `ocr_fn`）。

**`monitor_loop_all` 改动**（关键）：

1. 每 `PORT_REFRESH_ROUNDS` 轮：一次 `enumerate_running_games()`，得到
   `running` 列表 + `ports` 端口表；
2. **自动入库**：`CONFIG["rmgame_auto_discover"]=True` 时，对 `running` 中
   未入库的（slug 不在库）调 `auto_register`；幂等保证只在首次写入；
3. **动态刷新**：每轮循环前 `games = load_games()` 重读（含新入库），
   而不是用启动时传入的固定列表；
4. 后续轮询逻辑不变（每游戏 `build_snapshot` → `write_current`）。

`auto_discover=False`（降级模式）→ 保持现状：只感知端口不写库。

### 5.3 bridge.py —— 新工具与信任检查

**新工具 `discover_running`**（`data.py` TOOLS 同步注册）：

```json
{
  "name": "discover_running",
  "desc": "查询当前正在运行的游戏（进程枚举+引擎识别）：返回每个游戏的名称/引擎/目录/入库状态（已入库 或 未入库候选）。未入库的提示确认路径。",
  "args": {}
}
```

语义化返回示例：

```
检测到 2 个运行中的 RPG Maker 游戏：
- 《猎妻迷宫》（MV）工程根目录\RPG游戏\猎妻迷宫 —— 已入库（user，可启动）
- 《新作迷宫》（MZ）工程根目录\新作 —— 未入库候选（auto）
  未确认的游戏：点确认气泡「✅ 允许」，或右键菜单「🎮 游戏」确认
```

**`start_game` 信任检查**：`_start_game` 前置判定
`getattr(g, "trust", "user") != "user"` → 拒绝并返回确认指引文本
（见上例末行）。这是**唯一**新增的权限闸门。

其余工具（`read_current_text` / `query_wiki` / `scan_game` /
`read_raw_text` / `wiki_arbitrate` / `wiki_rebuild`）对 trust 不做区分 ——
它们要么读 `current.json`（monitor 已确认在跑的游戏），要么在工程根目录内
读写（raw/wiki），要么读游戏目录文本（与 OCR 同源，用户已主动运行）。

### 5.4 cli.py —— 确认与状态

| 命令 | 行为 |
|---|---|
| `scan --running` | 列出运行中游戏（等价 discover_running 的 CLI 版），交互确认入库（trust=user） |
| `scan --approve <slug>` | 仅升级 `auto → user`（不动其他字段），打印确认结果 |
| `status` | 游戏行追加 `trust` 列（auto/user）+ 运行中标记 |

### 5.5 data.py / settings.py —— 配置

`setting/app.ini`（`data.py` CONFIG 同步键）：

```ini
rmgame_auto_discover = true   ; 自动发现运行中的未注册游戏并入库（trust=auto）
```

`pet.py::_maybe_start_monitor` 不变（自动发现内嵌 `monitor_loop_all`）；
monitor 拉起条件（`rmgame_enabled` / `monitor_auto_start`）沿用。

---

## 6. 数据流

**自动感知流**（用户手动启动新游戏）：

```
用户双击 Game.exe（无调试端口）
  → monitor 守护轮询到进程（PORT_REFRESH_ROUNDS 周期内）
  → enumerate_running_games → 引擎判定通过 → auto_register（trust=auto）
  → 轮询列表刷新 → build_snapshot（无端口 → OCR 兜底）
  → runtime/current.json（source=ocr）
桌宠 read_current_text → 看到新游戏画面文本（OCR 级质量）
桌宠 discover_running → 确认「新游戏《X》已感知，未允许启动」
  → 引导用户 CLI scan --approve X（或可选气泡确认）
  → trust=user → 桌宠 start_game（CDP 高质量启动）→ 完整点评闭环
```

**人工入库流**（未运行的新游戏，保持不变）：

```
python -m rmgame.cli scan <目录> [--yes] → 交互确认 → trust=user 入库
```

**启动流**（trust=user，沿用现有 auto/normal/bypass 探测记忆）。

---

## 7. 权限与安全

- **自动入库不执行任何程序**：只枚举进程（只读）+ 读目录特征文件 +
  写 `games.json`；危险动作 `start_game`（执行外部 exe）由 `trust=user`
  锁住，人工确认前桌宠无任何执行通道。
- **写入边界不变**：`raw/`、`wiki/`、`runtime/`（工程根目录内）。
- **OCR/CDP 只读**：与现状一致，不碰游戏存档。
- **确认即信任升级**：`scan --approve <slug>` 只认**已自动入库的 slug**，
  不接受任意路径/任意名字（无库内记录则报错），防注入式确认。
- **降级开关**：`rmgame_auto_discover=False` 完全关闭自动入库，退回现状。

---

## 8. 技术风险与对策

| # | 风险 | 对策 |
|---|---|---|
| R1 | 进程枚举 PowerShell 调用重 | 复用 `PORT_REFRESH_ROUNDS=30` 节流；自动发现与端口刷新同轮，**零新增调用** |
| R2 | 误判非 RPG 进程为游戏 | 引擎判定是硬特征（package.json + data/ + rmmz_js）；即便误入库也只解锁只读能力，且可被 `scan --approve` 前人工忽略 |
| R3 | 同名不同目录游戏 slug 冲突 | 沿用 `register` 按 slug 合并语义（已存在即跳过）；同名多版本需人工改名 —— 现状即如此，不扩大范围（见 §10 未决） |
| R4 | 自动入库游戏路径失效/被删 | `load_games` / 读取链路现有容错；进程消失 → monitor 读不到 → 快照不更新，无异常；记录保留作历史 |
| R5 | OCR 质量差（手动启动无调试端口） | 文档/工具返回引导：确认后由桌宠 `start_game` 换 CDP 高质量读取 |
| R6 | 确认途径单一（CLI） | CLI 必做；pet 气泡确认列为可选 M6f（需要 pet UI 回调） |

---

## 9. 实施里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| M6a | discovery：`trust`/`last_seen` 字段 + 旧库兼容 + `discover_dir` + `auto_register` + `approve` | ✅ selftest：字段往返、缺省 user、幂等、升级 |
| M6b | monitor：`enumerate_running`（可注入）+ `monitor_loop_all` 自动发现与动态刷新 | ✅ selftest：mock 枚举注入 → 自动入库 → 幂等不降级 |
| M6c | bridge：`discover_running` 工具 + `start_game` 信任检查；data.py/llm.py/settings.py/app.ini/pet.py 同步 | ✅ selftest：trust=auto 拒启动、状态标注；llm selftest：schema/开关过滤 |
| M6d | cli：`scan --running` / `scan --approve` / `status` trust 列与运行中标记 | ✅ selftest：approve 升级/无记录报错；status 手测 |
| M6e | selftest 全链路回归（含 trust 断言），不污染真实 runtime/ | ✅ `python -m rmgame.cli selftest` 全绿 |
| M6f | pet 气泡确认：发现新游戏弹气泡，点击「允许」升级 trust；右键菜单「🎮 游戏」兑底 | ✅ 已实施（`rmgame_confirm_bubble` 开关；selftest 覆盖回调链路；UI 手测见 README） |

---

## 10. 未决问题

1. **同名不同目录游戏**：是否需要在 slug 后追加目录 hash 区分？（现状按
   slug 合并，多版本游戏只能人工改名 —— 建议保持现状，不引入）
2. **自动入库记录的清理**：被自动入库但用户从不确认的游戏，是否提供
   `scan --remove <slug>` 删除？（默认保留作历史，不新增命令）
3. **气泡确认 UI**：✅ 已实施（M6f，见 §9）。
4. **信任降级**：用户主动删除 `trust=user` 游戏后，再次运行时自动发现会
   以 `auto` 重新入库（能力降级，安全）—— 是否希望记住「用户曾拒绝」？

---

## 附录：日文原版 + MTool 汉化场景（实测结论，2026-08-13）

针对「日文原版游戏 + MTool 动态汉化」的完整影响评估与验证结果：

**验证方法**：对淫乱轮轴（`[JP][ver1.11]`，日文 MV 游戏）bypass 启动
（nwjs SDK v0.29.0 + `--remote-debugging-port`），CDP 读到
`scene=Scene_Title`、`source=cdp` —— 端口 9246 通道完全打通；窗口显示为
**日文**（MTool 未注入，需用户手动加载翻译文件才显示中文）。

**结论**：
- **bypass + CDP 对日文游戏完全可用**：读到的 `$gameMessage` 是日文数据层
  （MTool 替换的是显示层）→ 与日文 raw 字符级匹配 ✓ → 摘要、事件上下文、
  wiki 溯源、精确引用链路**全部可用**（与中文游戏同质，仅 LLM 需读日文）。
- **两条路径的取舍**：
  - 桌宠 bypass 启动 → CDP 日文 → 完整点评能力，但画面为日文（MTool 不注入）；
  - 用户手动 MTool 启动 → 画面中文，但进程无调试端口 → 桌宠走 OCR（中文）
    → 与日文 raw 失配 → 摘要/事件定位断链。
- **已实施的配套优化（C）**：summarizer / rewriter（概念发现+重写）/ GAME_RULES
  三处提示词声明「原文可能为日文，先准确理解再处理；专名采用通行音译、
  首次出现附日文原文对照（如：プリムラ（普莉姆拉）），同一角色保持同一译名」。
- **未做的增强（可选立项）**：matcher 跨语言事件级定位（B）——中文 OCR vs
  日文 raw 时按「地图+事件顺序」或按需 LLM 语义匹配，解决 OCR 路径断链。

**已知边界（工具环境）**：在 Codewhale 工具会话中用 CLI 启动游戏时，
游戏进程随命令结束被清理（进程树生命周期）——「只要命令在跑（计时/轮询）
游戏就不退出」。桌宠（pet.py 常驻）生产环境下由 pet 进程管理，不受此限。
