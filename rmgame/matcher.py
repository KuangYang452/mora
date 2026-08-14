# -*- coding: utf-8 -*-
"""OCR 文本 → raw 条目模糊匹配 —— rmgame/matcher

OCR 识别有噪声（错字/漏字/多余符号，如「※」被识别成「六」），无法与
raw 精确匹配；本模块做归一化 + 相似度匹配，把 OCR 文本定位到 raw 中最
相近的条目，从而让环境段/点评能引用**精确原文**。

匹配算法：归一化（去空白/标点/噪声符号）→ difflib.SequenceMatcher
（字符级公共子序列 ratio）。raw 侧覆盖地图事件 + 公共事件（CommonEvents）。
"""

import difflib
import json
import re
from pathlib import Path

from .discovery import RAW_DIR

# 归一化：去除空白、常见标点与 OCR 噪声符号
_NOISE = re.compile(
    r"[\s，。、！？「」『』…·:：;；,.!?()（）\"'※×＊*#＃～~—\-_/\\|]"
)


def normalize(text: str) -> str:
    """去空白/标点/常见 OCR 噪声符号，仅保留有效字符序列。"""
    return _NOISE.sub("", text or "")


def _iter_entries_with_src(slug: str):
    """遍历 raw/<slug> 全部对话条目，附带来源文件名（地图/公共/战斗）。"""
    base = RAW_DIR / slug
    maps_dir = base / "maps"
    if maps_dir.is_dir():
        for f in sorted(maps_dir.glob("Map[0-9]*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError):
                continue
            for e in data.get("entries", []):
                if e.get("text"):
                    yield e, f.name
    for fname, key in (("CommonEvents.json", "common_events"),
                       ("Troops.json", "troops")):
        f = base / fname
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError):
                data = {}
            for e in data.get(key, []):
                if e.get("text"):
                    yield e, fname


def _event_context(slug: str, map_file: str, event_id, page=None) -> list:
    """收集同 (map_file, event_id) 的条目 —— 事件完整对话流。

    page：事件页面索引（同一事件多页 = 多个独立对话阶段）。指定时只返回
    该页面条目；None（旧 raw 无 page 字段）返回全部，向后兼容。
    返回 [{id, speaker, text, page}, ...] 按 raw 中的顺序（事件开头在前）。
    """
    if map_file.startswith("Map"):
        f = RAW_DIR / slug / "maps" / map_file
        key = "entries"
    elif map_file == "CommonEvents.json":
        f = RAW_DIR / slug / "CommonEvents.json"
        key = "common_events"
    else:
        f = RAW_DIR / slug / "Troops.json"
        key = "troops"
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return []
    ctx = []
    for e in data.get(key, []):
        if e.get("event_id") == event_id and e.get("text"):
            ctx.append({"id": e.get("id", ""), "speaker": e.get("speaker"),
                        "text": e["text"], "page": e.get("page")})
    if page is not None:
        same_page = [e for e in ctx if e.get("page") == page]
        if same_page:
            ctx = same_page
    return ctx


def event_key(entry_id: str, page) -> str:
    """事件摘要缓存键：Map.Ev → 同页面共享摘要；旧数据（无 page）不带后缀。"""
    key = ".".join((entry_id or "").split(".")[:2])
    if page is not None:
        key += f".p{page}"
    return key


def match_text(text: str, slug: str, min_score: float = 0.35,
               top: int = 3) -> list:
    """OCR 文本 → raw 最佳匹配条目（附带事件完整上下文）。

    返回 [{id, score, speaker, text, src, event_id, event_context}] 按
    相似度降序（score = 归一化后的 SequenceMatcher ratio，0~1）；无命中
    返回 []。最佳命中的 event_context 为同事件（event_id）的全部条目
    （事件完整对话流，按序），供调用方展示"当前事件上下文"。
    说明：OCR 文本通常只覆盖画面中的一部分，min_score 需兼顾噪声与
    截断；调用方可用返回的精确原文替换 OCR 文本展示。
    """
    nt = normalize(text)
    if not nt:
        return []
    results = []
    for e, src in _iter_entries_with_src(slug):
        nr = normalize(e.get("text", ""))
        if not nr:
            continue
        score = difflib.SequenceMatcher(None, nt, nr, autojunk=False).ratio()
        if score >= min_score:
            results.append({
                "id": e.get("id", ""),
                "score": round(score, 3),
                "speaker": e.get("speaker"),
                "text": e.get("text", ""),
                "src": src,
                "event_id": e.get("event_id"),
                "page": e.get("page"),
            })
    results.sort(key=lambda x: x["score"], reverse=True)
    results = results[:top]
    # 最佳命中附加事件完整上下文（同 event_id 的对话流，事件开头在前）
    if results and results[0].get("event_id") is not None:
        results[0]["event_context"] = _event_context(
            slug, results[0]["src"], results[0]["event_id"],
            page=results[0].get("page"))
    return results
