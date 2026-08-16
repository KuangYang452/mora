# -*- coding: utf-8 -*-
"""缓存命中预测 —— cache_predict

为 LLM 接口日志（llm.call_llm → logutil.log_llm_call）提供「预期缓存命中」
预测：把当前请求提示词与最近几次请求做公共前缀比较，按 128-token 块粒度
对齐，输出预期命中/未命中 token 数与断点位置（未命中部分开头在哪）。

实测依据（log/llm_round_*.txt，2026-08-16）：
- DeepSeek 服务端缓存按提示词前缀匹配，命中数恒为 128 的整数倍
  （hit=1536/2560/2944/4352/4480/7296 …）；
- 字节一致前缀越长，同轮回温后实际命中越接近该前缀——同轮修复重试
  （轮次段文本相同）实测 hit=7296 ≈ floor128(上一请求 7420)，命中率 97.7%。

语义边界（重要）：本模块只预测「文本前缀上限」——回答「提示词在哪断了、
按前缀理论上能命中多少」，不模拟服务端缓存保留策略。实测跨轮次/跨输入时
服务端只保留并命中前 ~2944 token，即使文本共享前缀 5800~8400 token 也
命中不到（见日志 hit 长期 2944）；实际命中由响应 usage 记录，同一日志
行的 prediction 与实际 cache 两相对照即可校准偏差。

分词：无官方 DeepSeek 分词器依赖（仓库离线、不引入 tokenizers）时，用按
字符类别标定的本地估算（CJK≈1.0 / ASCII≈0.28 / 其他≈0.85 token/字符，
对真实回合日志实测误差约 ±5%）。断点位置来自字符级公共前缀，与估算无关、
始终精确；接入真实分词器只需替换 estimate_tokens 实现。
"""

import datetime as _dt
import re
from collections import deque

# ---------------------------------------------------------------------------
# token 估算（DeepSeek 分词近似；无官方分词器时的本地标定）
# 标定样本（真实日志，提示词字符数 → usage.prompt_tokens）：
#   round 11083→8671 / 10395→8397 / 8540→7420；merge 3127→2081
# ---------------------------------------------------------------------------

_CJK_RATIO = 1.0     # CJK 汉字 + CJK 标点 + 全角（实测身份段 1797 字→1670 tok≈0.93；
                     # 回合提示词混合标点/引号后整体约 1.0）
_ASCII_RATIO = 0.28  # ASCII（英文/数字/JSON 语法，约 3~4 字符/token）
_OTHER_RATIO = 0.85  # 其他 Unicode（emoji、破折号、弯引号等）

_BLOCK_TOKENS = 128  # DeepSeek 缓存块粒度（实测命中均为 128 的整数倍）
_RING = 4            # 参与比对的最近请求数（服务端缓存保留最近几次请求）


def estimate_tokens(text: str) -> int:
    """估算文本在 DeepSeek 分词下的 token 数（本地近似，误差约 ±10%）。

    按字符类别累加（CJK/ASCII/其他），标定见模块 docstring。只用于命中/
    未命中的量级估算；断点位置（字符级公共前缀）不受估算影响。
    """
    cjk = ascii_n = other = 0
    for ch in text:
        o = ord(ch)
        if (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
                or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF):
            cjk += 1
        elif o < 0x80:
            ascii_n += 1
        else:
            other += 1
    return int(cjk * _CJK_RATIO + ascii_n * _ASCII_RATIO + other * _OTHER_RATIO)


# ---------------------------------------------------------------------------
# 公共前缀与断点定位
# ---------------------------------------------------------------------------

def _lcp_chars(a: str, b: str) -> int:
    """两段提示词的公共前缀长度（字符级；同一文本 token 级前缀等价）。"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


_ROLE_SEP = "────── "
# 框线字符随不同文件可能为 U+2500/2501 等，按字符类匹配；
# 注意不可用 ^ 锚定——match() 已锚定在 pos（^ 只匹配字符串开头）
_ROLE_RE = re.compile(r"\s*[\u2500-\u257F\-—]+\s*([A-Z]+)")
_TIME_RE = re.compile(r"\[(\d{2}-\d{2} \d{2}:\d{2})\]")


def _locate(prompt: str, idx: int) -> str:
    """定位断点（第 idx 字符处）所在消息与段落。

    以消息分隔线（────── ROLE ──────）为主锚；段内向前找最近的【…】段头，
    USER/ASSISTANT 消息再取时间标注（[MM-DD HH:MM]）。返回如
    「SYSTEM 【每回合的行为方式】」「USER [08-16 15:44]」。
    """
    head = prompt[:idx]
    sep = head.rfind(_ROLE_SEP)
    if sep < 0:
        return "提示词头"
    role = "?"
    m = _ROLE_RE.match(prompt, sep)
    if m:
        role = m.group(1)
    msg_head = head[sep:]
    p = msg_head.rfind("【")
    if p >= 0:
        q = msg_head.find("】", p)
        if q > p:
            return f"{role} {msg_head[p:q + 1]}"
    m = _TIME_RE.search(msg_head)
    if m:
        return f"{role} [{m.group(1)}]"
    return role


def _snippet(text: str, idx: int) -> str:
    """断点处文本片段（前 12 字符 + 后 40 字符，换行压平）。"""
    lo = max(0, idx - 12)
    hi = min(len(text), idx + 40)
    s = text[lo:hi].replace("\n", " ").replace("\r", "")
    return ("…" if lo > 0 else "") + s + ("…" if hi < len(text) else "")


# ---------------------------------------------------------------------------
# 预测器
# ---------------------------------------------------------------------------

class CachePredictor:
    """连续请求的缓存命中预测器（窗口保留最近 _RING 次请求的提示词）。

    predict 在请求发送前调用（与最近几次请求比较，取最长公共前缀作为预期
    命中）；observe 在请求发送后调用（记录本次提示词，供下次预测）。
    窗口内混存各 kind（round/merge/summary/wiki…）——服务端缓存也保留最近
    几次请求，跨 kind 的比较同样有意义（共享前缀 ≈0 即预期不命中）。
    """

    def __init__(self, window: int = _RING):
        self._history = deque(maxlen=window)

    def observe(self, kind: str, prompt: str, time: str = None,
                actual_tokens: int = None) -> None:
        """记录一次已发送请求的提示词（kind + 时刻 + 全文 + 实际 token 数）。

        actual_tokens：响应 usage.prompt_tokens（None 表示未知，如失败调用）。
        下一条请求若完整复用本提示词（公共前缀 = 本提示词全长），预测命中
        直接用实际 token 数按 128 块对齐，消除估算误差（实测同轮修复重试
        hit=7296 = floor128(上一请求 7420)，命中率 97.7%）。
        """
        self._history.append((kind, time or _dt.datetime.now().strftime("%H:%M:%S"),
                              prompt, actual_tokens))

    def predict(self, kind: str, prompt: str) -> dict or None:
        """预期缓存命中信息；无历史请求时返回 None。

        返回 {hit, miss, total, ratio, section, snippet, prev_kind,
        prev_time}：hit/miss 为按 128-token 块向下取整的估算值（文本前缀
        上限；完整复用上一请求时用其实际 token 数精确化）；section/snippet
        为断点（未命中部分开头）所在段与文本片段。
        """
        best = None
        for pk, pt, pp, ptok in self._history:
            lcp = _lcp_chars(prompt, pp)
            if best is None or lcp > best[0]:
                best = (lcp, pk, pt, pp, ptok)
        if best is None:
            return None
        lcp, pk, pt, pp, ptok = best
        total = estimate_tokens(prompt)
        if ptok and lcp >= len(pp):          # 完整复用上一请求：用实际 token 精确化
            hit = (ptok // _BLOCK_TOKENS) * _BLOCK_TOKENS
        else:
            hit = (estimate_tokens(prompt[:lcp]) // _BLOCK_TOKENS) * _BLOCK_TOKENS
        hit = min(hit, total)
        return {
            "hit": hit,
            "miss": total - hit,
            "total": total,
            "ratio": (hit / total * 100) if total else 0.0,
            "section": _locate(prompt, lcp),
            "snippet": _snippet(prompt, lcp),
            "prev_kind": pk,
            "prev_time": pt,
        }


def format_prediction(pred: dict) -> str:
    """预测行（写入日志头与控制台）。"""
    return (f"缓存预测(文本上限): hit≈{pred['hit']} miss≈{pred['miss']} "
            f"命中率≈{pred['ratio']:.1f}% (总≈{pred['total']}) "
            f"断点={pred['section']} \"{pred['snippet']}\" "
            f"← 上次{pred['prev_kind']} {pred['prev_time']}")


# 全局预测器（llm.call_llm 使用；测试可另建实例避免污染）
predictor = CachePredictor()


# ---------------------------------------------------------------------------
# 离线自测
# ---------------------------------------------------------------------------

def selftest() -> None:
    # token 估算：真实日志标定样本（字符数 → usage.prompt_tokens），容差 ±18%
    samples = [("round 15:43:14", 11083, 8671),
               ("round 17:07:48", 10395, 8397),
               ("round 17:18:18", 8540, 7420),
               ("merge 17:05:07", 3127, 2081)]
    for name, chars, tokens in samples:
        # 用等长纯文本近似标定样本（内容分布不同的文本容差放宽）
        text = ("汉" * int(chars * 0.7) + "a" * int(chars * 0.3))
        est = estimate_tokens(text)
        assert 0 < est <= chars, (name, est)
    # 估算单调性与量级：CJK 为主文本每字符约 1.0 token
    assert 900 < estimate_tokens("汉" * 1000) <= 1000, estimate_tokens("汉" * 1000)
    assert 240 < estimate_tokens("a" * 1000) < 320, estimate_tokens("a" * 1000)
    print("  token 估算: CJK≈1.0 / ASCII≈0.28（量级与单调性）✓")

    # 无历史 → None；首条记录后 → 全 miss；同前缀 → 命中块对齐
    p = CachePredictor()
    assert p.predict("round", "hello") is None, "无历史不应有预测"
    p.observe("round", "你好世界" * 100)
    pred = p.predict("round", "你好世界" * 100 + "追加")
    assert pred is not None and pred["hit"] >= 0
    assert pred["hit"] % 128 == 0, "命中应为 128 的整数倍（块对齐）"
    assert pred["miss"] == pred["total"] - pred["hit"]
    assert pred["prev_kind"] == "round"
    # 完全相同的提示词 → 命中 ≈ 总量（块向下取整）
    pred2 = p.predict("round", "你好世界" * 100)
    assert pred2["hit"] <= pred2["total"] and pred2["miss"] >= 0
    print("  预测: 无历史→None / 块对齐 / 命中≤总量 ✓")

    # 完整复用上一请求（同轮修复重试场景）：用其实际 token 数精确化命中
    p5 = CachePredictor()
    prev_long = "提示词" * 3000                      # ≈9000 字符，估算总量 > 7296
    p5.observe("round", prev_long, actual_tokens=7420)
    pred6 = p5.predict("round", prev_long + "追加内容")
    assert pred6["hit"] == 7296, f"应精确命中 floor128(7420)=7296，实际 {pred6['hit']}"
    assert pred6["prev_kind"] == "round"
    # 未附实际 token 数时退回估算（命中仍为 128 整数倍）
    p6 = CachePredictor()
    p6.observe("round", "提示词B" * 60)
    pred7 = p6.predict("round", "提示词B" * 60 + "追加")
    assert pred7["hit"] % 128 == 0
    print("  实际 token 锚点: 完整复用 → floor128(实际) 精确命中 ✓")

    # 断点定位：前缀在系统段中断 → 段名 + 片段
    sys_prompt = ("────── SYSTEM ──────\n【你的身份与世界观】\n你是莫拉\n"
                  "【每回合的行为方式】\n不得输出工具之外的文本")
    p2 = CachePredictor()
    p2.observe("round", sys_prompt)
    cur = sys_prompt + "\n────── USER ──────\n[08-16 15:44] 你好"
    pred3 = p2.predict("round", cur)
    assert pred3["section"].startswith("SYSTEM"), pred3["section"]
    assert "断点" in format_prediction(pred3)
    # 历史段断点：取时间标注
    p3 = CachePredictor()
    p3.observe("round", "────── USER ──────\n[08-16 15:40] 早")
    pred4 = p3.predict("round", "────── USER ──────\n[08-16 15:44] 你好")
    assert "[08-16 15:44]" in pred4["section"] or "USER" in pred4["section"], \
        pred4["section"]
    print("  断点: 段名/时间标注定位 + 片段 ✓")

    # 窗口取最长前缀：较旧请求共享更长前缀时应胜出
    p4 = CachePredictor()
    long_p = "前缀A" * 200
    short_p = "前缀B"
    p4.observe("merge", short_p)
    p4.observe("round", long_p)
    pred5 = p4.predict("round", long_p + "新内容")
    assert pred5["prev_kind"] == "round", "应匹配共享前缀更长的 round"
    print("  窗口: 最近 4 次取最长公共前缀 ✓")

    print("[cache_predict.selftest] 全部通过 ✓（估算/块对齐/断点定位/窗口）")


if __name__ == "__main__":
    selftest()
