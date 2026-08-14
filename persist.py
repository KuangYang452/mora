# -*- coding: utf-8 -*-
"""角色状态与对话历史持久化 —— persist

把角色的变量（好感度/内心想法/技能）、未合并的对话历史、历史合并条目与
归档记录保存到存档（settings.save_dir()/pet_session.json，默认 runtime/）：

- 每次回合结束（_finish_agent）与退出（_quit）时保存，启动时恢复；
- history 只保存**未合并**的上下文消息（已合并的进入 archives）；
- merge 为最新一条历史合并条目 {summary, merged_at}（无则 None）；
- archives 为归档记录列表 [{archived_at, summary, merged_summary,
  messages}]（更早一段的旧合并摘要 summary + 本条归档时段的合并摘要
  merged_summary + 被合并的原始消息，供 query_archive 工具查询；
  merged_summary 为新增字段，旧存档缺失时查询端回退 summary）；
- 存档标注 saved_at；每条消息带 time（ISO 时间戳，由 context 写入），
  发往 LLM 的 messages 会剥离 time 并转为相对时间标注；
- 写盘用临时文件 + rename（原子写），失败不抛异常；
- 存档缺失/损坏时加载返回 None，调用方回退初始状态与开场白。
"""

import json
from datetime import datetime
from pathlib import Path

import settings

SESSION_PATH = settings.save_dir() / "pet_session.json"


def save_session(state: dict, history: list, merge=None,
                 archives: list = None) -> bool:
    """保存状态、未合并历史、合并条目与归档；失败不抛异常。"""
    try:
        SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "state": state,
            "history": history,
            "merge": merge,
            "archives": archives or [],
        }
        tmp = SESSION_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(SESSION_PATH)
        return True
    except OSError:
        return False


def load_session() -> dict or None:
    """读存档；不存在/损坏/结构缺失返回 None（调用方回退初始状态）。"""
    if not SESSION_PATH.exists():
        return None
    try:
        data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("history"), list) \
            or not isinstance(data.get("state"), dict):
        return None
    # 旧存档缺字段：补默认（merge=None / archives=[]）
    data.setdefault("merge", None)
    data.setdefault("archives", [])
    return data
