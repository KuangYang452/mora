# -*- coding: utf-8 -*-
"""rmgame 门面 —— 供 pet.py 调用的薄封装

pet 只依赖本门面与 rmgame.bridge（工具执行体），不再直接 import rmgame
内部模块（monitor/matcher/summarizer/llmfmt/discovery）；rmgame 内部
重构不会波及 pet。各函数为内部实现的直接转发，无额外逻辑。
"""

from .discovery import approve, load_games
from .matcher import event_key
from .monitor import load_current, monitor_loop_all, write_current
from .llmfmt import build_event_context
from .summarizer import summarize_event


def games() -> list:
    """游戏库列表（runtime/games.json）。"""
    return load_games()


def approve_game(slug: str):
    """升级游戏信任 auto → user（解锁启动）。"""
    return approve(slug)


def snapshot() -> dict:
    """当前文本快照（runtime/current.json）；无/损坏返回 None。"""
    return load_current()


def save_snapshot(snap: dict) -> None:
    """原子写 runtime/current.json。"""
    write_current(snap)


def monitor_loop(games: list, interval: float, stop_event=None,
                 on_auto_register=None) -> int:
    """守护轮询：读游戏状态 → 写快照（含自动发现入库）。"""
    return monitor_loop_all(games, interval=interval, stop_event=stop_event,
                            on_auto_register=on_auto_register)


def ev_key(match_id: str, page=None) -> str:
    """事件摘要缓存键（Map.Ev → 同页面共享摘要）。"""
    return event_key(match_id, page)


def event_context(slug: str, match_id: str) -> str:
    """事件完整上下文（LLM 友好格式）。"""
    return build_event_context(slug, match_id)


def event_summary(slug: str, ev_key: str, full: str) -> str:
    """生成/读取事件摘要（懒构建缓存 runtime/event_summary/）。"""
    return summarize_event(slug, ev_key, full)
