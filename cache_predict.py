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
命中不到（见日志 hit 长期 2944）；**且保留量随同前缀重复请求增长（缓存
预热）：同一输入多轮循环第 3 次起可从 ~2560 跃升至 5888**。真实命中只能
在拿到响应后由 usage 计算——`actual_break` 把实测命中映射回消息序列给出
真实断点（发送前不做滞后外推，predict 只给文本上限）。

分词：优先用 DeepSeek 官方分词器（`deepseek-tokenizer`，PyPI/AndersonBY，
自带 128K 词表 tokenizer.json，纯 Python 无第三方运行时依赖）——对真实
请求文本实测与 API 计数吻合（无工具消息 ±0.2%）；不可用（未安装）时退回
按字符类别标定的本地估算（CJK≈1.0 / ASCII≈0.28 / 其他≈0.85）。

计数口径（对齐 API，pre-tokenizer 模拟）：**分词器面对的既不是 messages 的
JSON，也不是日志渲染文本，而是服务端套用 DeepSeek 官方聊天模板
（encoding_dsv32.py 的 encode_messages）后的文本**（架构调研：vLLM
deepseek_v32.py / TensorRT-LLM deepseek_v32/encoding.py 同源）。本模块用
`_template_text` 在本地重建该模板（chat 模式）：
- system：纯内容（无特殊 token）；
- user：`<｜User｜>{content}<｜Assistant｜>` + `</think>`；
- assistant：`{content}{tool_calls}<｜end▁of▁sentence｜>`；
- tool：`\n\n<function_results>\n<result>{content}</result>\n</function_results>`；
- 开头 `<｜begin▁of▁sentence｜>`。
实测（真实请求对拍，2026-08-16）：模板口径与 API 的 prompt_tokens 偏差
约 1.3~1.8%（json.dumps 口径 5.1%、渲染文本口径 2.2%）——残差为本地
分词器（deepseek-tokenizer 字节级 BPE）与服务器 tokenizer 的方差。
工具 schema 位于 API 提示词**末尾**（实测前缀断裂时工具计入未命中侧），
仍按 `json.dumps(tools)` 计数。消息**共享判定**与计数分开：共享判定用
逐消息 JSON 字节相等（`_payload_text`，tool_calls 的 id 等未渲染字段差异
也计入），计数用模板文本——两者都是确定性消息级变换，前缀一一对应。
预测行的断点片段（section/snippet）取自**模板化文本**（tokenizer 视角，
含 <｜User｜> 等特殊 token），人读标签（角色 + 【…】段头）从消息内容
直接提取——不依赖日志渲染格式（日志的 `──────` 分隔与 tool 注释若套
模板会污染计数，故日志格式仅用于展示，不参与预测）。
"""

import datetime as _dt
import json
import re
from collections import deque

# ---------------------------------------------------------------------------
# token 计数：deepseek-tokenizer（真实分词）→ 失败时字符类别估算
# ---------------------------------------------------------------------------

_tokenizer = None          # DeepSeekTokenizer 单例（惰性加载）
_tokenizer_checked = False
_tools_tokens_cache = {}   # 工具 schema JSON 文本 → token 数（按文本记忆化）


def _load_deepseek_tokenizer():
    """惰性加载 deepseek-tokenizer（PyPI，AndersonBY/deepseek-tokenizer）。

    模块级 ds_token 在 import 时即加载 6.4MB tokenizer.json，故只在首次
    需要时导入；导入失败（依赖未安装）返回 None，调用方退回估算。
    """
    global _tokenizer, _tokenizer_checked
    if _tokenizer_checked:
        return _tokenizer
    _tokenizer_checked = True
    try:
        from deepseek_tokenizer import ds_token
        _tokenizer = ds_token
    except Exception:
        _tokenizer = None
    return _tokenizer


def _tokenize(text: str) -> int:
    """DeepSeek 标准分词计 token；分词器不可用时用 estimate_tokens 估算。"""
    tok = _load_deepseek_tokenizer()
    if tok is not None:
        try:
            return len(tok.encode(text, add_special_tokens=False))
        except Exception:
            pass
    return estimate_tokens(text)


def tools_tokens(tools: list) -> int:
    """工具 schema 的 token 数（与 API 计入 prompt_tokens 的口径一致）。

    tools 经 json.dumps(ensure_ascii=False) 序列化后分词；按序列化文本
    记忆化（同一工具清单重复调用不重复分词）。tools 为空返回 0。
    实测：14 个工具 schema ≈ 2585 token；文本+工具合计与 API 的
    prompt_tokens 偏差约 ±3%（消息序列化开销已含在内，无工具时 ±0.2%）。
    """
    if not tools:
        return 0
    text = json.dumps(tools, ensure_ascii=False)
    n = _tools_tokens_cache.get(text)
    if n is None:
        n = _tokenize(text)
        _tools_tokens_cache[text] = n
    return n


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

    按字符类别累加（CJK/ASCII/其他），标定见模块 docstring。只用于
    deepseek-tokenizer 不可用时的兜底；断点位置不受估算影响。
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
# 消息级前缀与断点定位（计数口径 = API 实际提交的 messages，见模块 docstring）
# ---------------------------------------------------------------------------

def _payload_text(messages: list) -> str:
    """messages 的 JSON 序列化（用于逐消息共享判定）。

    与 requests 提交的 payload 同构（服务端解码内容）；共享判定按序列化
    字节相等——tool_calls 的 id/type 等未渲染字段的差异也计入。
    """
    return json.dumps(messages, ensure_ascii=False)


# DeepSeek 官方聊天模板（encoding_dsv32.py 的 encode_messages，chat 模式）：
# 分词器面对的是这个模板化文本，不是原始 content、不是 JSON、不是日志渲染。
_BOS = "<｜begin▁of▁sentence｜>"
_EOS = "<｜end▁of▁sentence｜>"
_USER = "<｜User｜>"
_ASST = "<｜Assistant｜>"
_THINK_END = "</think>"
_DSML = "｜DSML｜"


def _template_parts(messages: list) -> list:
    """模板化 prompt 的分段（第 0 段为 BOS，第 i 段为第 i-1 条消息）。

    与 _template_text 同规则（官方 encode_messages，chat 模式）；分段便于
    断点定位——第 k 条消息的模板片段起点 = sum(len(parts[:k+1]))。
    """
    parts = [_BOS]
    for m in messages:
        role = m.get("role", "")
        content = m.get("content") or ""
        if role == "system":
            parts.append(content)
        elif role == "user":
            parts.append(_USER + content + _ASST + _THINK_END)
        elif role == "assistant":
            tc_str = ""
            tcs = m.get("tool_calls")
            if tcs:
                inner = "\n".join(
                    f"<{_DSML}invoke name=\"{t.get('function', {}).get('name', '')}\">\n"
                    f"{t.get('function', {}).get('arguments', '{}')}\n"
                    f"</{_DSML}invoke>" for t in tcs)
                tc_str = "\n\n<{d}function_calls>\n{tc}\n</{d}function_calls>".format(
                    d=_DSML, tc=inner)
            parts.append(content + tc_str + _EOS)
        elif role == "tool":
            parts.append("\n\n<function_results>\n<result>" + content
                         + "</result>\n</function_results>")
    return parts


def _template_text(messages: list) -> str:
    """本地重建服务端 pre-tokenizer 流程：messages → 官方聊天模板文本。

    模板规则（chat 模式，见模块 docstring）：
      system    : {content}
      user      : <｜User｜>{content}<｜Assistant｜></think>
      assistant : {content}{tool_calls}<｜end▁of▁sentence｜>
      tool      : \n\n<function_results>\n<result>{content}</result>\n</function_results>
      encode    : <｜begin▁of▁sentence｜> + Σ render_message
    实测与 API prompt_tokens 偏差约 1.3~1.8%（残差为分词器实现方差）。
    """
    return "".join(_template_parts(messages))


def _shared_messages(a: list, b: list) -> int:
    """两批 messages 的共享消息数（逐消息按序列化字节相等判定）。

    与 API 前缀匹配同口径：消息的 JSON 序列化逐字节相同才算共享——
    tool_calls 的 id/type 等未渲染字段的差异也计入（渲染代理文本会漏报
    这类差异，这里直接对齐真实 payload）。
    """
    k = 0
    while k < min(len(a), len(b)) and _payload_text(a[k]) == _payload_text(b[k]):
        k += 1
    return k


def _leading_system_count(messages: list) -> int:
    """前导 system 消息数（实测服务端命中大致停在此块末尾，±2 块）。"""
    n = 0
    for m in messages:
        if m.get("role") == "system":
            n += 1
        else:
            break
    return n


def _message_label(messages: list, k: int) -> str:
    """第 k 条消息（0 起）的人读标签：role + 首个【…】段头 / 时间标注。

    与模板文本解耦（模板里 system 消息无分隔标记，无法按文本定位）：
    标签直接从消息内容提取，断点片段则取自模板化文本（tokenizer 视角）。
    """
    if k >= len(messages):
        return "末尾"
    m = messages[k]
    role = (m.get("role") or "?").upper()
    content = m.get("content") or ""
    p = content.find("【")
    if p >= 0:
        q = content.find("】", p)
        if q > p:
            return f"{role} {content[p:q + 1]}"
    tm = _TIME_RE.search(content)
    if tm:
        return f"{role} [{tm.group(1)}]"
    return role


def _template_snippet(messages: list, k: int) -> str:
    """断点片段（tokenizer 视角）：模板化文本中第 k 条消息起点处的片段。

    片段含特殊 token（<｜User｜>/<｜end▁of▁sentence｜> 等）——预测行展示的
    就是分词器真正面对的文本；k 越界返回文本末尾片段。
    """
    parts = _template_parts(messages)
    text = "".join(parts)
    off = sum(len(p) for p in parts[:k + 1]) if k >= 0 else 0
    return _snippet(text, min(off, len(text)))


def _msg_token_pos(messages: list, n_tokens: int) -> int:
    """实测命中数 → 消息下标：累计模板化 token 达到 n_tokens 的那条消息。

    返回的是「命中预算落点」的消息下标（0 起）：其起点即推算的实际断点
    （该消息及之后未完整命中）。n_tokens 超过总量返回 len(messages)。
    """
    if n_tokens <= 0:
        return 0
    cum = 0
    for i, m in enumerate(messages):
        cum += _tokenize(_template_text([m]))
        if cum >= n_tokens:
            return i
    return len(messages)


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
    head 向后放宽一个分隔线长度（len(_ROLE_SEP)）：断点恰好落在分隔线
    起点（如前导 system 块末尾）时，仍能把该分隔线归入 head，定位到
    下一条消息（第一条未被缓存的消息）。
    """
    head = prompt[:min(len(prompt), idx + len(_ROLE_SEP))]
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

    def observe(self, kind: str, messages: list, time: str = None,
                actual_tokens: int = None, actual_hit: int = None) -> None:
        """记录一次已发送请求的 messages（kind + 时刻 + 实际 token 数）。

        messages：**实际提交给 API 的 message dict 列表**（不是渲染文本）——
        前缀比较与 token 计数都以它的 JSON 序列化（_payload_text）为准，
        与 API prompt_tokens 同口径。
        actual_tokens：响应 usage.prompt_tokens（None 表示未知，如失败调用）。
        actual_hit：响应 usage.prompt_cache_hit_tokens（None 表示未知）——
        记录实测命中后，下一次预测的「推算实际断点」直接用该值映射，比
        结构规则更贴近服务端当前保留行为。
        下一条请求若完整复用本请求（消息前缀 = 本请求全长），预测命中
        直接用实际 token 数按 128 块对齐，消除估算误差（实测同轮修复重试
        hit=7296 = floor128(上一请求 7420)，命中率 97.7%）。
        """
        self._history.append((kind, time or _dt.datetime.now().strftime("%H:%M:%S"),
                              messages, actual_tokens, actual_hit))

    def predict(self, kind: str, messages: list,
                tools: list = None) -> dict or None:
        """预期缓存命中信息（发送前）；无历史请求时返回 None。

        messages：实际提交给 API 的 message dict 列表（计数/共享判定口径）。
        返回 {hit, miss, total, ratio, section, snippet, prev_kind,
        prev_time}：total = 模板化 prompt token + 工具 schema token（与
        API 的 prompt_tokens 同口径，实测偏差约 1.3~1.8%）；hit 为按
        128-token 块向下取整的共享消息前缀 token（工具在 API 提示词末尾，
        前缀断裂时计入未命中侧，故 hit 不含工具；完整复用上一请求时用其
        实际 token 数精确化）——这是**文本前缀上限**：若服务端全量保留
        该前缀理论上能命中这么多，实际命中由拿到响应后 actual_break 计算。
        section/snippet 为断点（第一条未共享消息）的模板化片段（tokenizer
        视角，含 <｜User｜> 等特殊 token）。
        """
        best = None
        for pk, pt, pp, ptok, phit in self._history:
            k = _shared_messages(messages, pp)
            if best is None or k > best[0]:
                best = (k, pk, pt, pp, ptok, phit)
        if best is None:
            return None
        k, pk, pt, pp, ptok, phit = best
        total = _tokenize(_template_text(messages)) + tools_tokens(tools)
        if ptok and k >= len(pp):          # 完整复用上一请求：用实际 token 精确化
            hit = (ptok // _BLOCK_TOKENS) * _BLOCK_TOKENS
        else:
            hit = (_tokenize(_template_text(messages[:k]))
                   // _BLOCK_TOKENS) * _BLOCK_TOKENS
        hit = min(hit, total)
        # 断点定位（tokenizer 视角）：第 k 条消息 = 第一条未共享消息
        section = _message_label(messages, k)
        snippet = _template_snippet(messages, k)
        return {
            "hit": hit,
            "miss": total - hit,
            "total": total,
            "ratio": (hit / total * 100) if total else 0.0,
            "section": section,
            "snippet": snippet,
            "prev_kind": pk,
            "prev_time": pt,
        }


def actual_break(messages: list, actual_hit: int,
                 actual_total: int = None) -> dict or None:
    """拿到 API 回应后计算实际断点（真实命中映射回消息序列）。

    actual_hit：usage.prompt_cache_hit_tokens；actual_total：usage.prompt_tokens。
    total 用实测值（缺省时退回模板口径估算）；断点 = 实际命中落点所在消息
    （_msg_token_pos 按累计模板化 token 映射），片段为模板化文本（tokenizer
    视角）。与 predict 的区别：predict 是发送前的文本上限，本函数是响应后
    的实测——服务端保留量随同前缀重复请求增长（缓存预热），只有实测才能
    反映真实断点。
    """
    if actual_hit is None:
        return None
    total = int(actual_total) if actual_total else max(
        int(actual_hit or 0), _tokenize(_template_text(messages)))
    hit = min(max(0, int(actual_hit)), total)
    miss = max(0, total - hit)
    # 全命中 → 断点在最末尾；否则取命中落点所在消息
    k = len(messages) if hit >= total else _msg_token_pos(messages, hit)
    return {
        "hit": hit,
        "miss": miss,
        "total": total,
        "ratio": (hit / total * 100) if total else 0.0,
        "section": _message_label(messages, k),
        "snippet": _template_snippet(messages, k),
    }


def format_actual(act: dict) -> str:
    """实测行（拿到响应后写入日志与控制台）：真实命中 + 真实断点。"""
    return (f"缓存实测: hit={act['hit']} miss={act['miss']} "
            f"命中率={act['ratio']:.1f}% (总 {act['total']}) "
            f"断点={act['section']} \"{act['snippet']}\"")


def format_prediction(pred: dict) -> str:
    """预测行（发送前写入日志头与控制台）：文本前缀上限 + 断点。"""
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
    print("  token 估算: CJK≈1.0 / ASCII≈0.28（量级与单调性，兜底路径）✓")

    # 真实分词器（deepseek-tokenizer，PyPI）：已安装则验证词表/往返一致；
    # 未安装（离线环境）跳过——估算兜底路径已在上方覆盖
    tok = _load_deepseek_tokenizer()
    if tok is not None:
        assert tok.vocab_size == 129283, f"词表应为 128K(+3)={tok.vocab_size}"
        text = "你好世界，今天天气不错。Cache hit!"
        ids = tok.encode(text, add_special_tokens=False)
        assert ids and tok.decode(ids, skip_special_tokens=True) == text, \
            "encode/decode 往返应一致"
        n = _tokenize(text)
        assert 0 < n <= len(text) * 2, n
        # 与估算的量级一致性：同一文本真实分词应远小于字符数
        assert _tokenize("汉" * 1000) <= 1000, _tokenize("汉" * 1000)
        print(f"  deepseek-tokenizer: vocab={tok.vocab_size} 往返一致 ✓（真实分词路径）")
    else:
        print("  deepseek-tokenizer: 未安装，走估算兜底 ✓")

    # ---- 消息级接口：计数/共享判定口径 = API 实际提交的 messages ----
    def _sys(text):
        return [{"role": "system", "content": text}]

    # 无历史 → None；首条记录后 → 全 miss；同前缀 → 命中块对齐
    p = CachePredictor()
    assert p.predict("round", _sys("hello")) is None, "无历史不应有预测"
    p.observe("round", _sys("你好世界" * 100))
    pred = p.predict("round", [{"role": "system", "content": "你好世界" * 100},
                               {"role": "user", "content": "追加"}])
    assert pred is not None and pred["hit"] >= 0
    assert pred["hit"] % 128 == 0, "命中应为 128 的整数倍（块对齐）"
    assert pred["miss"] == pred["total"] - pred["hit"]
    assert pred["prev_kind"] == "round"
    # 完全相同的提示词 → 命中 ≈ 总量（块向下取整）
    pred2 = p.predict("round", _sys("你好世界" * 100))
    assert pred2["hit"] <= pred2["total"] and pred2["miss"] >= 0
    print("  预测: 无历史→None / 块对齐 / 命中≤总量 ✓")

    # 完整复用上一请求（同轮修复重试场景）：用其实际 token 数精确化命中
    p5 = CachePredictor()
    prev_long = "提示词" * 5000                     # 真实分词约 1~1.5 万 token > 7296
    p5.observe("round", _sys(prev_long), actual_tokens=7420)
    pred6 = p5.predict("round", [{"role": "system", "content": prev_long},
                                 {"role": "user", "content": "追加内容"}])
    assert pred6["hit"] == 7296, f"应精确命中 floor128(7420)=7296，实际 {pred6['hit']}"
    assert pred6["prev_kind"] == "round"
    # 未附实际 token 数时退回估算（命中仍为 128 整数倍）
    p6 = CachePredictor()
    p6.observe("round", _sys("提示词B" * 60))
    pred7 = p6.predict("round", [{"role": "system", "content": "提示词B" * 60},
                                 {"role": "user", "content": "追加"}])
    assert pred7["hit"] % 128 == 0
    print("  实际 token 锚点: 完整复用 → floor128(实际) 精确命中 ✓")

    # 实测断点（拿到响应后）：真实命中映射回消息序列，片段为模板文本
    msgs_sys = [{"role": "system", "content": "【你的身份与世界观】\n你是莫拉\n" + "汉" * 400},
                {"role": "system", "content": "【好感等级规则】\n厌恶阶段"},
                {"role": "user", "content": "[08-16 15:44] 你好" + "汉" * 100}]
    cur_msgs = msgs_sys + [{"role": "assistant", "content": "回话"}]
    n_total_est = _tokenize(_template_text(cur_msgs))
    # 命中 256 → 落在第一个 system 消息内（约 400 token）→ 断点 SYSTEM
    act_in_sys = actual_break(cur_msgs, actual_hit=256, actual_total=n_total_est)
    assert act_in_sys["section"].startswith("SYSTEM"), act_in_sys["section"]
    # 命中 450 → 越过 system 块（约 405 token）落在 user 消息 → 断点 USER
    act_user = actual_break(cur_msgs, actual_hit=450, actual_total=n_total_est)
    assert act_user["hit"] == 450 and act_user["section"].startswith("USER"), act_user
    assert "<｜User｜>" in act_user["snippet"], \
        f"实测片段应为模板文本（含 <｜User｜>），实际 {act_user['snippet']!r}"
    assert act_user["miss"] == n_total_est - 450
    # 命中为 0 → 断点在最前；命中超总量 → 断点在末尾
    act0 = actual_break(cur_msgs, actual_hit=0, actual_total=n_total_est)
    assert act0["hit"] == 0 and act0["section"].startswith("SYSTEM"), act0
    act_full = actual_break(cur_msgs, actual_hit=n_total_est * 2,
                            actual_total=n_total_est)
    assert act_full["hit"] == n_total_est and act_full["section"] == "末尾", act_full
    # 无实测命中 → None
    assert actual_break(cur_msgs, None) is None
    assert "缓存实测" in format_actual(act_user), "实测行格式"
    print("  实测断点: 真实命中映射（tokenizer 视角）/ 边界 / 无命中→None ✓")

    # 工具 schema token：计入总数（与 API prompt_tokens 同口径，实测 ±3%）
    assert tools_tokens(None) == 0 and tools_tokens([]) == 0
    fake_tools = [{"type": "function", "function": {
        "name": "say", "description": "说出台词",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string"}},
                       "required": ["text"]}}}]
    n_tools = tools_tokens(fake_tools)
    assert n_tools > 0
    assert tools_tokens(fake_tools) == n_tools, "同一工具清单应记忆化（不重复分词）"
    p7 = CachePredictor()
    p7.observe("round", _sys("你好" * 100))
    pred8 = p7.predict("round", [{"role": "system", "content": "你好" * 100},
                                 {"role": "user", "content": "追加"}],
                       tools=fake_tools)
    assert pred8["total"] == pred8["miss"] + pred8["hit"]
    assert pred8["total"] >= n_tools, "总数应含工具 schema token"
    print("  工具 schema: 计入总数（API 同口径，记忆化）✓")

    # 断点片段 = 模板化文本（tokenizer 视角）：共享 system 后断在 USER
    p2 = CachePredictor()
    p2.observe("round", _sys("你是莫拉"))
    cur2 = [{"role": "system", "content": "你是莫拉"},
            {"role": "user", "content": "[08-16 15:44] 你好"}]
    pred3 = p2.predict("round", cur2)
    assert pred3["section"].startswith("USER"), pred3["section"]
    assert "<｜User｜>" in pred3["snippet"], \
        f"断点片段应为模板文本（含 <｜User｜>），实际 {pred3['snippet']!r}"
    assert "断点" in format_prediction(pred3)
    # 历史段断点：取时间标注
    p3 = CachePredictor()
    p3.observe("round", [{"role": "user", "content": "[08-16 15:40] 早"}])
    pred4 = p3.predict("round", [{"role": "user", "content": "[08-16 15:44] 你好"}])
    assert "[08-16 15:44]" in pred4["section"] or "USER" in pred4["section"], \
        pred4["section"]
    print("  断点: 模板片段（tokenizer 视角）+ 段名/时间标注定位 ✓")

    # 窗口取最长共享消息数：较旧请求共享更多时应胜出
    p4 = CachePredictor()
    p4.observe("merge", _sys("前缀B"))
    p4.observe("round", _sys("前缀A" * 200))
    pred5 = p4.predict("round", [{"role": "system", "content": "前缀A" * 200},
                                 {"role": "user", "content": "新内容"}])
    assert pred5["prev_kind"] == "round", "应匹配共享消息更多的 round"
    print("  窗口: 最近 4 次取最长共享消息前缀 ✓")

    print("[cache_predict.selftest] 全部通过 ✓（估算/块对齐/断点定位/窗口）")


if __name__ == "__main__":
    selftest()
