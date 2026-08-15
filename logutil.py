# -*- coding: utf-8 -*-
"""日志工具 —— 桌宠角色

职责：
- 把每回合「最终交付给 LLM 的消息 + LLM 原始响应 + 解析结果 + 状态变化」
  以可读文本写入根目录 log/ 文件夹
- 滚动保留最近 MAX_LOGS 条（自动删除最旧文件）
- 供 debug.py 读取展示

开关：setting/app.ini 的 log_enabled（默认 True）。
"""

import datetime as _dt
import json
from pathlib import Path

import settings

LOG_DIR = settings.log_dir()
MAX_LOGS = 10            # 保留最近 10 条回合日志
_PREFIX = "round_"


def is_enabled() -> bool:
    return bool(settings.app_get("log_enabled", True))


def _ensure_dir() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR


def _fmt_dict_state(state: dict) -> str:
    """状态（好感度/内心想法）转成一行可读文本。"""
    if not state:
        return "（无）"
    aff = state.get("affection")
    from character import current as char_current
    level_for = char_current().level_for
    aff_s = f"{aff}/100（{level_for(int(aff))}）" if aff is not None else "?"
    skills = state.get("skills") or []
    skills_s = "、".join(skills) if skills else "（无）"
    return f"好感度: {aff_s} | 内心想法: {state.get('inner_thought', '')} | 技能: {skills_s}"


def _fmt_messages(messages: list) -> str:
    """messages 列表 → 分段可读文本（忠实于输入：system 即最终交付的提示词）。

    tool 角色消息带标题与 tool_call_id，与 llm._fmt_messages_for_log 一致，
    保证日志所见即模型所收。
    """
    if not messages:
        return "（空）"
    lines = []
    sys_seen = False
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content") or ""
        if role == "system":
            title = ("SYSTEM（最终交付给 LLM 的提示词）" if not sys_seen
                     else "SYSTEM（附加指令：示例/激活）")
            sys_seen = True
            lines.append(f"────── {title} ──────")
        elif role == "user":
            lines.append("────── USER ──────")
        elif role == "assistant":
            lines.append("────── ASSISTANT（此前回合）──────")
        elif role == "tool":
            content = f"{content}\n（tool_call_id: {m.get('tool_call_id', '')}）"
            lines.append("────── TOOL ──────")
        lines.append(content)
    return "\n".join(lines)


def _fmt_parsed(parsed) -> str:
    if parsed is None:
        return "（未解析）"
    skills = parsed.skills
    if skills is None:
        skills_s = "（未声明，保留原状态）"
    elif not skills:
        skills_s = "（空）"
    else:
        skills_s = "、".join(skills)
    return (
        f"reply: {parsed.reply!r}\n"
        f"affection_delta: {parsed.affection_delta}\n"
        f"inner_thought: {parsed.inner_thought!r}\n"
        f"emote: {parsed.emote} | bounce: {parsed.bounce}\n"
        f"skills: {skills_s}"
    )


def prune() -> None:
    """只保留最近 MAX_LOGS 条日志（文件名按时间戳排序）。"""
    if not LOG_DIR.exists():
        return
    files = sorted(p for p in LOG_DIR.iterdir() if p.name.startswith(_PREFIX) and p.suffix == ".txt")
    for old in files[:-MAX_LOGS]:
        try:
            old.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 通用 LLM 调用日志（统一入口 llm.call_llm 内部自动调用）
# ---------------------------------------------------------------------------

MAX_LLM_LOGS = 50            # 通用 LLM 调用日志保留条数（llm_*.txt）


def log_llm_call(kind: str, prompt: str, response: str,
                 ok: bool = True, note: str = "",
                 reasoning: str = "", cache: str = "") -> None:
    """写一条通用 LLM 调用日志。

    kind：'round' | 'merge' | 'summary' | 'wiki_discovery' | 'wiki_rewrite' 等。
    记录完整交付消息与原始响应（含成功/失败标记），滚动保留 MAX_LLM_LOGS 条。
    cache：缓存命中统计行（llm._cache_stats 生成，如「缓存命中: hit=1536
    miss=5662 命中率=21.3% (总 7198)」），空串不记录。
    由 llm.call_llm 统一调用——所有 LLM 调用经此接口即自动留痕。
    """
    if not is_enabled():
        return
    _ensure_dir()
    now = _dt.datetime.now()
    fname = f"llm_{kind}_{now:%Y%m%d_%H%M%S_%f}.txt"
    head = (f"【LLM 调用日志】kind={kind} | ok={'✓' if ok else '✗'}\n"
            f"note: {note}\n"
            f"time: {now.isoformat(timespec='seconds')}\n"
            + (f"cache: {cache}\n" if cache else "")
            + "----------------------------------------\n"
            "【交付给 LLM 的消息】\n")
    try:
        (LOG_DIR / fname).write_text(
            head + (prompt or "（空）") + "\n"
            "----------------------------------------\n"
            + (("【LLM 推理内容】\n" + (reasoning or "（空）") + "\n"
                "----------------------------------------\n") if reasoning else "")
            + "【LLM 原始响应】\n" + (response or "（空）") + "\n",
            encoding="utf-8")
        _prune_llm_logs()
    except OSError:
        pass  # 日志失败不影响主流程


def _prune_llm_logs() -> None:
    """只保留最近 MAX_LLM_LOGS 条 llm_*.txt。"""
    if not LOG_DIR.exists():
        return
    try:
        files = sorted(LOG_DIR.glob("llm_*.txt"),
                       key=lambda p: p.stat().st_ctime)
        for old in files[:-MAX_LLM_LOGS]:
            old.unlink()
    except OSError:
        pass


def _cache_line(raw_reply) -> str:
    """从响应 dict 的 usage 提取缓存命中统计一行（与 llm._cache_stats 同字段）。

    端点无缓存字段返回空串（不记录）。logutil 为 llm 依赖的叶子模块，
    为避免循环导入不做顶层 import，此处按相同字段自行提取（与 reasoning
    提取同样的处理方式）。
    """
    if not isinstance(raw_reply, dict):
        return ""
    try:
        usage = raw_reply["usage"]
    except (KeyError, TypeError):
        return ""
    if not isinstance(usage, dict):
        return ""
    hit = usage.get("prompt_cache_hit_tokens")
    if hit is None:
        hit = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    total = usage.get("prompt_tokens")
    if hit is None and miss is None:
        return ""
    hit = int(hit or 0)
    miss = int(miss if miss is not None else max(0, int(total or 0) - hit))
    total_n = int(total or (hit + miss))
    ratio = (hit / total_n * 100) if total_n else 0.0
    return f"缓存命中: hit={hit} miss={miss} 命中率={ratio:.1f}% (总 {total_n})"


def log_round(messages: list, raw_reply: str = "", parsed=None,
              state_before: dict = None, state_after: dict = None,
              error: str = "") -> Path:
    """写一条完整回合日志，返回文件路径。"""
    from llm import load_llm_config  # lazy：避免顶层循环依赖
    if not is_enabled():
        return Path()
    _ensure_dir()
    reasoning = ""
    cache = ""
    if isinstance(raw_reply, dict):
        cache = _cache_line(raw_reply)
        try:
            msg = raw_reply["choices"][0]["message"]
            for key in ("reasoning_content", "reasoning"):
                val = msg.get(key)
                if isinstance(val, str) and val.strip():
                    reasoning = val.strip()
                    break
        except (KeyError, IndexError, TypeError):
            pass
        raw_reply = json.dumps(raw_reply, ensure_ascii=False, indent=2)
    now = _dt.datetime.now()
    fname = f"{_PREFIX}{now:%Y%m%d_%H%M%S_%f}.txt"
    path = LOG_DIR / fname

    lines = [
        "=" * 60,
        f"回合日志  {now:%Y-%m-%d %H:%M:%S}",
        f"模型: {load_llm_config().get('model', '?')} | "
        f"端点: {load_llm_config().get('base_url', '?')}",
        "=" * 60,
        "",
        "【最终交付给 LLM 的完整消息】",
        _fmt_messages(messages),
        "",
    ]
    if error:
        lines += ["【错误】", error, ""]
    else:
        lines += [
            "【LLM 推理内容】",
            reasoning or "（无）",
            "",
            "【缓存命中】",
            cache or "（端点未返回缓存字段）",
            "",
            "【LLM 原始响应】",
            raw_reply or "（空）",
            "",
            "【解析结果】",
            _fmt_parsed(parsed),
            "",
            "【状态变化】",
            f"之前: {_fmt_dict_state(state_before)}",
            f"之后: {_fmt_dict_state(state_after)}",
            "",
        ]
    text = "\n".join(lines)

    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)
    prune()
    return path


def list_logs() -> list:
    """按时间升序返回日志文件路径列表。"""
    if not LOG_DIR.exists():
        return []
    return sorted(p for p in LOG_DIR.iterdir() if p.name.startswith(_PREFIX) and p.suffix == ".txt")


def read_log(path) -> str:
    with Path(path).open(encoding="utf-8", newline="") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 离线自测
# ---------------------------------------------------------------------------

def selftest() -> None:
    import tempfile
    # 用临时目录验证写入与滚动保留
    tmp = Path(tempfile.mkdtemp())
    saved = globals()["LOG_DIR"]
    globals()["LOG_DIR"] = tmp  # 临时替换到临时目录
    try:
        for i in range(12):
            log_round([{"role": "system", "content": f"prompt-{i}"},
                       {"role": "user", "content": f"hi-{i}"}],
                      raw_reply=f'{{"reply": "r{i}"}}', parsed=None,
                      state_before={"affection": 20}, state_after={"affection": 20 + i})
        files = list_logs()
        assert len(files) == MAX_LOGS, f"应保留 {MAX_LOGS} 条，实际 {len(files)}"
        assert files[0].name.startswith("round_"), files[0]
        # 12 条保留 10 条 → 删除最旧 2 条（prompt-0、prompt-1），最旧保留 prompt-2
        assert "prompt-0" not in read_log(files[0]), "最旧日志应已滚动"
        assert "prompt-1" not in read_log(files[0]), "最旧日志应已滚动"
        assert "prompt-2" in read_log(files[0])
        assert "prompt-11" in read_log(files[-1])
        # 内容结构
        text = read_log(files[-1])
        assert "最终交付给 LLM 的完整消息" in text
        assert "SYSTEM（最终交付给 LLM 的提示词）" in text
        assert "LLM 原始响应" in text and "好感度: 31/100" in text
        # 缓存命中记录：响应 dict 带 usage → 回合日志含缓存统计行；
        # 无缓存字段 → 标注缺失
        log_round([{"role": "system", "content": "p"}],
                  raw_reply={"choices": [{"message": {"content": "x"}}],
                             "usage": {"prompt_tokens": 100,
                                       "prompt_cache_hit_tokens": 30,
                                       "prompt_cache_miss_tokens": 70}},
                  parsed=None)
        assert "缓存命中: hit=30 miss=70 命中率=30.0% (总 100)" in read_log(list_logs()[-1])
        log_round([{"role": "system", "content": "p"}],
                  raw_reply={"choices": [{"message": {"content": "x"}}]}, parsed=None)
        assert "（端点未返回缓存字段）" in read_log(list_logs()[-1])
        # 通用 LLM 调用日志：cache 参数写入文件头
        log_llm_call("merge", "prompt-x", "resp-y", cache="缓存命中: hit=1 miss=2 命中率=33.3% (总 3)")
        llm_files = sorted(LOG_DIR.glob("llm_*.txt"))
        assert llm_files, "应生成 llm_ 日志"
        assert "cache: 缓存命中: hit=1 miss=2" in llm_files[-1].read_text(encoding="utf-8")
        print(f"[logutil.selftest] 通过 ✓ 保留 {len(files)} 条 | "
              f"最旧含 prompt-2，最新含 prompt-11 | 缓存命中记录 ✓")
    finally:
        globals()["LOG_DIR"] = saved
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    selftest()
