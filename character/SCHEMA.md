# 角色提示词内容契约 v2 —— identity.json

提示词**内容段**的唯一输入标准：`【你的身份与世界观】`、`【情境】`（场景）、
开场预置上下文（opening）。程序不直接解析角色卡（`card.json#description` /
`first_mes` 等外部格式），只认本契约；外部内容必须经适配
（`freeze_prototype.py` 生成模板 + 人工确认）进入本契约。

## 文件位置

`character/<slug>/identity.json`，每个角色一个。缺失或损坏 → 启动报错
（CharacterError，不静默兜底）。

## Schema（v2）

```json
{
  "schema": "identity/v2",
  "source": "card.json#description+first_mes",
  "identity": {
    "name": "角色名",
    "title": "头衔/身份",
    "aliases": ["别名"],
    "species": "种族/存在形态",
    "age": "年龄描述",
    "appearance": "外观描述",
    "extra": ["身份补充条目"]
  },
  "world": {
    "name": "世界观名称",
    "definition": "世界观定义",
    "elements": [
      {"name": "要素名", "desc": "要素描述"}
    ]
  },
  "personality": {
    "core": ["真实性格条目"],
    "surface": "表面性格描述"
  },
  "background": {
    "past": ["过去背景条目"],
    "present": ["当前活动条目"]
  },
  "agenda": "角色议程（可选）：长期动机/目标，字符串或字符串数组（数组按行渲染）",
  "relationships": {
    "<关系对象>": "关系描述"
  },
  "abilities": {
    "<能力名>": "能力描述"
  },
  "traits": {
    "<特质分类>": "特质描述"
  },
  "dialogue_style": {
    "<场景>": "对话示例/风格"
  },
  "affection_levels": {
    "<等级名>": "自然语言段落（与 profile [affection_levels] 区间名对齐；可选）"
  },
  "scenario": {
    "default": "默认场景描写（【情境】段的静态场景）",
    "variants": {
      "<变体名>": "可选：命名场景变体（供环境驱动切换，未启用时忽略）"
    }
  },
  "opening": ["开场台词1", "开场台词2"]
}
```

## 必填字段（缺失 → CharacterError）

- `schema`（`identity/v2`；`identity/v1` 兼容加载并警告，缺省 scenario/opening
  回退从 first_mes 提取）
- `identity.name` / `identity.title` / `world.name` / `world.definition`
- `scenario.default`（默认场景描写）
- `opening`（非空字符串数组，开场台词流）

其余字段可选，缺省视为空。类型错误按缺失处理并计入警告。

## 渲染规则

- 【你的身份与世界观】由程序按固定模板渲染（身份块 + 世界观块 + 扩展块），
  不删减内容。`agenda`（可选）渲染为「角色议程：…」块，紧跟背景（过去/现状/
  习惯）之后——让「为什么做这些事」紧跟「正在做什么」。
- 角色议程在**激活指令尾部贴近输出重复一行**（近因效应；正文唯一来源 = 契约
  `agenda`，两处同源不产生双源）——与内容模式裸标记同为约定 7 的「关键规则
  贴近输出重复一次」（模型有忽略长设定的偏置，议程只在身份段容易「知道但
  没执行」）。
- 【情境】静态场景 = `data.SCENE`（程序级覆盖，非空优先）> 契约
  `scenario.default`；场景为**空间描写**（角色所处的环境与氛围，非行为叙事），
  直接作为【情境】段全部内容（不追加 persona —— 其内容与 identity 契约的
  surface/relationships 重复；关系规则与两常驻对象声明属行为协议段「对话边界」）。
- 【好感等级规则】注入**当前好感等级**对应的内容，优先级：
  `affection_levels[当前等级]`（契约字段，自然语言）> 角色卡世界书条目
  （`好感等级:XXX`，原卡 YAML 风格，仅当前一级）。契约字段缺失时回退
  世界书，不报错。
- 开场预置上下文 = `opening` 数组，作为上下文文本流起始（assistant 消息），
  随历史被窗口裁剪。
- 占位符（`{{user}}` 等）在提示词组装后统一实例化。

## 人称规范（场景与叙述的代词约定）

提示词分两层，各有代词约定：

- **场景层（空间描写，舞台说明性质）**：指代角色一律用 `{{char}}`
  （实例化 = 显示名），**不用「你」**——空间感描写是客观叙述，第二人称
  会造成「你身处于…你端坐于…」的粘连；也不用「她」（原卡 first_mes 的
  第三人称残留，易与对话叙述混淆）。场景只写环境、氛围与静态姿态，
  不写动作叙事线（起身/踱步/凑近等过场动作随 first_mes 台词剥离）
- **对话层（行为指令与关系叙述）**：指代智能体用「你」，指代用户用
  `{{user}}`（实例化值 = setting/user.ini 的 ref）
- 对话示例、台词引用属于角色的口头话语，保留口语原样（「你」「杂鱼」等），
  不用占位符替换，也不得带括号旁白（与「台词不得含括号旁白」红线一致）
- 台词示例在契约中以**示范语料条目**呈现（`对话示例（参考语气与风格，不必照搬原文）：`
  后接 `- "…"` 列表），与行为叙述分离——明确其参考样本地位，避免模型把
  示例当模板照抄、泛化变差
- 桌宠形态（悬浮立绘/气泡说话）不在提示词中声明——模型只负责台词与状态
  输出，展示层由 GUI 承担，形态声明对模型行为无增益

## 世界书好感等级条目 → 契约 affection_levels（转化格式）

原卡世界书条目为 YAML 结构（`behavioral_patterns:` / `dialogue_examples:` /
`- 列表项`），注入提示词会暴露数据结构、与全篇自然语言风格割裂，且台词
示例可能自带括号旁白（与输出红线冲突）。适配到契约时按以下模板人工重写：

1. 标题行 `等级名 (English):` → 等级名作为 `affection_levels` 的 key
2. `behavioral_patterns:` 列表项 → 前缀「在这个阶段，你会表现出：」，
   列表项用顿号连接成一句叙述；对象用 `{{user}}`
3. `dialogue_examples:` 列表项 → 独立为**示范语料条目**：换行后接
   「对话示例（参考语气与风格，不必照搬原文）：」，逐条以 `- "…"` 列表呈现；
   删除其中的括号旁白（如 `(露出嫌恶的表情)`），保留口语原样（不用占位符）
4. 其他未知字段 → 前缀「此外：」，内容原样；嵌套结构展开为逗号句

示例（恋慕）：

```
YAML 原条目：
恋慕 (Love):
    behavioral_patterns:
      - 极度傲娇
      - 吃醋但死不承认
    dialogue_examples:
      - "杂鱼今天怎么这么晚才来...吾辈可是一点都不想{{user}}哦!❤️"

→ 契约字段：
"恋慕": "在这个阶段，你会表现出：极度傲娇、吃醋但死不承认。
对话示例（参考语气与风格，不必照搬原文）：
- \"杂鱼今天怎么这么晚才来……吾辈可是一点都不想{{user}}哦！❤️\""
```

## 适配规则（外部内容 → 标准）

1. `freeze_prototype.py` 从原型角色卡生成模板：
   - `world_info` → `world`；`base_info` → `identity`
   - `life_story` / `present` → `background` / `personality`（+ 外观）
   - 其余小节原文保留（`_raw`）
   - `first_mes` → `scenario.default`（剥 `<font>` 台词块与 `<StatusBlock>`
     后的叙述文本）+ `opening`（`<font>` 台词块逐条）
2. 已有 `identity.json` 时：**不覆盖**（人工精调数据优先），仅校验角色名
   与原型一致，不一致则警告。
3. 校验器：`character` 包加载时执行（必填校验 + 类型校验 + v1→v2 迁移
   提示），警告汇总到 `CharacterData.identity_warnings`，由 `debug.py` 可查。
