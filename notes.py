# -*- coding: utf-8 -*-
"""角色私有笔记工具 —— 文本持久化（mora_notes）

给角色的工具通道提供执行体：mora_notes 工具 → 语义化结果文本
（供 pet.py 的 agent 循环作为 tool 消息回传给 LLM）。

权限边界（机制约束，不依赖 LLM 自觉）：
- 只允许操作专属笔记目录 runtime/notes/ 内的 .txt 文件；
  笔记名做严格校验（拒绝路径分隔符 / 隐藏名 / 越界），resolve 后
  再校验父目录，双保险杜绝路径穿越。
- 笔记内容**不设任何主题限制**：角色自行决定记什么
  （感想、观察、资料摘录、给未来自己的留言等），程序只负责存取。
- 写操作自动创建目录；编码统一 UTF-8。
"""

import re
from pathlib import Path

import settings

# 角色私有笔记目录（runtime/notes/，随 runtime 目录中心化）
NOTES_DIR = settings.runtime_dir() / "notes"

# 笔记名：中文/字母/数字开头，可含下划线、连字符、点、空格，长度 1~64
_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff\-_. ]{0,63}$")

_ACTIONS = ("list", "read", "write", "append", "delete")


def _safe_name(name) -> tuple:
    """校验并规范化笔记名 → (规范名带.txt, None) 或 (None, 错误文本)。"""
    s = str(name or "").strip()
    if not s:
        return None, "缺少笔记名（name 参数）。"
    if "/" in s or "\\" in s or s in (".", ".."):
        return None, "笔记名非法：不得包含路径分隔符，也不得是 . 或 .."
    if not _NAME_RE.match(s):
        return None, ("笔记名非法：仅允许中文、字母、数字、下划线、连字符、"
                      "点和空格，且以非点字符开头，长度 1~64")
    return s + ".txt", None


def _resolve(name) -> tuple:
    """解析笔记真实路径（确保落在 NOTES_DIR 内）→ (Path, None) 或 (None, 错误)。"""
    fn, err = _safe_name(name)
    if err:
        return None, err
    try:
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"笔记目录不可用：{exc}"
    base = NOTES_DIR.resolve()
    p = (NOTES_DIR / fn).resolve()
    if p.parent != base:
        return None, "笔记名非法：越出笔记文件夹。"
    return p, None


def _list() -> str:
    try:
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(NOTES_DIR.glob("*.txt"))
    except OSError as exc:
        return f"笔记目录不可用：{exc}"
    if not files:
        return "（笔记文件夹是空的。你可以用 write 写下第一篇笔记。）"
    lines = [f"- {f.stem}（{f.stat().st_size} 字节）" for f in files]
    return f"现有笔记 {len(files)} 篇：\n" + "\n".join(lines)


def _read(p: Path, name: str) -> str:
    if not p.exists():
        return (f"笔记「{name}」不存在。可用 list 查看现有笔记，"
                "或用 write 新建一篇。")
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"读取笔记「{name}」失败：{exc}"
    return f"笔记「{name}」内容（{len(text)} 字符）：\n{text}"


def _write(p: Path, name: str, content) -> str:
    existed = p.exists()
    try:
        p.write_text(str(content or ""), encoding="utf-8")
    except OSError as exc:
        return f"写入笔记「{name}」失败：{exc}"
    action = "覆盖更新" if existed else "新建"
    return f"已{action}笔记「{name}」（{len(str(content or ''))} 字符）。"


def _append(p: Path, name: str, content) -> str:
    text = str(content or "")
    if not text:
        return f"追加失败：content 为空（append 需要提供要追加的文本）。"
    try:
        if p.exists():
            with open(p, "a", encoding="utf-8") as f:
                f.write("\n" + text if p.stat().st_size else text)
            return f"已在笔记「{name}」末尾追加（{len(text)} 字符）。"
        p.write_text(text, encoding="utf-8")
        return f"笔记「{name}」尚不存在，已按追加内容新建（{len(text)} 字符）。"
    except OSError as exc:
        return f"追加笔记「{name}」失败：{exc}"


def _delete(p: Path, name: str) -> str:
    if not p.exists():
        return f"笔记「{name}」不存在，无需删除。"
    try:
        p.unlink()
    except OSError as exc:
        return f"删除笔记「{name}」失败：{exc}"
    return f"已删除笔记「{name}」。"


def execute(action: str, args: dict) -> str:
    """mora_notes 工具执行入口 → 语义化中文结果文本。"""
    act = str(action or "").strip()
    if act not in _ACTIONS:
        return (f"未知操作「{act or '（空）'}」，支持："
                + " / ".join(_ACTIONS))
    args = args or {}
    if act == "list":
        return _list()
    name = str(args.get("name") or "").strip()
    p, err = _resolve(name)
    if err:
        return err
    if act == "read":
        return _read(p, name)
    if act == "write":
        return _write(p, name, args.get("content"))
    if act == "append":
        return _append(p, name, args.get("content"))
    if act == "delete":
        return _delete(p, name)
    return f"未知操作「{act}」。"  # 理论不可达（act 已校验）
