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
  不静默兜底（延续原架构约定）。
"""

import configparser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SETTING_DIR = ROOT / "setting"

# 版本号唯一事实来源（语义化版本 MAJOR.MINOR.PATCH，见 docs/VERSIONING.md）：
# 发布流程 = 全量自测 → CHANGELOG.md 归档 → 更新本常量 → 提交 → git tag v<版本>。
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
