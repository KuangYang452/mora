# -*- coding: utf-8 -*-
"""角色加载器 —— character/<slug>/

每个角色一个子目录（character/ 下含 card.json 的子目录即角色）：
- card.json      运行时角色卡（name/description/personality/scenario/
                 first_mes/mes_example/character_book）
- profile.ini    程序集成元数据（自称/称呼/语音色/初始状态/好感等级/
                 降级台词/问候模板/提示词片段）
- sprite.png     立绘
- prototype.json 原始参照卡（只读溯源）

支持多角色：
- list_characters() 列出全部 slug；
- load_character(slug=None) 加载指定角色，slug 未指定时随机；
- default_character() = slug 排序第一个（离线工具/自测可复现，非随机）；
- set_current() 设定当前激活角色，current() 返回（未设定时懒加载
  默认角色）。

占位符实例化（CharacterData.instantiate）：
  {{user}}/{{User}}   用户称呼（setting/user.ini ref）
  {{char}}/{{Char}}   角色显示名（profile [meta] display_name）
  {{char_name}}       同 {{char}}
  {{identity}}        完整身份（profile [meta] identity）
  {{char_self}}       角色自称（profile [voice] self_ref）
  {{user_ref}}        角色对用户的称呼（profile [voice] user_ref）
"""

import configparser
import copy
import json
import random
import re
from pathlib import Path

import settings

CARD_FILE = "card.json"
PROFILE_FILE = "profile.ini"
SPRITE_FILE = "sprite.png"
PROTO_FILE = "prototype.json"
IDENTITY_FILE = "identity.json"   # 【你的身份与世界观】段契约（见 character/SCHEMA.md）

# identity.json 必填字段（schema v2）：缺失 → CharacterError（启动即报错）
# v1 必填 + v2 必填（scenario/opening）；v1 文件兼容加载并回退 first_mes 提取
_IDENTITY_REQUIRED = ("identity.name", "identity.title",
                      "world.name", "world.definition")
_IDENTITY_REQUIRED_V2 = ("scenario.default", "opening")
_IDENTITY_SCHEMA = "identity/v2"
_IDENTITY_SCHEMA_V1 = "identity/v1"

# 可选字段类型检查表：字段路径 → 期望类型（list 元素须为 str）
_IDENTITY_LIST_FIELDS = (
    "identity.aliases", "identity.extra", "personality.core",
    "background.past", "background.present", "background.habits",
)
_IDENTITY_DICT_FIELDS = (
    "relationships", "abilities", "traits",
    "dialogue_style",
)


def _set_identity_path(data: dict, path: str, value) -> None:
    """按点分路径写入 identity dict（可选字段类型不符时的清理/置空）。"""
    keys = path.split(".")
    node = data
    for k in keys[:-1]:
        nxt = node.get(k) if isinstance(node, dict) else None
        if not isinstance(nxt, dict):
            nxt = {}
            node[k] = nxt
        node = nxt
    node[keys[-1]] = value

# 缺省好感等级（profile [affection_levels] 未配置时用）
_DEFAULT_LEVELS = [(0, 9, "厌恶"), (10, 29, "初遇"), (30, 49, "熟悉"),
                   (50, 69, "思慕"), (70, 89, "恋慕"), (90, 100, "亲爱")]


class CharacterError(Exception):
    pass


class CharacterData:
    """角色数据对象：card + profile + 资源路径。"""

    def __init__(self, slug: str, root: Path):
        self.slug = slug
        self.root = root
        self.card = self._load_card()
        self.profile = self._load_profile()
        self.identity_data, self.identity_warnings = self._load_identity()
        # v1 兼容回退：契约缺 scenario/opening 时从 first_mes 提取（v2 时不用）
        self._v1_scene, self._v1_opening = self._extract_first_mes_fallback()
        self.sprite = root / SPRITE_FILE
        self.prototype = root / PROTO_FILE

    def _load_card(self) -> dict:
        p = self.root / CARD_FILE
        if not p.exists():
            raise CharacterError(f"角色 {self.slug} 缺少 {CARD_FILE}")
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise CharacterError(f"角色卡解析失败（{p}）: {exc}")

    def _extract_first_mes_fallback(self) -> tuple:
        """v1 兼容：从 first_mes 提取 (场景, 开场台词列表)。

        与旧 llm.extract_opening 等价（剥 <StatusBlock> 与 <font> 台词块）。
        """
        raw = re.sub(r"<StatusBlock>.*?</StatusBlock>", "",
                     self.card.get("first_mes") or "", flags=re.S)
        lines = [re.sub(r'^[\u201c"]+|[\u201d"]+$', "", m.group(1)).strip()
                 for m in re.finditer(r"<font[^>]*>(.*?)</font>", raw, re.S)]
        lines = [x for x in lines if x]
        scene = re.sub(r"<font[^>]*>.*?</font>", "", raw, flags=re.S)
        scene = re.sub(r"<[^>]+>", "", scene)
        scene = re.sub(r"\n{3,}", "\n\n", scene).strip()
        return scene, lines

    def _load_identity(self) -> tuple:
        """加载并校验 identity.json（【身份与世界观】契约，见 character/SCHEMA.md）。

        必填字段缺失 → CharacterError（不静默兜底）；可选字段类型不符 →
        置空并记入 identity_warnings（debug.py 可查）。返回 (data, warnings)。
        """
        p = self.root / IDENTITY_FILE
        if not p.exists():
            raise CharacterError(
                f"角色 {self.slug} 缺少 {IDENTITY_FILE}（【你的身份与世界观】"
                "段的唯一输入契约，见 character/SCHEMA.md；可用 "
                "freeze_prototype.py 从原型生成模板）")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise CharacterError(f"identity.json 解析失败（{p}）: {exc}")
        if not isinstance(data, dict):
            raise CharacterError(f"identity.json 根节点必须是 JSON 对象（{p}）")
        warnings = []
        schema = data.get("schema")
        if schema == _IDENTITY_SCHEMA_V1:
            warnings.append(f"schema 为 {_IDENTITY_SCHEMA_V1}（旧版）：缺 scenario/opening，"
                            "回退从 first_mes 提取，建议升级为 "
                            f"{_IDENTITY_SCHEMA}（运行 freeze_prototype.py 适配）")
        elif schema != _IDENTITY_SCHEMA:
            warnings.append(f"schema 应为 {_IDENTITY_SCHEMA}，实际 {schema!r}")
        for path in _IDENTITY_REQUIRED:
            v = data
            for k in path.split("."):
                v = v.get(k) if isinstance(v, dict) else None
            if not (isinstance(v, str) and v.strip()):
                raise CharacterError(
                    f"identity.json 缺少必填字段: {path}（{p}，契约见 character/SCHEMA.md）")
        # v2 必填（scenario.default / opening）：v1 文件跳过（回退 first_mes）
        if schema == _IDENTITY_SCHEMA:
            for path in _IDENTITY_REQUIRED_V2:
                v = data
                for k in path.split("."):
                    v = v.get(k) if isinstance(v, dict) else None
                if path == "opening":
                    ok = isinstance(v, list) and v and all(
                        isinstance(x, str) and x.strip() for x in v)
                else:
                    ok = isinstance(v, str) and v.strip()
                if not ok:
                    raise CharacterError(
                        f"identity.json 缺少必填字段: {path}（{p}，契约见 character/SCHEMA.md）")
        for path in _IDENTITY_REQUIRED:
            v = data
            for k in path.split("."):
                v = v.get(k) if isinstance(v, dict) else None
            if not (isinstance(v, str) and v.strip()):
                raise CharacterError(
                    f"identity.json 缺少必填字段: {path}（{p}，契约见 character/SCHEMA.md）")
        # 可选字段类型检查：不符 → 置空 + 警告（不崩）
        for path in _IDENTITY_LIST_FIELDS:
            v = data
            for k in path.split("."):
                v = v.get(k) if isinstance(v, dict) else None
            if v is None:
                continue
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                warnings.append(f"identity.{path} 应为字符串数组，实际 {type(v).__name__}，已忽略")
                _set_identity_path(data, path, [])
        for path in _IDENTITY_DICT_FIELDS:
            v = data
            for k in path.split("."):
                v = v.get(k) if isinstance(v, dict) else None
            if v is None:
                continue
            if not isinstance(v, dict):
                warnings.append(f"identity.{path} 应为对象，实际 {type(v).__name__}，已忽略")
                _set_identity_path(data, path, {})
        # world.elements：必须是 {name, desc} 对象数组
        w = data.get("world")
        if isinstance(w, dict) and "elements" in w:
            if not isinstance(w["elements"], list) \
                    or not all(isinstance(x, dict) for x in w["elements"]):
                warnings.append("identity.world.elements 应为对象数组，已忽略")
                w["elements"] = []
        # scenario：必须是对象，default 非空字符串（variants 可选）
        sc = data.get("scenario")
        if sc is not None and not isinstance(sc, dict):
            warnings.append(f"identity.scenario 应为对象，实际 {type(sc).__name__}，已忽略")
            data["scenario"] = {}
        elif isinstance(sc, dict) and "default" in sc \
                and not (isinstance(sc.get("default"), str) and sc["default"].strip()):
            warnings.append("identity.scenario.default 应为非空字符串，已忽略")
            sc["default"] = ""
        return data, warnings

    def _load_profile(self) -> dict:
        """profile.ini → 嵌套 dict {section: {key: value}}。

        用无行内注释的 RawConfigParser（角色台词可能含 # / ;）。
        """
        p = self.root / PROFILE_FILE
        out = {}
        if not p.exists():
            return out
        cp = configparser.RawConfigParser()
        try:
            cp.read(p, encoding="utf-8")
        except (configparser.Error, OSError):
            return out
        for sec in cp.sections():
            out[sec] = {k: v for k, v in cp.items(sec)}
        return out

    # ---- 便捷属性 ----

    @property
    def meta(self) -> dict:
        return self.profile.get("meta", {})

    @property
    def display_name(self) -> str:
        return (self.meta.get("display_name")
                or self.card.get("name", "") or self.slug)

    @property
    def identity(self) -> str:
        return self.meta.get("identity") or self.display_name

    @property
    def self_ref(self) -> str:
        return self.profile.get("voice", {}).get("self_ref", "我")

    @property
    def user_ref(self) -> str:
        return self.profile.get("voice", {}).get("user_ref", "你")

    @property
    def speech_color(self) -> str:
        return self.profile.get("voice", {}).get("speech_color", "#336633")

    @property
    def affection_levels(self) -> list:
        """[(lo, hi, name), ...]（升序）；缺省用通用六档。"""
        raw = self.profile.get("affection_levels", {})
        levels = []
        for span, name in raw.items():
            m = re.match(r"^\s*(\d+)\s*[-~]\s*(\d+)\s*$", span)
            if not m:
                continue
            levels.append((int(m.group(1)), int(m.group(2)), name.strip()))
        levels.sort(key=lambda x: x[0])
        return levels or list(_DEFAULT_LEVELS)

    def level_for(self, affection: int) -> str:
        for lo, hi, name in self.affection_levels:
            if lo <= affection <= hi:
                return name
        return self.affection_levels[-1][2] if self.affection_levels else "初遇"

    def initial_state(self) -> dict:
        st = self.profile.get("state", {})
        try:
            aff = int(st.get("initial_affection", 20))
        except (TypeError, ValueError):
            aff = 20
        return {
            "affection": max(0, min(100, aff)),
            "inner_thought": st.get("initial_inner_thought", ""),
            "skills": [],
        }

    @property
    def scenario_default(self) -> str:
        """默认场景描写（【情境】段静态场景；v2 契约，v1 回退 first_mes 提取）。"""
        sc = self.identity_data.get("scenario") or {}
        if isinstance(sc, dict) and isinstance(sc.get("default"), str) \
                and sc["default"].strip():
            return sc["default"].strip()
        return self._v1_scene

    @property
    def opening(self) -> list:
        """开场台词流（上下文起始；v2 契约，v1 回退 first_mes 提取）。"""
        op = self.identity_data.get("opening")
        if isinstance(op, list) and op and all(isinstance(x, str) for x in op):
            return list(op)
        return self._v1_opening

    def fallback(self, key: str) -> str:
        """降级台词模板（未实例化；调用方自行 instantiate）。"""
        return self.profile.get("fallback", {}).get(key, "")

    def render_identity(self) -> str:
        """渲染【你的身份与世界观】段（契约模板，见 character/SCHEMA.md）。

        只做模板拼接、不删减内容；{{user}} 等占位符保留，由提示词组装后
        统一实例化（llm._instantiate）。
        """
        d = self.identity_data or {}
        idt = d.get("identity") or {}
        w = d.get("world") or {}
        lines = []

        head = (idt.get("name") or "").strip()
        title = (idt.get("title") or "").strip()
        if head and title:
            head += f"（{title}）"
        id_lines = [head] if head else []
        aliases = [a for a in (idt.get("aliases") or [])
                   if isinstance(a, str) and a.strip()]
        if aliases:
            id_lines.append("别名：" + "、".join(aliases))
        if (idt.get("species") or "").strip():
            id_lines.append("种族/存在形态：" + idt["species"].strip())
        if (idt.get("age") or "").strip():
            id_lines.append("年龄：" + idt["age"].strip())
        if (idt.get("appearance") or "").strip():
            id_lines.append("外观：" + idt["appearance"].strip())
        for e in idt.get("extra") or []:
            if isinstance(e, str) and e.strip():
                id_lines.append("· " + e.strip())
        if id_lines:
            lines.append("你的身份：" + "\n".join(id_lines))

        w_name = (w.get("name") or "").strip()
        w_def = (w.get("definition") or "").strip()
        if w_name or w_def:
            w_lines = [f"{w_name} —— {w_def}"
                       if w_name and w_def else (w_name or w_def)]
            for el in w.get("elements") or []:
                if isinstance(el, dict) and (el.get("name") or "").strip() \
                        and (el.get("desc") or "").strip():
                    w_lines.append(f"{el['name'].strip()}：{el['desc'].strip()}")
            lines.append("你所在的世界：" + "\n".join(w_lines))

        p = d.get("personality") or {}
        if isinstance(p, dict):
            core = [c for c in (p.get("core") or [])
                    if isinstance(c, str) and c.strip()]
            if core:
                lines.append("真实性格：" + "\n".join(core))
            if (p.get("surface") or "").strip():
                lines.append("表面性格：" + p["surface"].strip())

        bg = d.get("background") or {}
        if isinstance(bg, dict):
            for label, key in (("背景（过去）", "past"),
                               ("背景（现状）", "present"), ("习惯", "habits")):
                items = [x for x in (bg.get(key) or [])
                         if isinstance(x, str) and x.strip()]
                if items:
                    lines.append(label + "：" + "\n".join(items))

        for label, key in (("社会关系", "relationships"), ("能力", "abilities"),
                           ("特质", "traits"), ("对话风格", "dialogue_style")):
            sec = d.get(key)
            if isinstance(sec, dict) and sec:
                items = [f"{k}：{str(v).strip()}"
                         for k, v in sec.items() if str(v).strip()]
                if items:
                    lines.append(label + "：" + "\n".join(items))

        return "\n".join(lines)

    def greeting_template(self) -> str:
        return (self.profile.get("greeting", {}).get("template")
                or "{{user}}看向了{{char_name}}")

    @property
    def persona(self) -> str:
        return self.profile.get("prompt", {}).get("persona", "")

    @property
    def immersion_perspective(self) -> str:
        return self.profile.get("prompt", {}).get("immersion_perspective", "")

    # ---- 占位符实例化 ----

    def instantiate(self, text: str) -> str:
        if not text:
            return text
        uref = settings.user_ref()
        text = text.replace("{{user}}", uref).replace("{{User}}", uref)
        text = text.replace("{{char}}", self.display_name) \
                   .replace("{{Char}}", self.display_name)
        text = text.replace("{{char_name}}", self.display_name)
        text = text.replace("{{identity}}", self.identity)
        text = text.replace("{{char_self}}", self.self_ref)
        text = text.replace("{{user_ref}}", self.user_ref)
        return text

    def deepcopy(self) -> "CharacterData":
        c = CharacterData.__new__(CharacterData)
        c.slug = self.slug
        c.root = self.root
        c.card = copy.deepcopy(self.card)
        c.profile = copy.deepcopy(self.profile)
        c.identity_data = copy.deepcopy(self.identity_data)
        c.identity_warnings = list(self.identity_warnings)
        c.sprite = self.sprite
        c.prototype = self.prototype
        return c


# ---------------------------------------------------------------------------
# 角色目录管理
# ---------------------------------------------------------------------------

def _char_root(slug: str) -> Path:
    return settings.character_dir() / slug


def list_characters() -> list:
    """扫描 character/ 下所有含 card.json 的子目录 slug（排序）。"""
    base = settings.character_dir()
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir()
                  if p.is_dir() and (p / CARD_FILE).exists())


def load_character(slug: str = None) -> CharacterData:
    """加载角色；slug 未指定时随机选一个。"""
    slugs = list_characters()
    if not slugs:
        raise CharacterError(
            "character/ 下没有可用角色（缺少含 card.json 的子目录）")
    if slug is None:
        slug = random.choice(slugs)
    elif slug not in slugs:
        raise CharacterError(f"未找到角色「{slug}」（现有：{'、'.join(slugs)}）")
    return CharacterData(slug, _char_root(slug))


def default_character() -> CharacterData:
    """默认角色 = slug 排序第一个（离线工具/自测可复现，非随机）。"""
    slugs = list_characters()
    if not slugs:
        raise CharacterError("character/ 下没有可用角色")
    return CharacterData(slugs[0], _char_root(slugs[0]))


# ---------------------------------------------------------------------------
# 当前激活角色上下文
# ---------------------------------------------------------------------------

_current = None


def set_current(char: CharacterData) -> None:
    """设定当前激活角色（pet 启动时调用）。"""
    global _current
    _current = char


def current() -> CharacterData:
    """当前激活角色；未设定时懒加载默认角色（slug 排序第一个）。"""
    global _current
    if _current is None:
        _current = default_character()
    return _current


# ---------------------------------------------------------------------------
# 离线自测
# ---------------------------------------------------------------------------

def selftest() -> None:
    """契约结构自测：不依赖任何具体角色人设（1.0 起验证通用契约）。"""
    slugs = list_characters()
    assert slugs, "character/ 下应至少有一个角色（含 card.json 的子目录）"
    assert "demo" in slugs, f"原创演示角色 demo 应存在，实际 {slugs}"
    c = load_character("demo")
    # 角色卡与集成元数据（不验证具体人设内容，只验证结构）
    assert c.card.get("name"), "card.json 应含 name"
    assert c.display_name and c.self_ref and c.user_ref, (c.display_name, c.self_ref, c.user_ref)
    assert c.level_for(20) == "初遇" and c.level_for(95) == "亲爱", c.affection_levels
    st = c.initial_state()
    assert st["affection"] == 20 and isinstance(st["skills"], list)
    # identity.json 契约（【身份与世界观】/【情境】/开场，见 character/SCHEMA.md）
    assert c.identity_data.get("schema") == "identity/v2", c.identity_data.get("schema")
    assert c.identity_data["identity"]["name"], "identity.name 必填"
    assert c.identity_data["identity"]["title"], "identity.title 必填"
    assert c.identity_data["world"]["name"] and c.identity_data["world"]["definition"]
    assert not c.identity_warnings, c.identity_warnings
    rid = c.render_identity()
    assert c.display_name in rid and "{{user}}" in rid, \
        "渲染应含显示名与 {{user}} 占位符（组装时统一实例化）"
    # scenario / opening 契约（v2）：默认场景非空、开场台词流非空
    assert c.scenario_default, "scenario.default 必填（v2 契约）"
    assert c.opening and all(isinstance(x, str) and x.strip() for x in c.opening), c.opening
    assert "<font" not in c.scenario_default and "<StatusBlock>" not in c.scenario_default, \
        "场景不应残留标签"
    t = c.instantiate("{{char_self}}是{{identity}}，{{user_ref}}你好，{{user}}再见")
    assert c.self_ref in t and c.identity in t and c.user_ref in t, t
    assert "对方" in t, "{{user}} 应实例化为 user.ini 的称呼"
    assert c.fallback("silent") and c.greeting_template()
    # 随机加载：多角色时不会崩
    r = load_character()
    assert r.slug in slugs
    # 当前角色上下文：未设定时是默认（第一个），设定后可切换
    cur = current()
    assert cur.slug == slugs[0], cur.slug
    set_current(c)
    assert current().slug == "demo"
    print(f"[character.selftest] 通过 ✓ 角色 {len(slugs)} 个：{'、'.join(slugs)}")


if __name__ == "__main__":
    selftest()
