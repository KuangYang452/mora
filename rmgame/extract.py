# -*- coding: utf-8 -*-
"""文本提取 —— rmgame/extract

职责（设计文档 §4.2）：
- 解析新世代游戏 data/ 下的 Map*.json（明文 JSON）
- 对话提取器（默认）：事件命令流中 code 101/401/405/102/402/403/404 →
  按事件聚合为对话条目（含选项与分支文本）
- 全量提取器（--all）：额外提取 Items/Skills/Actors/System 的描述文本
- 输出：raw/<slug>/maps/Map001.json + meta.json（+ database.json）

命令码（MV/MZ 事件命令）：
- 101 显示文本（faceName 参数）| 401 文本行 | 405 文本换行（续行）
- 102 显示选项 | 402 选项分支 | 403 分支 else | 404 分支结束
"""

import json
import re
import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

from .discovery import RAW_DIR, GameInfo

# 文本命令码（对话状态机关注）
_CMD_SHOW_TEXT = 101      # 显示文本（头部）
_CMD_TEXT = 401           # 文本行
_CMD_TEXT_CONT = 405      # 文本换行（续行）
_CMD_CHOICES = 102        # 显示选项
_CMD_BRANCH = 402         # 选项分支开始
_CMD_BRANCH_ELSE = 403    # 分支 else
_CMD_BRANCH_END = 404     # 分支结束


@dataclass
class ExtractionResult:
    """一次提取的内存结果；write_raw 负责落盘。"""
    game: GameInfo
    maps: list = field(default_factory=list)   # [{map_id, map_name, file, entries}]
    common_events: list = field(default_factory=list)  # 公共事件对话条目
    troops: list = field(default_factory=list)  # 战斗事件对话条目（Troops.json）
    database: list = field(default_factory=list)  # 全量提取的文本条目
    all_text: bool = False

    @property
    def map_count(self) -> int:
        return len(self.maps)

    @property
    def entry_count(self) -> int:
        return (sum(len(m["entries"]) for m in self.maps)
                + len(self.common_events) + len(self.troops))


# ---------------------------------------------------------------------------
# 单地图对话提取
# ---------------------------------------------------------------------------

def _map_id_from_name(stem: str) -> int:
    m = re.search(r"(\d+)", stem)
    return int(m.group(1)) if m else 0


def _new_entry(map_stem: str, ev_id: int, ev_name: str, cmd_idx: int,
               page_idx: int = 0) -> dict:
    return {
        "id": f"{map_stem}.{ev_id}.{cmd_idx}",
        "event_id": ev_id,
        "page": page_idx,      # 事件页面索引（同一事件多页 = 多个独立对话阶段）
        "event_name": ev_name,
        "type": "dialogue",
        "speaker": ev_name or None,
        "face": None,
        "text": "",
        "choices": [],
        "branch_texts": [],
        "raw": [],
    }


def _flush(entries: list, cur: dict) -> None:
    """收尾当前对话段：无有效文本则丢弃。"""
    if cur is None:
        return
    if cur["text"] or cur["branch_texts"]:
        cur["text"] = cur["text"].rstrip("\n")
        entries.append(cur)


def _walk_commands(cmd_list: list, entries: list,
                   map_stem: str, ev_id: int, ev_name: str,
                   page_idx: int = 0) -> None:
    """事件命令流 → 对话条目（状态机）。"""
    cur = None
    branch = None
    for idx, cmd in enumerate(cmd_list):
        code = cmd.get("code")
        params = cmd.get("parameters") or []
        if code == _CMD_SHOW_TEXT:
            _flush(entries, cur)
            cur = _new_entry(map_stem, ev_id, ev_name, idx, page_idx)
            face = params[0] if params and isinstance(params[0], str) and params[0] else None
            cur["face"] = face
            cur["raw"].append(code)
            branch = None
        elif code in (_CMD_TEXT, _CMD_TEXT_CONT):
            text = params[0] if params else ""
            if cur is None:      # 无 101 直接出现的文本：兜底开段
                cur = _new_entry(map_stem, ev_id, ev_name, idx, page_idx)
            if branch:
                cur["branch_texts"].append({"branch": branch, "text": text})
            else:
                # 多行文本以换行分隔（游戏内分行显示），拼接时行间补 \n
                cur["text"] = (cur["text"] + "\n" + text) if cur["text"] else text
            cur["raw"].append(code)
        elif code == _CMD_CHOICES:
            if cur is None:
                cur = _new_entry(map_stem, ev_id, ev_name, idx, page_idx)
            cur["choices"] = params[0] if params and isinstance(params[0], list) else []
            cur["raw"].append(code)
        elif code == _CMD_BRANCH:
            if cur is None:
                cur = _new_entry(map_stem, ev_id, ev_name, idx, page_idx)
            branch = params[1] if len(params) > 1 and params[1] else "分支"
            cur["raw"].append(code)
        elif code == _CMD_BRANCH_ELSE:
            if cur is not None:
                branch = "否则"
                cur["raw"].append(code)
        elif code == _CMD_BRANCH_END:
            branch = None
            if cur is not None:
                cur["raw"].append(code)
        else:
            _flush(entries, cur)
            cur = None
            branch = None
    _flush(entries, cur)


def _extract_map(map_path: Path) -> dict or None:
    """单个地图数据文件（Map001.json 等）→ {map_id, map_name, file, entries}。

    顶层不是 dict（如 MapInfos.json 被误匹配）时返回 None，由调用方跳过。
    """
    data = json.loads(map_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        return None
    stem = map_path.stem
    map_name = data.get("displayName") or stem
    entries = []
    for ev in data.get("events") or []:
        if not isinstance(ev, dict):
            continue  # MV 事件槽位可能为 null（空事件）
        ev_id = int(ev.get("id", 0))
        ev_name = str(ev.get("name", "") or "")
        for pidx, page in enumerate(ev.get("pages") or []):
            cmd_list = page.get("list") or []
            _walk_commands(cmd_list, entries, stem, ev_id, ev_name, page_idx=pidx)
    return {
        "map_id": _map_id_from_name(stem),
        "map_name": map_name,
        "file": f"{stem}.json",
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# 公共事件提取（CommonEvents.json）：开场/通用剧情文本，同属对话范畴
# ---------------------------------------------------------------------------

def _extract_common_events(data_dir: Path) -> list:
    """CommonEvents.json → 对话条目（id 形如 CommonEvents.218.5）。

    公共事件与地图事件同为命令流（code 101/401/405/102 等），复用
    _walk_commands；条目 id 以 CommonEvents 为文件名段，供 raw:// 引用
    解析推导出 CommonEvents.json。
    """
    f = data_dir / "CommonEvents.json"
    if not f.exists():
        return []
    try:
        events = json.loads(f.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return []
    entries = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        ev_id = int(ev.get("id", 0))
        cmd_list = ev.get("list") or []
        _walk_commands(cmd_list, entries, "CommonEvents", ev_id, "")
    return entries


# ---------------------------------------------------------------------------
# 战斗事件提取（Troops.json）：战斗中的对话/台词
# ---------------------------------------------------------------------------

def _extract_troops(data_dir: Path) -> list:
    """Troops.json → 对话条目（id 形如 Troops.5.3）。

    战斗事件与地图/公共事件同为命令流（code 101/401/405 等），复用
    _walk_commands；条目 id 以 Troops 为文件名段，供 raw:// 引用解析
    推导出 Troops.json。战斗事件名称（如「Boss战」）作为 speaker 线索。
    """
    f = data_dir / "Troops.json"
    if not f.exists():
        return []
    try:
        troops = json.loads(f.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return []
    entries = []
    for t in troops or []:
        if not isinstance(t, dict):
            continue
        troop_id = int(t.get("id", 0))
        name = str(t.get("name", "") or "")
        for pidx, page in enumerate(t.get("pages") or []):
            cmd_list = page.get("list") or []
            _walk_commands(cmd_list, entries, "Troops", troop_id, name, page_idx=pidx)
    return entries


# ---------------------------------------------------------------------------
# 全量提取（--all）：数据库类文本
# ---------------------------------------------------------------------------

def _extract_database(data_dir: Path) -> list:
    """Items/Skills/Actors/System 的 name/description 等 → 文本条目。"""
    out = []
    specs = [
        ("Items.json", "item", ("name", "description")),
        ("Weapons.json", "weapon", ("name", "description")),
        ("Armors.json", "armor", ("name", "description")),
        ("Skills.json", "skill", ("name", "description")),
        ("Enemies.json", "enemy", ("name", "description")),
        ("Classes.json", "class", ("name", "description")),
        ("States.json", "state", ("name", "description")),
        ("Actors.json", "actor", ("name", "nickname", "profile")),
    ]
    for fname, kind, fields in specs:
        f = data_dir / fname
        if not f.exists():
            continue
        try:
            rows = json.loads(f.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue
        for i, row in enumerate(rows or []):
            if not isinstance(row, dict):
                continue
            texts = [str(row.get(k, "")).strip() for k in fields]
            texts = [t for t in texts if t]
            if not texts:
                continue
            out.append({
                "id": f"{kind}.{i}",
                "type": kind,
                "name": row.get("name", ""),
                "text": "\n".join(texts),
            })
    sys_f = data_dir / "System.json"
    if sys_f.exists():
        try:
            sysd = json.loads(sys_f.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            sysd = {}
        title = sysd.get("gameTitle", "") if isinstance(sysd, dict) else ""
        if title:
            out.append({"id": "system.0", "type": "system",
                        "name": "游戏标题", "text": str(title)})
    return out


# ---------------------------------------------------------------------------
# 提取入口与落盘
# ---------------------------------------------------------------------------

def extract_game(game: GameInfo, all_text: bool = False) -> ExtractionResult:
    """解析游戏 data 目录 → 内存结果（不写盘）。"""
    data_dir = Path(game.data_dir)
    if not data_dir.is_dir():
        raise FileNotFoundError(f"游戏数据目录不存在: {data_dir}")
    maps = []
    # 只匹配地图数据文件（Map 后跟数字）；排除 MapInfos.json 等信息文件
    for f in sorted(data_dir.glob("Map[0-9]*.json"),
                    key=lambda p: _map_id_from_name(p.stem)):
        m = _extract_map(f)
        if m is not None:
            maps.append(m)
    result = ExtractionResult(game=game, maps=maps, all_text=all_text)
    result.common_events = _extract_common_events(data_dir)
    result.troops = _extract_troops(data_dir)   # 战斗对话（默认提取）
    if all_text:
        result.database = _extract_database(data_dir)
    return result


def write_raw(result: ExtractionResult) -> Path:
    """把提取结果写入 raw/<slug>/，返回 raw 游戏目录。"""
    slug = result.game.slug
    game_raw = RAW_DIR / slug
    maps_dir = game_raw / "maps"
    maps_dir.mkdir(parents=True, exist_ok=True)
    for m in result.maps:
        (maps_dir / m["file"]).write_text(
            json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.common_events:
        (game_raw / "CommonEvents.json").write_text(
            json.dumps({"common_events": result.common_events,
                        "count": len(result.common_events)},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
    if result.troops:
        (game_raw / "Troops.json").write_text(
            json.dumps({"troops": result.troops,
                        "count": len(result.troops)},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
    db_file = game_raw / "database.json"
    if result.database:
        db_file.write_text(
            json.dumps(result.database, ensure_ascii=False, indent=2),
            encoding="utf-8")
    elif db_file.exists():
        # 本次未提取全量文本：清理上次残留（避免数据不一致）
        db_file.unlink()
    meta = {
        "slug": slug,
        "name": result.game.name,
        "engine": result.game.engine,
        "game_dir": result.game.dir,
        "all_text": result.all_text,
        "map_count": len(result.maps),
        "common_event_count": len(result.common_events),
        "troop_count": len(result.troops),
        "entry_count": result.entry_count,
        "extracted_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    (game_raw / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return game_raw
