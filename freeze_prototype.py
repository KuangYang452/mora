# -*- coding: utf-8 -*-
"""固化工具 —— 把原型角色卡固化为 character/<slug>/ 角色目录

用户自备的 SillyTavern 角色卡作为原型（程序不直接依赖），本脚本从原型提取
人设数据，生成角色目录 `character/<slug>/`（character 包加载的运行时结构）：

- card.json      运行时角色卡（name/description/personality/scenario/
                 first_mes/mes_example/character_book）
- prototype.json 原型卡副本（只读溯源，程序不读取）
- sprite.png     立绘副本（来自原型图）
- profile.ini    程序集成元数据（自称/称呼/初始状态/好感等级/语音色/
                 降级台词/问候模板/提示词片段）——**若已存在则保留**
                 （profile 是精调的程序集成数据，重固化不覆盖）

原型定位（find_proto）：优先 prototype/ 参照目录（把原型角色卡命名为
`card.json`、立绘命名为 `sprite.png` 放入），其次角色目录内 prototype.json
副本。

用法：
  python freeze_prototype.py            # 固化到 character/<slug>（slug 取自原型名或默认）
  python freeze_prototype.py <slug>     # 指定角色目录名

当原型角色卡被更新后，重新运行本脚本即可同步 card.json / prototype.json /
sprite.png；profile.ini 需要增删字段时手动维护。
"""

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROTO_DIR = ROOT / "prototype"
PROTO_CARD = PROTO_DIR / "card.json"
PROTO_SPRITE = PROTO_DIR / "sprite.png"
CHAR_DIR = ROOT / "character"

# 固化哪些字段（原型字段 → card.json 键）
FIELDS = ("name", "description", "personality", "scenario", "first_mes",
          "mes_example", "character_book")

# 默认 profile.ini（仅首次生成；已存在则保留）—— 值与 character/<slug>/profile.ini
# 的精调版本对齐的通用骨架，字段含义见 character/__init__.py 的 CharacterData。
DEFAULT_PROFILE = """\
; 角色元数据 —— character/<slug>
; 运行时人设来源：card.json（角色卡）+ 本 profile（程序集成元数据）。
; 占位符约定（程序统一实例化）：
;   {{char}}       角色名（card.json 的 name 字段）
;   {{char_name}}  显示名（本文件 [meta] display_name）
;   {{identity}}   完整身份（本文件 [meta] identity）
;   {{user}}       用户称呼（setting/user.ini 的 ref）
;   {{char_self}}  角色自称（本文件 [voice] self_ref）
;   {{user_ref}}   角色对用户的称呼（本文件 [voice] user_ref）

[meta]
slug = {slug}
display_name = {display_name}
identity = {identity}

[voice]
self_ref = 我
user_ref = 你
speech_color = #5b7d8c

[state]
initial_affection = 20
initial_inner_thought = 有新的访客来了，看看ta想聊些什么。

[affection_levels]
0-9 = 厌恶
10-29 = 初遇
30-49 = 熟悉
50-69 = 思慕
70-89 = 恋慕
90-100 = 亲爱

[fallback]
silent = （{{char_self}}只是安静地看着你）
thinking = （{{char_self}}沉默地看着你）
busy = （{{char_self}}似乎走神了…再叫你一次吧）
working = （{{char_self}}还在忙…下次再来听结果吧）

[greeting]
template = {{user}}走向了{{char_name}}

[prompt]
persona = 你保持自己的性格与说话风格，自然地与{{user}}交流；{{user}}是与你平等聊天的普通人。
immersion_perspective = 以{{identity}}的视角观察{{user}}
"""


def find_proto(slug: str = None) -> Path:
    """定位原型角色卡：优先 prototype/ 参照目录，其次角色目录内副本。"""
    if PROTO_CARD.exists():
        return PROTO_CARD
    if slug:
        fallback = CHAR_DIR / slug / "prototype.json"
        if fallback.exists():
            return fallback
    sys.exit(f"[错误] 找不到原型角色卡: {PROTO_CARD}"
             "（请把角色卡命名为 prototype/card.json，"
             "或提供已有角色目录内的 prototype.json 副本）")


def extract(proto_path: Path) -> dict:
    if not proto_path.exists():
        sys.exit(f"[错误] 找不到原型角色卡: {proto_path}")
    with proto_path.open(encoding="utf-8") as f:
        raw = json.load(f)
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    out = {}
    for key in FIELDS:
        out[key] = data.get(key, "")
    return out


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                    encoding="utf-8")


# ---------------------------------------------------------------------------
# 外部内容 → 契约适配（identity.json 模板生成）
# ---------------------------------------------------------------------------
# 原型角色卡的 description 是 SillyTavern 风格 YAML-ish 文本，缩进不规范
# （同级键 0/2/4 空格混杂、重复键），不做可靠自动解析；适配器按顶层小节
# 切分（`key:` 无缩进），用容错正则提取关键字段（base_info / world_info /
# life_story / present），其余小节原文保留，生成骨架模板后人工精化。


def _split_top_sections(text: str) -> dict:
    """按顶层小节切分（行首 `key:` 且无缩进）。"""
    sections = {}
    cur_key = None
    buf = []
    for line in (text or "").split("\n"):
        stripped = line.rstrip()
        m = re.match(r"^([A-Za-z_]+):\s*$", stripped)
        if m and not line.startswith(" "):
            if cur_key:
                sections[cur_key] = "\n".join(buf).strip()
            cur_key = m.group(1)
            buf = []
        elif cur_key is not None:
            buf.append(stripped)
    if cur_key:
        sections[cur_key] = "\n".join(buf).strip()
    return sections


def _sub_block(block: str, key: str) -> str:
    """提取 block 内 `key:` 之后的子内容。

    子内容收集到下一个**英文无值键行**（`key:`）为止 —— 中文子键行（如
    world_info.elements 的元素名）与 `key: value` 同行行均不触发边界。
    """
    lines = (block or "").split("\n")
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)([A-Za-z_]+):\s*$", line.rstrip())
        if m and m.group(2) == key:
            out = []
            for nxt in lines[i + 1:]:
                s = nxt.rstrip()
                if not s.strip():
                    out.append(s)
                    continue
                if re.match(r"^\s*[A-Za-z_]+:\s*$", s):
                    break
                out.append(s)
            return "\n".join(out).strip("\n").rstrip()
    return ""


def _kv_scan(block: str) -> dict:
    """扫描块内全部 `key: value` 键值对（容忍任意缩进，单行值）。"""
    out = {}
    for line in (block or "").split("\n"):
        m = re.match(r"^\s*([A-Za-z_]+):\s*(\S.*)$", line.rstrip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _list_items(text: str) -> list:
    """收集 `- item` 列表项（容忍任意缩进）。"""
    return [m.group(1).strip() for m in
            (re.finditer(r"^\s*-\s*(.+)$", (text or ""), flags=re.M))]


def _parse_elements(text: str) -> list:
    """解析 world_info.elements：`    名称:` + `      descriptions: 值`。"""
    out = []
    cur = None
    for line in (text or "").split("\n"):
        s = line.rstrip()
        m = re.match(r"^\s+([^:\s][^:]*):\s*$", s)
        if m:
            cur = {"name": m.group(1).strip()}
            out.append(cur)
            continue
        if cur is not None:
            md = re.match(r"^\s+descriptions?:\s*(.+)$", s)
            if md:
                cur["desc"] = md.group(1).strip()
                cur = None
    return [e for e in out if e.get("name") and e.get("desc")]


def build_identity_template(card: dict) -> dict:
    """从原型卡生成 identity.json 骨架（外部内容适配为契约 v1）。"""
    secs = _split_top_sections(card.get("description") or "")

    bi = secs.get("base_info", "")
    bi_kv = _kv_scan(bi)
    extra = _list_items(_sub_block(bi, "extra"))
    if (bi_kv.get("identity") or "").strip():
        extra.append(bi_kv["identity"].strip())
    identity = {
        "name": (bi_kv.get("name") or "").strip(),
        "title": (bi_kv.get("title") or "").strip(),
        "aliases": [],
        "species": "",
        "age": (bi_kv.get("age") or "").strip(),
        "appearance": "",
        "extra": extra,
    }

    wi = secs.get("world_info", "")
    wi_kv = _kv_scan(wi)
    elements = _parse_elements(_sub_block(wi, "elements"))
    world = {
        # world.name 无直接来源：取 elements 首项（通常即世界/图书馆名），
        # 缺失时留空（必填校验会报错，提示人工填写）
        "name": (elements[0]["name"] if elements else ""),
        "definition": (wi_kv.get("definition") or "").strip(),
        "elements": elements,
    }

    ls = secs.get("life_story", "")
    background = {
        "past": _list_items(_sub_block(ls, "past")),
        "present": _list_items(_sub_block(ls, "present")),
        "habits": [],
    }

    pr = secs.get("present", "")
    personality = {
        "core": _list_items(_sub_block(pr, "true_personality")),
        "surface": _sub_block(pr, "surface_personality"),
    }
    background["habits"] = _list_items(_sub_block(pr, "habits"))
    identity["appearance"] = _sub_block(pr, "appearance")

    template = {
        "schema": "identity/v2",
        "source": "card.json#description+first_mes",
        "identity": identity,
        "world": world,
        "personality": personality,
        "background": background,
        # 角色议程（可选）：长期动机/目标；原型无直接来源，留空待人工填写
        "agenda": "",
    }
    for src, dst in (("social_connections", "relationships"),
                     ("special_abilities", "abilities"),
                     ("character_traits", "traits"),
                     ("dialogue_examples", "dialogue_style")):
        if secs.get(src):
            template[dst] = {"_raw": secs[src]}

    # scenario / opening：从 first_mes 提取（剥 <StatusBlock> 与 <font> 台词块）
    fm_raw = re.sub(r"<StatusBlock>.*?</StatusBlock>", "",
                     card.get("first_mes") or "", flags=re.S)
    opening = [re.sub(r'^[\u201c"]+|[\u201d"]+$', "", m.group(1)).strip()
               for m in re.finditer(r"<font[^>]*>(.*?)</font>", fm_raw, re.S)]
    opening = [x for x in opening if x]
    fm_scene = re.sub(r"<font[^>]*>.*?</font>", "", fm_raw, flags=re.S)
    fm_scene = re.sub(r"<[^>]+>", "", fm_scene)
    fm_scene = re.sub(r"\n{3,}", "\n\n", fm_scene).strip()
    template["scenario"] = {"default": fm_scene, "variants": {}}
    template["opening"] = opening

    template["_notes"] = (
        "由 freeze_prototype.py 从原型 description + first_mes 适配生成的骨架；"
        "建议人工精化未结构化字段（aliases/species/appearance 等）。")
    return template


def main() -> None:
    slug = sys.argv[1] if len(sys.argv) > 1 else None
    proto_path = find_proto(slug)
    card = extract(proto_path)
    if not slug:
        # 未指定 slug：从原型角色名生成（去路径分隔符，中文保留）
        name = (card.get("name") or "").strip()
        slug = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", name).strip("-").lower()
        slug = slug or "character"
    out_dir = CHAR_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) card.json（运行时角色卡，程序唯一读取的角色卡文件）
    write_json(out_dir / "card.json", card)

    # 2) prototype.json（原型卡副本，只读溯源）
    shutil.copyfile(proto_path, out_dir / "prototype.json")

    # 3) sprite.png（立绘副本；原型缺失则保留现有）
    if PROTO_SPRITE.exists():
        shutil.copyfile(PROTO_SPRITE, out_dir / "sprite.png")

    # 4) profile.ini（程序集成元数据：首次生成默认，已存在保留精调）
    profile_path = out_dir / "profile.ini"
    if not profile_path.exists():
        display_name = card.get("name") or slug
        identity = card.get("name") or display_name
        profile_path.write_text(
            DEFAULT_PROFILE.format(slug=slug, display_name=display_name,
                                   identity=identity),
            encoding="utf-8")
        profile_note = "新建默认 profile.ini"
    else:
        profile_note = "保留已有 profile.ini（精调内容不动）"

    # 5) identity.json（【你的身份与世界观】契约，见 character/SCHEMA.md）
    #    外部内容适配：不存在则从原型生成模板；已存在则保留人工精调，
    #    仅校验角色名与原型一致（不一致 = 原型可能已换，警告不覆盖）。
    identity_path = out_dir / "identity.json"
    if identity_path.exists():
        try:
            old_id = json.loads(identity_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            old_id = {}
        tpl = build_identity_template(card)
        old_name = (old_id.get("identity") or {}).get("name", "")
        new_name = (tpl.get("identity") or {}).get("name", "")
        # 容忍包含关系（原型名常带英文原名括号，如「某个角色 (Role Name)」）
        if old_name and new_name and old_name != new_name \
                and old_name not in new_name and new_name not in old_name:
            print(f"[freeze] 警告: identity.json 角色名「{old_name}」与原型「{new_name}」"
                  "不一致（未覆盖，请人工确认）")
        identity_note = "保留已有 identity.json（人工精调数据优先）"
    else:
        write_json(identity_path, build_identity_template(card))
        identity_note = "新建 identity.json 模板（外部内容已适配为标准契约，建议人工精化）"

    n = len(card.get("description", ""))
    entries = len((card.get("character_book") or {}).get("entries", []))
    print(f"[freeze] 已固化角色: {slug} → {out_dir.relative_to(ROOT)}/")
    print(f"[freeze] card.json | description {n} 字符 | 世界书 {entries} 条")
    print(f"[freeze] prototype.json / sprite.png 已同步 | {profile_note} | {identity_note}")
    print(f"[freeze] 来源原型: {proto_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
