# -*- coding: utf-8 -*-
"""游戏发现与注册 —— rmgame/discovery

职责：
- 扫描指定目录，识别 RPG Maker 新世代（MV/MZ）与老世代（XP/VX/Ace）游戏
- 游戏库注册表：runtime/games.json（唯一来源）
- 入库分两级信任：user（人工确认，可启动）/ auto（自动发现运行中游戏，仅只读）
  自动发现见 monitor.enumerate_running；确认升级见 approve（CLI scan --approve）

判定特征（见设计文档 §4.1）：
| 世代 | 特征 |
|---|---|
| MV  | Game.exe + www/data/ 或 data/ + package.json（main 指向 index.html） |
| MZ  | Game.exe + data/ + package.json + js/rmmz_*.js |
| 老世代 | Game.exe + Data/*.rvdata2 / .rvdata / .rxdata（仅登记不解析） |
"""

import json
import os
import re
import hashlib
import datetime as _dt
from dataclasses import dataclass, asdict, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径常量（raw/wiki/runtime 均在角色目录 = 包上级）
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = BASE_DIR / "raw"
WIKI_DIR = BASE_DIR / "wiki"
RUNTIME_DIR = BASE_DIR / "runtime"
GAMES_FILE = RUNTIME_DIR / "games.json"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class GameInfo:
    slug: str          # 唯一标识：名称小写 + 连字符（稳定，迁移数据目录时不变）
    name: str          # 显示名（引擎标题 System.json gameTitle / Game.ini Title 择优）
    exe_path: str      # Game.exe 绝对路径
    dir: str           # 游戏根目录
    engine: str        # "mv" | "mz" | "legacy"
    data_dir: str      # 文本数据目录（MV: www/data 或 data；MZ: data）
    version: str = ""  # 可空
    added_at: str = ""  # 入库时间
    launch_mode: str = "auto"  # 启动方式：auto（探测）| normal | bypass（nwjs 旁路）
    trust: str = "user"   # 信任级别：user（人工确认，可启动）| auto（自动发现，禁启动）
    last_seen: str = ""   # 运行中发现时间（自动入库）；人工入库可空
    aliases: list = field(default_factory=list)  # 可匹配名称（自动采集：引擎标题/目录名/父目录名）
    name_manual: bool = False  # 显示名是否人工命名（True 时重扫/自动发现不覆盖，新检测名并入别名）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GameInfo":
        kwargs = {k: d.get(k) for k in
                  ("slug", "name", "exe_path", "dir", "engine",
                   "data_dir", "version", "added_at", "launch_mode",
                   "trust", "last_seen", "aliases", "name_manual")}
        kwargs["launch_mode"] = kwargs.get("launch_mode") or "auto"  # 旧库缺字段 → auto
        kwargs["trust"] = kwargs.get("trust") or "user"      # 旧库缺字段 → user（历史均为人工入库）
        kwargs["last_seen"] = kwargs.get("last_seen") or ""
        kwargs["aliases"] = list(kwargs.get("aliases") or [])
        kwargs["name_manual"] = bool(kwargs.get("name_manual", False))  # 旧库缺字段 → False
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# slug 生成与游戏库读写
# ---------------------------------------------------------------------------

def make_slug(name: str) -> str:
    """游戏名 → 小写连字符标识；保留中日韩字符。"""
    s = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", name.strip()).strip("-")
    s = s.lower()
    return s or "game"


# 自动命名哈希占位（如 game-a1b2c3d4e5）：发现结果一律以此命名，正式名称
# 由角色/用户经 rename_game 赋予（见 _build_info 说明）
_AUTO_NAME_RE = re.compile(r"^game-[0-9a-f]{10}$")


def _auto_hash_name(game_dir: Path) -> str:
    """自动命名占位名：基于游戏目录绝对路径的短哈希（稳定、唯一）。

    同目录永远同哈希（幂等）；不同目录几乎不可能碰撞 —— 彻底避免引擎
    默认包名（如 MZ 的 rmmz-game）造成的命名重复与先入为主。
    """
    digest = hashlib.sha256(
        str(game_dir.resolve()).encode("utf-8")).hexdigest()
    return f"game-{digest[:10]}"


def is_auto_hash(name: str) -> bool:
    """是否自动命名哈希占位（发现结果，尚未经 rename_game 正式命名）。"""
    return bool(_AUTO_NAME_RE.match(name or ""))


# 引擎默认包名等通用名：不收入别名（避免多游戏共享同一通用名导致
# 按别名解析歧义 —— 正是两个 rmmz-game 撞名的根源之一）
_GENERIC_ALIAS_RE = re.compile(
    r"^(?:rmmv-game|rmmz-game|game|mz|mv|project\d*)$", re.I)


def load_games() -> list:
    """读取游戏库；文件缺失或损坏返回空列表。"""
    if not GAMES_FILE.exists():
        return []
    try:
        data = json.loads(GAMES_FILE.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return []
    return [GameInfo.from_dict(g) for g in data.get("games", [])]


def save_games(games: list) -> None:
    """写入游戏库（原子写：先写临时文件再替换）。"""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"games": [g.to_dict() for g in games]}
    tmp = GAMES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(GAMES_FILE)


# ---------------------------------------------------------------------------
# 引擎判定
# ---------------------------------------------------------------------------

# 目录名中的"非游戏名"片段：方括号标记（[JP]/[ver1.11]）与版本号（v1.0 / ver1.11 / 1.11）
_STRIP_NAME_NOISE = re.compile(
    r"\[[^\]]*\]|\b(?:ver|v|version)\s*\d+(?:\.\d+)*\b|\d+(?:\.\d+)+", re.I)


def _meaningful_dir_name(dir_name: str) -> bool:
    """目录名剥掉版本/语言标记后是否仍有实质内容。

    版本子目录（如 [JP][ver1.11]）或压缩包名常不含游戏名，
    此时应退回引擎标题（System.json gameTitle）；正常游戏目录
    （如「猎妻迷宫」「MZDemo」）保留目录名即可。
    """
    s = _STRIP_NAME_NOISE.sub("", dir_name or "").strip(" -_[]()")
    return bool(s)


def _read_system_title(data_dir: Path) -> str:
    """从 System.json 读引擎游戏标题（MV/MZ 权威名称）；失败返回空串。"""
    sys_f = data_dir / "System.json"
    if not sys_f.is_file():
        return ""
    try:
        sysd = json.loads(sys_f.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return ""
    if not isinstance(sysd, dict):
        return ""
    return str(sysd.get("gameTitle", "") or "").strip()


def _read_game_ini_title(game_dir: Path) -> str:
    """从 Game.ini 读引擎游戏标题（XP/VX/Ace 权威名称）；失败返回空串。"""
    ini_f = game_dir / "Game.ini"
    if not ini_f.is_file():
        return ""
    try:
        text = ini_f.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    m = re.search(r"^\s*Title\s*=\s*(.+?)\s*$", text, re.M | re.I)
    return m.group(1).strip() if m else ""


def _collect_aliases(game_dir: Path, engine_title: str, pkg_name: str,
                     dir_meaningful: bool) -> list:
    """自动采集可匹配名称（零手工）：引擎标题 / package.json name / 目录名。

    目录名不含游戏名（版本子目录）时，父目录常为打包目录
    （如 [淫乱轮轴][輪淫のスピンドル][Spindle]），拆方括号并入。
    返回去重列表（不含空串）；name 本身由调用方再排除。
    """
    names = []
    for s in (engine_title, pkg_name, game_dir.name):
        if s and s not in names and not _GENERIC_ALIAS_RE.match(s):
            names.append(s)
    if not dir_meaningful:
        parent = game_dir.parent.name
        if parent:
            for p in re.split(r"[\[\]]+", parent):
                p = p.strip()
                if p and p not in names and not _GENERIC_ALIAS_RE.match(p):
                    names.append(p)
    return names


def _is_engine(game_dir: Path, exe: Path) -> GameInfo or None:
    """对含 Game.exe 的目录做引擎判定；无法识别返回 None。"""
    pkg = game_dir / "package.json"
    data_candidates = [
        game_dir / "www" / "data",
        game_dir / "data",
    ]
    pkg_main = ""
    pkg_name = ""
    if pkg.exists():
        try:
            pj = json.loads(pkg.read_text(encoding="utf-8-sig"))
            pkg_main = str(pj.get("main", "") or "")
            pkg_name = str(pj.get("name", "") or "").strip()
        except (json.JSONDecodeError, OSError):
            pass

    for data_dir in data_candidates:
        if not data_dir.is_dir():
            continue
        # MZ：js/rmmz_*.js 存在
        js_dir = game_dir / "js"
        mz_marker = any(p.name.startswith("rmmz_") for p in js_dir.glob("rmmz_*.js")) \
            if js_dir.is_dir() else False
        if mz_marker:
            title = _read_system_title(data_dir)
            return _build_info(game_dir, exe, "mz", data_dir, pkg_name,
                               pkg_main, title)
        # MV：js/rpg_core.js 或 package.json main 指向 index.html
        mv_marker = (js_dir / "rpg_core.js").exists() or pkg_main.endswith(".html")
        if mv_marker:
            title = _read_system_title(data_dir)
            return _build_info(game_dir, exe, "mv", data_dir, pkg_name,
                               pkg_main, title)

    # 老世代：Data/*.rvdata2 / .rvdata / .rxdata（仅登记）
    legacy_dir = game_dir / "Data"
    if legacy_dir.is_dir():
        for suffix in (".rvdata2", ".rvdata", ".rxdata"):
            if any(p.suffix == suffix for p in legacy_dir.iterdir()):
                title = _read_game_ini_title(game_dir)
                return _build_info(game_dir, exe, "legacy", legacy_dir, "",
                                   "", title)
    return None


def _build_info(game_dir: Path, exe: Path, engine: str, data_dir: Path,
                pkg_name: str, pkg_main: str, engine_title: str = "") -> GameInfo:
    """构建 GameInfo：自动命名一律哈希占位，真实名称全部收进 aliases。

    自动命名使用基于游戏目录的哈希（稳定唯一、无先入为主、无重名——
    引擎默认包名如 rmmz-game 会被多个游戏共享而撞名，见 _auto_hash_name）；
    正式名称由角色/用户经 rename_game 赋予（迫使莫拉起名）。
    可匹配名称（引擎标题 System.json gameTitle / Game.ini Title、
    package.json name、目录名；目录名无意义时父目录拆段）全部收进
    aliases 供匹配（_resolve_game 命中任一即可）。
    """
    dir_name = game_dir.name
    dir_meaningful = _meaningful_dir_name(dir_name)
    name = _auto_hash_name(game_dir)
    slug = make_slug(name)
    aliases = [a for a in _collect_aliases(game_dir, engine_title, pkg_name,
                                           dir_meaningful)
               if a != name and make_slug(a) != slug]
    return GameInfo(
        slug=slug,
        name=name,
        exe_path=str(exe),
        dir=str(game_dir),
        engine=engine,
        data_dir=str(data_dir),
        version=pkg_main if engine in ("mv", "mz") else "",
        aliases=aliases,
    )


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------

def discover_dir(exe_path) -> "GameInfo" or None:
    """对单个 Game.exe 所在目录做引擎判定（复用 _is_engine）。

    供进程枚举侧（monitor.enumerate_running）识别运行中的游戏；
    不扫描、不写库。返回 GameInfo（trust 默认 user，由调用方决定入库方式）。
    """
    exe = Path(exe_path)
    if not exe.is_file():
        return None
    return _is_engine(exe.parent, exe)


def discover(root_dir, recursive: bool = True) -> list:
    """扫描根目录，返回识别出的游戏列表（不修改游戏库）。"""
    root = Path(root_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在: {root}")
    found = []
    seen = set()
    for game_dir in _walk(root, recursive):
        exe = game_dir / "Game.exe"
        if not exe.exists():
            continue
        info = _is_engine(game_dir, exe)
        if info is None:
            continue
        key = (info.slug, info.dir)
        if key in seen:
            continue
        seen.add(key)
        found.append(info)
    return found


def _walk(root: Path, recursive: bool):
    """目录遍历：recursive=False 时只查根目录一层。"""
    if recursive:
        for dirpath, _, _ in os.walk(root):
            yield Path(dirpath)
    else:
        yield root


def register(games: list, replace: bool = False) -> list:
    """把发现结果并入游戏库：按 slug 合并，不重复入库（人工路径，trust=user）。

    已存在条目（按 dir 命中）自动刷新更权威的 name / aliases
    （引擎标题 System.json gameTitle 等）—— slug 保持不变，避免迁移
    raw/wiki/runtime 数据目录；一次扫描即自愈，无需手工维护。
    """
    existing = [] if replace else load_games()
    by_slug = {g.slug: g for g in existing}
    by_dir = {g.dir: g for g in existing}
    now = _dt.datetime.now().isoformat(timespec="seconds")
    for g in games:
        old = by_dir.get(g.dir)
        if old is not None:
            # 已入库：仅刷新显示名与别名（slug 稳定）
            # 手动命名条目（name_manual）不被自动检测名覆盖；新检测名并入别名。
            # 自动检测名现在是哈希占位（_build_info），一律不覆盖已有实质名
            # （否则 猎妻迷宫 等已有名称会在下次发现时被改成 game-xxxx）。
            if (not old.name_manual and old.name != g.name
                    and not is_auto_hash(g.name)):
                old.name = g.name
            if not old.name_manual:
                if old.aliases != g.aliases:
                    old.aliases = g.aliases
            else:
                for a in [g.name] + list(g.aliases or []):
                    if a and a != old.name and a not in old.aliases:
                        old.aliases.append(a)
            continue
        if g.slug in by_slug:
            continue  # 同 slug 其他目录已入库（防重）
        g.added_at = now
        by_slug[g.slug] = g
        by_dir[g.dir] = g
    merged = list(by_slug.values())
    save_games(merged)
    return merged


def auto_register(info: GameInfo) -> bool:
    """自动发现入库（trust=auto，幂等）：slug 未入库才写入。

    自动入库只解锁只读/写角色目录的能力（read_current_text / scan_game /
    query_wiki / read_raw_text 等）；start_game（执行外部 exe）仍需人工
    确认升级 trust=user（见 approve）。返回是否发生了写入。
    """
    games = load_games()
    for g in games:
        if g.slug == info.slug:
            return False  # 已入库（不论 trust），不覆盖不降级
        if g.dir == info.dir:
            # 同目录已入库：仅刷新更权威的 name / aliases（slug 稳定）；
            # 手动命名条目（name_manual）不被自动名覆盖，新检测名并入别名
            changed = False
            if (not getattr(g, "name_manual", False) and g.name != info.name
                    and not is_auto_hash(info.name)):
                g.name = info.name
                changed = True
            if not getattr(g, "name_manual", False):
                if g.aliases != info.aliases:
                    g.aliases = info.aliases
                    changed = True
            else:
                for a in [info.name] + list(info.aliases or []):
                    if a and a != g.name and a not in g.aliases:
                        g.aliases.append(a)
                        changed = True
            if changed:
                save_games(games)
            return False
    now = _dt.datetime.now().isoformat(timespec="seconds")
    info.trust = "auto"
    info.added_at = now
    info.last_seen = now
    games.append(info)
    save_games(games)
    return True


def approve(slug: str):
    """升级游戏信任 auto → user（人工确认，解锁 start_game）。

    只认库中已有记录（slug 或名称），不接受任意路径/任意名字，防注入式确认。
    返回 (GameInfo | None, 消息文本)；库中无记录时 GameInfo 为 None。
    """
    games = load_games()
    g = next((x for x in games if x.slug == slug or x.name == slug), None)
    if g is None:
        g = next((x for x in games if slug in getattr(x, "aliases", [])), None)
    if g is None:
        return None, f"游戏库中无「{slug}」（先 scan 或自动发现入库）。"
    if (getattr(g, "trust", "user") or "user") == "user":
        return g, f"《{g.name}》已是人工确认状态（trust=user），无需升级。"
    g.trust = "user"
    save_games(games)
    return g, f"《{g.name}》已确认（trust=user），角色现在可以启动它了。"


# ---------------------------------------------------------------------------
# 改名（角色命名 / 用户改名统一入口）
# ---------------------------------------------------------------------------

def _sync_dir_meta(meta_path: Path, new_slug: str, new_name: str) -> None:
    """同步 raw/meta.json 与 wiki/index.json 内嵌的 slug/name（迁移后自洽）。"""
    if not meta_path.is_file():
        return
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return
    if isinstance(data, dict) and ("slug" in data or "name" in data):
        data["slug"] = new_slug
        data["name"] = new_name
        meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def _migrate_slug_dirs(old_slug: str, new_slug: str, new_name: str) -> None:
    """迁移 slug 数据目录：raw/<old> / wiki/<old> / event_summary/<old> → 新 slug。

    先校验全部迁移目标空闲（避免部分迁移），再执行目录重命名，最后同步
    meta.json / index.json 内嵌字段。无数据目录时零操作。
    """
    pairs = []
    for base in (RAW_DIR, WIKI_DIR):
        src = base / old_slug
        if src.is_dir():
            dst = base / new_slug
            if dst.exists():
                raise RuntimeError(f"迁移目标已存在: {dst}")
            pairs.append((src, dst))
    es = RUNTIME_DIR / "event_summary"
    src = es / old_slug
    if src.is_dir():
        dst = es / new_slug
        if dst.exists():
            raise RuntimeError(f"迁移目标已存在: {dst}")
        pairs.append((src, dst))
    for src, dst in pairs:
        src.rename(dst)
    _sync_dir_meta(RAW_DIR / new_slug / "meta.json", new_slug, new_name)
    _sync_dir_meta(WIKI_DIR / new_slug / "index.json", new_slug, new_name)


def rename_game(key: str, new_name: str) -> tuple:
    """改名（角色命名 / 用户改名统一入口）：更新 games.json + 按需迁移数据目录。

    - key 按 slug / name / aliases 匹配库中条目；新名非空；
    - 新 slug = make_slug(new_name)；与库中其他游戏（不同目录）slug 冲突 → 拒绝；
    - 旧名并入 aliases（保匹配）；name_manual=True（重扫/自动发现不覆盖）；
    - raw/ wiki/ runtime/event_summary/ 下旧 slug 目录存在则整体迁移到新 slug，
      并同步 meta.json / index.json 内嵌的 slug / name；无数据目录时零成本。
    返回 (GameInfo | None, 消息文本)。
    """
    new_name = (new_name or "").strip()
    if not new_name:
        return None, "新名称不能为空。"
    games = load_games()
    g = next((x for x in games if x.slug == key or x.name == key), None)
    if g is None:
        g = next((x for x in games if key in getattr(x, "aliases", [])), None)
    if g is None:
        return None, f"游戏库中无「{key}」。"
    if new_name == g.name:
        return g, f"《{g.name}》名称未变化，无需改名。"
    new_slug = make_slug(new_name)
    if new_slug != g.slug:
        for other in games:
            if other is not g and other.slug == new_slug:
                return None, (f"改名冲突：库中已有《{other.name}》使用标识"
                              f"「{new_slug}」，请换一个名称。")
        try:
            _migrate_slug_dirs(g.slug, new_slug, new_name)
        except RuntimeError as exc:
            return None, f"改名中止（数据目录迁移失败）：{exc}"
    if g.name not in g.aliases:
        g.aliases.append(g.name)
    g.name = new_name
    g.slug = new_slug
    g.name_manual = True
    save_games(games)
    return g, f"《{new_name}》已改名（标识 {new_slug}）"
