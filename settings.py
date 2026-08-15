# -*- coding: utf-8 -*-
"""配置加载器 —— setting/*.ini 与路径中心

唯一配置入口（程序主体通用，不含角色数据）：
- setting/app.ini   应用运行配置（外观/行为/rmgame 开关/目录路径）
- setting/llm.ini   LLM 连接配置（原 llm_config.json）
- setting/user.ini  用户配置（用户称呼，{{user}} 占位符实例化值）

角色专属配置在 character/<slug>/（card.json + profile.ini），由
character 包加载，不经过本模块。

约定：
- 所有目录路径一律从 paths() 读取，禁止在模块里散落写死
  Path(__file__).resolve().parent / "xxx"；
- ini 解析使用 RawConfigParser（不做 % 插值，避免台词中的 % 被吞）；
- LLM 配置缺失必填字段（base_url/api_key/model）时抛 ConfigError，
  不静默兜底（README「架构」约定 2）；
- 运行期热读（M3 配置分层，见 docs/REFACTOR_DESIGN.md §6）：功能开关类
  键（content_mode / tool_choice / log_enabled / rmgame_* / retry_* 等）
  经 app_get 读取（白名单 + mtime 缓存），修改 app.ini 后无需重启；
  启动期冻结键（外观/行为/上下文窗口）由调用方启动时经 app_config 读取一次。
"""

import configparser
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SETTING_DIR = ROOT / "setting"

# 版本号唯一事实来源（语义化版本 MAJOR.MINOR.PATCH，规范见 README
# 「版本与发布」附录）：发布流程 = 全量自测 → CHANGELOG.md 归档 → 更新本常量
# → 提交 → git tag v<版本>。
VERSION = "1.1.0"


class ConfigError(Exception):
    pass


def _read_ini(name: str, inline: bool = False) -> configparser.RawConfigParser:
    """读 ini；inline=True 时启用行内注释（# / ; 截断，用于程序配置，
    值不含这些符号的文件如 profile.ini 不要启用）。"""
    if inline:
        cp = configparser.RawConfigParser(inline_comment_prefixes=("#", ";"))
    else:
        cp = configparser.RawConfigParser()
    cp.read(SETTING_DIR / name, encoding="utf-8")
    return cp


def _get(cp, section: str, key: str, default=None):
    try:
        return cp.get(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return default


# ---------------------------------------------------------------------------
# 应用配置（app.ini → dict，键名与旧 data.CONFIG 完全兼容）
# ---------------------------------------------------------------------------

# 类型转换表：app.ini 全部读为字符串，按此表还原类型；未列出的键按 str。
# 时间/时长类键统一浮点（允许小数）：bubble 秒与每字符毫秒、闲聊间隔秒、
# 环境快照新鲜度秒 —— 曾误入 _INT_KEYS 导致 "3.0"/"10.0" 解析失败回退 0
# （气泡停留 0ms 瞬间消失的根因，见 docs/GAME_ONBOARDING_DESIGN.md 之外的问题记录）。
_INT_KEYS = {
    "bubble_fade_ms", "bubble_type_ms", "bubble_pause_ms", "bubble_corner",
    "bubble_shadow", "auto_chat_minutes", "auto_chat_max_minutes",
    "history_rounds", "context_keep_recent",
    "context_keep_mid", "agent_max_turns",
}
_FLOAT_KEYS = {
    "scale", "auto_chat_growth", "rmgame_monitor_interval",
    "bubble_min_seconds", "bubble_max_seconds", "bubble_read_ms_per_char",
    "auto_chat_min_gap_seconds", "rmgame_env_fresh_seconds",
}
_BOOL_KEYS = {
    "idle_bob", "greet_on_start", "speech_as_tool",
    "retry_on_vague_query", "force_say_to_finish", "mesugaki_style_block",
    "retry_on_repeated_query", "retry_on_multi_query",
    "rmgame_enabled", "rmgame_cdp_enabled", "monitor_auto_start",
    "rmgame_auto_discover", "rmgame_confirm_bubble",
    "log_enabled",
}


def _conv(key: str, raw: str):
    if key in _INT_KEYS:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    if key in _FLOAT_KEYS:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    if key in _BOOL_KEYS:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return raw


def app_config() -> dict:
    """应用运行配置 dict（原 data.CONFIG 迁移）。"""
    cp = _read_ini("app.ini", inline=True)
    out = {}
    if cp.has_section("app"):
        for key, val in cp.items("app"):
            out[key] = _conv(key, val)
    return out


# ---------------------------------------------------------------------------
# 路径中心
# ---------------------------------------------------------------------------

def paths() -> dict:
    """工程目录路径（全部相对工程根，可从 app.ini [paths] 覆盖）。

    save_dir 缺省 runtime（会话存档 runtime/pet_session.json 与
    rmgame 运行时数据 current.json/games.json/notes/ 同目录，保持一致）；
    runtime 目录固定为工程根 runtime/，不配置。
    """
    cp = _read_ini("app.ini", inline=True)
    save_rel = _get(cp, "paths", "save_dir", "runtime") or "runtime"
    log_rel = _get(cp, "paths", "log_dir", "log") or "log"
    char_rel = _get(cp, "paths", "character_dir", "character") or "character"
    return {
        "root": ROOT,
        "save": ROOT / save_rel,
        "log": ROOT / log_rel,
        "character": ROOT / char_rel,
        "runtime": ROOT / "runtime",
    }


def save_dir() -> Path:
    return paths()["save"]


def runtime_dir() -> Path:
    """运行时数据目录（current.json / games.json / notes/ / event_summary/）。"""
    return paths()["runtime"]


def log_dir() -> Path:
    return paths()["log"]


def character_dir() -> Path:
    return paths()["character"]


# ---------------------------------------------------------------------------
# LLM 配置（llm.ini，原 llm_config.json）
# ---------------------------------------------------------------------------

def llm_config(path: Path = None) -> dict:
    """读取 LLM 配置；缺失/损坏/必填字段缺失抛 ConfigError。

    path 可覆盖（selftest 用）；默认为 setting/llm.ini。
    """
    p = path or (SETTING_DIR / "llm.ini")
    if not p.exists():
        raise ConfigError(
            f"缺少 LLM 配置文件: {p.name}，请在 setting/ 下创建并填写"
            " base_url / api_key / model。")
    cp = configparser.RawConfigParser(inline_comment_prefixes=("#", ";"))
    try:
        cp.read(p, encoding="utf-8")
    except (configparser.Error, OSError) as exc:
        raise ConfigError(f"LLM 配置解析失败（{p.name}）: {exc}")

    def g(key, default=None):
        return _get(cp, "llm", key, default)

    for key in ("base_url", "api_key", "model"):
        if not str(g(key, "") or "").strip():
            raise ConfigError(f"LLM 配置缺少必填字段: {key}（{p.name}）")

    def num(key, default, cast):
        try:
            return cast(g(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "base_url": str(g("base_url")).strip(),
        "api_key": str(g("api_key")).strip(),
        "model": str(g("model")).strip(),
        "temperature": num("temperature", 0.95, float),
        "max_tokens": num("max_tokens", 1024, int),
        "reasoning": str(g("reasoning", "true")).strip().lower() in ("1", "true", "yes", "on"),
        "reasoning_effort": str(g("reasoning_effort", "low") or "low").strip(),
    }


# ---------------------------------------------------------------------------
# 用户配置（user.ini）
# ---------------------------------------------------------------------------

def user_ref() -> str:
    """用户称呼（{{user}} 占位符实例化值）；缺省「对方」。"""
    cp = _read_ini("user.ini", inline=True)
    val = _get(cp, "user", "ref", "对方")
    return str(val or "对方").strip() or "对方"


# ---------------------------------------------------------------------------
# 运行时写回（文本级键值替换，保留注释）
# ---------------------------------------------------------------------------

def set_app_value(key: str, value: str) -> None:
    """写回 setting/app.ini 的 [app] 节键值（文本级替换，保留行内注释）。

    供运行时开关（如内容模式切换）使用：configparser 写回会丢弃注释，
    故沿用 launcher 同款文本替换（值中不出现 ; / # —— app.ini 的解析
    本就将其视为行内注释，读写一致）。键不存在时在节尾追加；
    app.ini 缺失时抛 ConfigError（README「架构」约定 2：无兜底默认，缺失即报错）。
    """
    path = SETTING_DIR / "app.ini"
    if not path.exists():
        raise ConfigError("缺少配置文件: app.ini，请从 setting/app.ini.example 复制")
    lines = path.read_text(encoding="utf-8").splitlines()
    sec_idx = next((i for i, ln in enumerate(lines)
                    if re.match(r"^\s*\[app\]\s*$", ln)), None)
    if sec_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("[app]")
        lines.append(f"{key} = {value}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    end = len(lines)
    for i in range(sec_idx + 1, len(lines)):
        if re.match(r"^\s*\[", lines[i]):
            end = i
            break
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*)$")
    for i in range(sec_idx + 1, end):
        m = key_re.match(lines[i])
        if m:
            rest = m.group(1)
            cm = re.search(r"\s*[;#].*$", rest)
            comment = cm.group(0) if cm else ""
            lines[i] = f"{key} = {value}{comment}"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return
    lines.insert(end, f"{key} = {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 运行期热读（M3 配置分层，见 docs/REFACTOR_DESIGN.md §6）
# ---------------------------------------------------------------------------

# 运行期热读键白名单：影响回合行为/功能开关的键，修改 setting/app.ini 后
# 无需重启、下次读取即生效。白名单之外的键是**启动期冻结配置**（外观/行为/
# 上下文窗口等），由调用方在进程启动时读取一次（pet 存 self._cfg 快照）——
# 不得经 app_get 读取，误用会退化为「改了不生效」的旧语义（README「架构」
# 约定 2 精神：错误即报错）。
_HOT_KEYS = frozenset({
    "content_mode", "tool_choice", "log_enabled", "mesugaki_style_block",
    "retry_on_vague_query", "force_say_to_finish",
    "retry_on_repeated_query", "retry_on_multi_query",
    "rmgame_enabled", "rmgame_cdp_enabled", "rmgame_confirm_bubble",
    "monitor_auto_start", "rmgame_monitor_interval",
    "rmgame_auto_discover", "rmgame_nwjs_sdk", "rmgame_env_fresh_seconds",
})

# mtime 缓存：app.ini 未变时复用解析结果（热路径免重复读盘解析）；
# set_app_value / 外部编辑都会改变 mtime，自动失效。
_app_cache = {"mtime": None, "data": None}

# 临时覆盖（离线自测用，app_get 优先读取；调用方负责恢复）
_OVERRIDES: dict = {}


def _cached_app() -> dict:
    """app.ini [app] 节解析结果（mtime 缓存；文件缺失返回空 dict）。"""
    p = SETTING_DIR / "app.ini"
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        mtime = None
    if _app_cache["data"] is not None and _app_cache["mtime"] == mtime:
        return _app_cache["data"]
    cp = _read_ini("app.ini", inline=True)
    out = {}
    if cp.has_section("app"):
        for key, val in cp.items("app"):
            out[key] = _conv(key, val)
    _app_cache["mtime"] = mtime
    _app_cache["data"] = out
    return out


def override(key: str, value):
    """临时覆盖热读键（离线自测用）；恢复 = 覆盖回原值或 clear_overrides。"""
    if key not in _HOT_KEYS:
        raise ConfigError(f"app.ini 键 {key!r} 不在运行期热读白名单，不能覆盖")
    _OVERRIDES[key] = value


def clear_overrides() -> None:
    _OVERRIDES.clear()


def app_get(key: str, default=None):
    """运行期热读 setting/app.ini 的 [app] 键（mtime 缓存）。

    仅白名单热键可读（白名单之外为启动期冻结配置，启动时读取一次）；
    文件缺失/键缺失返回 default（与 app_config 语义一致）。
    """
    if key not in _HOT_KEYS:
        raise ConfigError(
            f"app.ini 键 {key!r} 不在运行期热读白名单（启动期配置请在启动时"
            "读取一次，见 docs/REFACTOR_DESIGN.md §6）")
    if key in _OVERRIDES:
        return _OVERRIDES[key]
    return _cached_app().get(key, default)


# ---------------------------------------------------------------------------
# 离线自测
# ---------------------------------------------------------------------------

def selftest() -> None:
    # 白名单约束：启动期键拒绝热读（错误即报错，防误用退化为"改了不生效"）
    try:
        app_get("scale", 0.6)
        raise AssertionError("scale 为启动期键，不应经 app_get 读取")
    except ConfigError:
        pass
    # 热键可读；文件缺失/键缺失返回默认（真实 app.ini 存在与否均可自测）
    assert app_get("rmgame_enabled", True) in (True, False)
    # 白名单内但 app.ini 未配置的键 → 返回默认（空值字符串或 None 均可）
    nwjs = app_get("rmgame_nwjs_sdk", None)
    assert nwjs is None or isinstance(nwjs, str)
    # override 生效与恢复
    assert app_get("tool_choice", "auto") in ("required", "auto", None)
    settings_saved = app_get("tool_choice", "auto")
    try:
        override("tool_choice", "auto")
        assert app_get("tool_choice", "auto") == "auto"
    finally:
        clear_overrides()
        assert app_get("tool_choice", "auto") == settings_saved or settings_saved is None
    # 类型还原表（含 1.2 新增的重复查询布尔键）
    assert _conv("history_rounds", "30") == 30
    assert _conv("rmgame_monitor_interval", "3.0") == 3.0
    assert _conv("log_enabled", "false") is False
    assert _conv("retry_on_repeated_query", "false") is False, "重复查询开关应布尔还原"
    assert _conv("retry_on_multi_query", "false") is False, "多查询开关应布尔还原"
    assert _conv("content_mode", "nsfw") == "nsfw"
    # mtime 缓存：连续读取返回同一 dict 对象（缓存命中）
    assert _cached_app() is _cached_app()
    print("[settings.selftest] 通过 ✓ app_get 热读 / 白名单约束 / override / 类型还原 / mtime 缓存")


if __name__ == "__main__":
    selftest()
