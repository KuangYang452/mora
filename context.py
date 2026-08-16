# -*- coding: utf-8 -*-
"""上下文管理模块 —— 桌宠角色

组装顺序与时间标注的全仓约定见 README「架构」约定 3/4（本模块为实现处，
docstring 只记模块内机制，不重复约定全文）。

- 维护会话历史（内存，由 persist 落盘）：未合并消息 + 合并条目 + 归档
- 注入开场白台词作为上下文文本流的起始（assistant 消息）
- 每条消息带 time（ISO 时间戳）；组装 messages 时：
  - 当前时间锚点放在消息序列后部、激活指令之前（动态内容置于尾部，
    避免在开头切断缓存前缀，同时贴近生成位置）；
  - 历史消息以**绝对时间**半角方括号元信息前缀标注（[08-14 23:50]，
    由消息的绝对时间确定、逐字节稳定，不随请求时刻漂移——利于缓存
    前缀连续命中；模型结合末尾时间锚点推算距离），避免全角括号样式
    被模型模仿进台词；
  - 历史超过 keep_recent 条时，把最旧部分（含旧合并条目）交给合并函数
    压缩为一条合并条目（ensure_merged），旧内容归档，可经 query_archive
    查询；上下文只保留一条最新合并内容 + 未合并消息。
- 发往 LLM 的 messages 剥离 time 字段，模型只看到绝对时间标注。
"""

from datetime import datetime

from session import USER_REFERENCE
from textutil import strip_paren_annotations

_WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]


def current_time_text(now: datetime = None) -> str:
    """语义化当前时间：此刻是 2026年8月12日（周三）22:15。

    now 可传入固定时刻：多轮工具循环中时间锚点应在循环触发时冻结一次
    （循环内每轮复用同一时刻，前缀逐字节稳定，利于缓存命中）。
    """
    now = now or datetime.now()
    return (f"此刻是 {now.year}年{now.month}月{now.day}日"
            f"（周{_WEEKDAYS[now.weekday()]}）{now.hour:02d}:{now.minute:02d}")


def rel_time(iso, now=None) -> str:
    """绝对时间 → 微博风格相对时间；无法解析返回空串。"""
    now = now or datetime.now()
    try:
        dt = datetime.fromisoformat(str(iso or ""))
    except (ValueError, TypeError):
        return ""
    secs = (now - dt).total_seconds()
    if secs < 60:
        return "刚刚"
    mins = int(secs // 60)
    if mins < 60:
        return f"{mins}分钟前"
    hours = int(mins // 60)
    if hours < 24:
        return f"{hours}小时前"
    days = int(hours // 24)
    if days < 30:
        return f"{days}天前"
    return f"{days // 30}个月前"


def abs_time_label(iso) -> str:
    """消息绝对时间 → 半角方括号绝对时间标注 [MM-DD HH:MM]；无法解析返回空串。

    绝对时间由消息自身的 time 字段确定，**逐字节稳定**（不随请求时刻漂移），
    用作 user 历史消息前缀——历史段因此成为稳定的缓存前缀；模型可结合
    消息序列末尾的【当前时间】锚点推算每条消息距今多久。年份省略（锚点
    提供年份上下文；跨年边界的歧义在桌宠会话时长下可接受）。
    """
    try:
        dt = datetime.fromisoformat(str(iso or ""))
    except (ValueError, TypeError):
        return ""
    return f"[{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}]"


def _merge_time_span(merge: dict) -> str:
    """合并摘要的时间窗：由被合并消息的绝对时间（time_min/time_max）格式化为
    绝对时间窗（如「这段对话发生于 08-14 21:00 至 08-15 00:30」）。

    绝对格式由存储的绝对时间确定、逐字节稳定（不随请求时刻漂移），
    与绝对时间标签同一套约定，作为缓存前缀的稳定段；旧存档无
    time_min/time_max 时返回空串（不编造时间）。
    """
    tmin, tmax = merge.get("time_min"), merge.get("time_max")
    if not tmin or not tmax:
        return ""

    def _fmt(iso):
        try:
            dt = datetime.fromisoformat(str(iso))
        except (ValueError, TypeError):
            return None
        return f"{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}"

    r1, r2 = _fmt(tmin), _fmt(tmax)
    if not r1 or not r2:
        return ""
    return f"这段对话发生于 {r1} 至 {r2}。"


class ContextManager:
    def __init__(self, rounds: int = 30, opening: list = None,
                 keep_recent: int = 10, keep_mid: int = 20):
        """rounds：上下文保留轮数（1 轮 = 1 条 user + 1 条 assistant）。
        opening：开场白台词列表，作为历史起始的 assistant 消息。
        keep_recent：最新保留的未合并消息条数（合并后也只保留这 N 条原文）。
        keep_mid：较新缓冲条数；合并触发阈值为 keep_recent + keep_mid——
        未合并原文最多到 30 条（10 最新 + 至多 20 较新）才触发合并，
        合并后回落为 keep_recent 条原文 + 1 条已合并（默认 10 + 1）。"""
        self.rounds = max(1, int(rounds))
        self.keep_recent = max(1, int(keep_recent))
        self.keep_mid = max(0, int(keep_mid))
        self.merge_threshold = self.keep_recent + self.keep_mid
        self.history = []          # 未合并消息（含 time）
        self.merge = None          # 最新合并条目 {summary, merged_at} 或 None
        self.archives = []         # 归档 [{archived_at, summary, messages}]
        self._merge_failed = False
        if opening:
            for line in opening:
                self.add_assistant(line)

    def add_user(self, text: str) -> None:
        self._merge_failed = False   # 新输入到来：允许重试合并
        self.history.append(self._msg("user", text))

    def add_assistant(self, text: str) -> None:
        self.history.append(self._msg("assistant", text))

    @staticmethod
    def _msg(role: str, text: str) -> dict:
        """带时间标注的消息（time 供持久化；build_messages 转绝对时间标注）。"""
        return {"role": role, "content": text,
                "time": datetime.now().isoformat(timespec="seconds")}

    @property
    def trimmed(self) -> list:
        """滑动窗口：只保留最近 rounds 轮（×2 条消息）。"""
        return self.history[-self.rounds * 2:]

    def ensure_merged(self, merge_fn) -> bool:
        """未合并历史超过阈值（keep_recent + keep_mid）时触发合并。

        合并除最新 keep_recent 条之外的全部（含旧合并条目）：旧合并条目 +
        被合并消息归档，history 回落为最近 keep_recent 条原文（默认 10），
        另保留 1 条已合并。合并周期 ≈ keep_mid 条（默认 20 条/次）。
        merge_fn(msgs, old_merge) -> str：调用方注入的 LLM 合并函数；
        失败（返回空/异常）保持现状并标记本回合不再重试，下次输入再试。
        """
        if len(self.history) <= self.merge_threshold or self._merge_failed:
            return False
        split = len(self.history) - self.keep_recent
        to_merge, keep = self.history[:split], self.history[-self.keep_recent:]
        try:
            summary = merge_fn(to_merge, self.merge)
        except Exception:
            summary = None
        if not summary or not summary.strip():
            self._merge_failed = True
            return False
        self.archives.append({
            "archived_at": datetime.now().isoformat(timespec="seconds"),
            "summary": (self.merge or {}).get("summary"),   # 更早一段的旧合并摘要
            "merged_summary": summary.strip(),               # 本条归档时段的合并摘要
            "messages": to_merge,                            # 被合并的原始消息
        })
        times = [m.get("time") for m in to_merge if m.get("time")]
        self.merge = {
            "summary": summary.strip(),
            "merged_at": datetime.now().isoformat(timespec="seconds"),
            # 被合并消息的绝对时间窗：注入时由程序换算相对当前时刻的时间窗
            #（LLM 摘要不再标注时间，避免旧相对时间随注入漂移误导时间线）
            "time_min": min(times) if times else None,
            "time_max": max(times) if times else None,
        }
        self.history = keep
        return True

    def query_archive(self, query: str = None, limit: int = 5,
                      detail: bool = False) -> str:
        """按关键词查询归档记录（供 query_archive 工具），返回语义化文本。

        query 支持空格拆成多个短词：整串命中优先，否则按命中词数排序
        （任一命中即召回，命中词越多越靠前）；省略则按时间返回最近归档。
        默认只回各归档摘要（防超长回传），detail=True 才附原始消息全文。
        limit 容错并夹在 1~50（防 LLM 传超大值导致超长回传）。
        """
        try:
            limit_n = max(1, min(50, int(limit or 5)))
        except (TypeError, ValueError):
            limit_n = 5
        detail = str(detail).lower() in ("true", "1", "yes", "on")
        q = (query or "").strip()
        tokens = q.split()
        n_total = len(self.archives)
        scored = []
        for idx, rec in enumerate(reversed(self.archives)):
            merged = rec.get("merged_summary") or ""
            old = rec.get("summary") or ""
            body = "\n".join(m.get("content", "") for m in rec.get("messages") or []
                             if m.get("content"))
            hay = "\n".join(x for x in (merged, old, body) if x)
            if not q:
                score = n_total - idx              # 无关键词：按时间倒序全量候选
            elif q in hay:
                score = 10000 + n_total - idx      # 整串命中优先
            else:
                hits = sum(1 for t in tokens if t in hay)
                if not hits:
                    continue
                score = hits * 100 + n_total - idx  # 命中词数优先，新归档靠前
            scored.append((score, rec))
        if not scored:
            return "归档中未找到相关记录。" if q else "归档为空。"
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [rec for _, rec in scored[:limit_n]]
        head = f"命中 {len(scored)}/{n_total} 条归档" + \
            (f"，展示前 {len(results)} 条。" if len(scored) > len(results) else "。")
        out = [head]
        for rec in results:
            when = rel_time(rec.get("archived_at"))
            out.append(f"【归档于 {when}】")
            shown = rec.get("merged_summary") or rec.get("summary")
            if shown:
                out.append("摘要：\n" + shown)
            if detail:
                lines = [f"- [{rel_time(m.get('time'))}] {m.get('content', '')}"
                         for m in rec.get("messages") or [] if m.get("content")]
                if lines:
                    out.append("原始消息：\n" + "\n".join(lines))
        return "\n\n".join(out)

    def build_messages(self, system_prompt: str, activation: str = None,
                       first_user_instr: str = None, pre_time: list = None,
                       mid_static: list = None, now: datetime = None) -> list:
        """组装发往 LLM 的完整消息。

        system + [mid_static 半动态段] + 合并条目（若有）+ 未合并历史（绝对
        时间标注）+ [pre_time 动态尾部段] + 当前时间（时间锚点）+ 指令附加。
        所有 time 字段剥离，模型只见绝对时间标注。

        mid_static：半动态段（变化频率远低于回合级的静态段补充，如好感等级
        规则/技能 world book——按设计预期一日不跨级）。按给定顺序插在静态
        system 之后、中期记忆之前：日常请求它逐字节稳定、参与缓存前缀；
        跨级（低频）时断点只落在本段，身份块与历史完全不受影响。
        元素为字符串（自动包成 system 消息）或完整消息 dict。

        now：当前时间锚点所用时刻。None = 调用时刻（datetime.now()）；
        多轮工具循环应传入循环触发时刻，使整段前缀在循环内逐字节稳定。
        当前时间锚点放在激活指令之前而非上下文开头：锚点内容随请求动态
        变化（「此刻是 …」分钟粒度），置于开头会在第二个消息处切断缓存
        前缀（静态在前、动态在后）；放后部既保住前缀，又贴近生成位置
        （近因效应），「以上对话消息」的说明放在被标注消息之后语义更顺。
        历史消息用绝对时间标注（abs_time_label，逐字节稳定）而非相对时间
        （相对标注会随请求时刻漂移、切断历史段缓存前缀）。

        pre_time：高频变化的动态段（如【{USER_REFERENCE}正在…】/【本回合可用工具】/
        【当前状态】/【本回合推进】，见 llm.env_section / tool_list_section /
        state_section / turn_section），按给定顺序插在时间锚点之前——全部
        落在历史之后，使静态 system 段、mid_static 段与历史段成为稳定缓存
        前缀。元素为字符串（自动包成 system 消息）或完整消息 dict。

        first_user_instr：思维链风格指令，追加到第一条 user 历史消息末尾
        （紧贴用户首句，靠近模型生成位置）；若历史中还没有 user 消息
        （纯开场白回合），则作为独立【思维链风格】system 消息注入。
        """
        msgs = [{"role": "system", "content": system_prompt}]
        # mid_static 半动态段：静态之后、中期记忆之前（变化频率：等级规则
        # 跨级才变 / 技能 world book 激活才变，均低于合并条目）。
        for seg in mid_static or []:
            if isinstance(seg, dict):
                msgs.append(seg)
            else:
                msgs.append({"role": "system", "content": str(seg)})
        # 合并条目：更早历史的压缩（中期记忆层）
        # 措辞双重要求：明确「这是你的中期记忆」，同时保留防御语义——
        # 它是对更早对话的压缩，不是{USER_REFERENCE}刚说的话，不要当作本回合发言来回应。
        # 时间窗由程序按被合并消息的绝对时间换算（_merge_time_span，绝对
        # 格式、逐字节稳定），不依赖 LLM 摘要中可能过时的相对时间。
        if self.merge and (self.merge.get("summary") or "").strip():
            span = _merge_time_span(self.merge)
            msgs.append({
                "role": "system",
                "content": "【中期记忆】以下是你对更早对话的压缩记忆，属于你的"
                           "中期记忆层（更早但不遥远的过往）；它是你记忆的浓缩，"
                           f"不是{USER_REFERENCE}刚说的话，也不要求你现在回应，仅供回忆参考。\n"
                           + (span + "\n" if span else "")
                           + self.merge["summary"].strip(),
            })
        # 未合并历史：绝对时间 → 半角方括号标注，只加在 user 消息上。
        # 标注由消息绝对时间确定、逐字节稳定（缓存友好）。assistant 消息
        # 保持纯台词：若带前缀，模型会把「assistant 消息 = 时间标注 + 台词」
        # 当成自己的发言格式模仿进输出（旧全角括号样式曾导致（刚刚）污染）。
        trimmed = self.trimmed
        first_user_idx = next((i for i, m in enumerate(trimmed)
                               if m.get("role") == "user"), None)
        for i, m in enumerate(trimmed):
            label = abs_time_label(m.get("time"))
            content = m.get("content", "")
            if label and m.get("role") == "user":
                content = f"{label} {content}"
            if first_user_instr and i == first_user_idx:
                content = f"{content}\n\n{first_user_instr}"
            if m.get("role") == "assistant":
                # 临时措施：历史旧风格台词带括号旁白（（歪头凑近）等），
                # 展示层统一清洗为纯台词，避免模型把「括号旁白 + 台词」当成
                # 自己的发言格式模仿进输出；存储与归档保留原文可回溯。
                content = strip_paren_annotations(content)
            msgs.append({"role": m.get("role", "user"), "content": content})
        # 思维链风格指令：历史中没有 user 消息（纯开场白回合）时，作为
        # 独立【思维链风格】system 消息注入。
        if first_user_instr and first_user_idx is None:
            msgs.append({
                "role": "system",
                "content": "【思维链风格】\n" + first_user_instr,
            })
        # 动态尾部段（pre_time）：状态/环境/推进等高频变化内容，插在时间
        # 锚点之前——全部位于历史之后，system 静态段与历史段不受其影响，
        # 可连续命中缓存前缀（静态在前、动态在后）。
        for seg in pre_time or []:
            if isinstance(seg, dict):
                msgs.append(seg)
            else:
                msgs.append({"role": "system", "content": str(seg)})
        # 当前时间（时间锚点）：放在消息序列后部、激活指令之前。
        # 内容随请求动态变化（「此刻是 …」逐分钟变），放开头会在头部切断
        # 缓存前缀（静态在前、动态在后）；放后部不牺牲前缀，又贴近生成
        # 位置（近因效应），说明文字用「以上」指向已出现的标注消息。
        msgs.append({
            "role": "system",
            "content": f"【当前时间】{current_time_text(now)}。"
                       "以上对话消息的标注为绝对时间（如「08-14 23:50」），"
                       "可与当前时间比对推算距今多久。",
        })
        if activation:
            msgs.append({
                "role": "system",
                "content": "【本回合指令】\n" + activation,
            })
        return msgs

    def clear(self) -> None:
        self.history = []
        self.merge = None
        self.archives = []
        self._merge_failed = False


# ---------------------------------------------------------------------------
# 离线自测
# ---------------------------------------------------------------------------

def selftest() -> None:
    from datetime import timedelta
    now = datetime(2026, 8, 12, 22, 0, 0)

    # 相对时间转换（微博风格，供归档展示/合并输入等辅助通道使用）
    assert rel_time((now - timedelta(seconds=30)).isoformat(), now) == "刚刚"
    assert rel_time((now - timedelta(minutes=5)).isoformat(), now) == "5分钟前"
    assert rel_time((now - timedelta(hours=3)).isoformat(), now) == "3小时前"
    assert rel_time((now - timedelta(days=2)).isoformat(), now) == "2天前"
    assert rel_time((now - timedelta(days=45)).isoformat(), now) == "1个月前"
    assert rel_time("bad", now) == ""
    # 绝对时间标注（主通道）：由消息绝对时间确定，逐字节稳定（缓存友好）
    assert abs_time_label("2026-08-14T23:50:00") == "[08-14 23:50]"
    assert abs_time_label("2026-01-02T03:04:05") == "[01-02 03:04]"
    assert abs_time_label("bad") == "" and abs_time_label(None) == ""

    ctx = ContextManager(rounds=2, opening=["开场A", "开场B", "开场C"])
    assert [m["content"] for m in ctx.history] == ["开场A", "开场B", "开场C"], ctx.history
    assert all(m["role"] == "assistant" and m.get("time") for m in ctx.history), ctx.history
    ctx.add_user("你好")
    ctx.add_assistant("哼哼～")
    ctx.add_user("再来")
    ctx.add_assistant("好的～")
    assert len(ctx.trimmed) == 4, ctx.trimmed
    assert ctx.trimmed[0]["content"] == "你好", ctx.trimmed
    assert all(t["content"] not in ("开场A", "开场B", "开场C") for t in ctx.trimmed)
    msgs = ctx.build_messages("SYS")
    assert msgs[0] == {"role": "system", "content": "SYS"}
    # 结构：SYS + 4 条历史 + 当前时间（动态锚点移至后部）= 6
    assert len(msgs) == 6, [m["role"] for m in msgs]
    assert "【当前时间】" in msgs[5]["content"], msgs[5]
    # 时间前缀只加在 user 消息上：assistant 历史保持纯台词，避免模型
    # 把「assistant 消息 = 时间标注 + 台词」当成自己的发言格式模仿进输出。
    # trimmed 4 条 = 你好(user) / 哼哼(assistant) / 再来(user) / 好的(assistant)；
    # 当前时间锚点在消息序列末尾（缓存：动态内容在后，不切头部前缀）。
    # user 前缀为绝对时间标注（由消息自身 time 确定，逐字节稳定）
    assert msgs[1]["role"] == "user" and msgs[1]["content"].startswith(
        abs_time_label(ctx.history[0]["time"]) + " "), msgs[1]
    assert msgs[2]["role"] == "assistant" and msgs[2]["content"] == "哼哼～", msgs[2]
    assert msgs[3]["role"] == "user" and msgs[3]["content"].startswith(
        abs_time_label(ctx.history[2]["time"]) + " "), msgs[3]
    assert msgs[4]["role"] == "assistant" and msgs[4]["content"] == "好的～", msgs[4]
    assert msgs[5]["role"] == "system" and "【当前时间】" in msgs[5]["content"], msgs[5]
    assert "以上对话消息" in msgs[5]["content"], "锚点位于历史之后，说明应用「以上」"
    assert "绝对时间" in msgs[5]["content"], "锚点应说明绝对时间标注约定"
    assert all("time" not in m for m in msgs), "messages 不应携带 time"
    # 历史括号清洗（临时措施）：assistant 历史带括号旁白 → 展示层清洗为
    # 纯台词（存储不变）；user 消息的括号（元信息如「（现在没人在说话）」）不清洗
    ctx_p = ContextManager(rounds=2)
    ctx_p.add_user("在吗")
    ctx_p.add_assistant("（歪头凑近）在的呀～（眨眼）")
    ctx_p.add_user("（现在没有人在跟你说话）")
    ctx_p.add_assistant("哼哼～杂鱼❤️")
    msgs_p = ctx_p.build_messages("SYS")
    asst_hist = [m["content"] for m in msgs_p if m["role"] == "assistant"]
    assert asst_hist == ["在的呀～", "哼哼～杂鱼❤️"], asst_hist
    user_hist = [m["content"] for m in msgs_p if m["role"] == "user"]
    assert any("（现在没有人在跟你说话）" in u for u in user_hist), user_hist
    # 存储层保留原文（可回溯、可归档），清洗只发生在展示层
    assert ctx_p.history[1]["content"] == "（歪头凑近）在的呀～（眨眼）", ctx_p.history[1]
    # 指令附加：激活指令为最后一条 system 消息（贴近生成位置）
    msgs2 = ctx.build_messages("SYS", activation="ACT_NOW")
    assert msgs2[-1]["role"] == "system" and msgs2[-1]["content"].endswith("ACT_NOW"), msgs2
    assert msgs2[-1]["content"].startswith("【本回合指令】"), msgs2[-1]
    assert len(msgs2) == len(msgs) + 1, msgs2
    # first_user_instr：追加到第一条 user 历史消息末尾（紧贴用户首句）
    msgs3i = ctx.build_messages("SYS", first_user_instr="【思维模式要求】推理式")
    assert len(msgs3i) == len(msgs), "first_user_instr 不应改变消息条数"
    fu = msgs3i[1]  # 第一条 user 历史（你好）
    assert fu["role"] == "user" and fu["content"].endswith("【思维模式要求】推理式"), fu
    assert fu["content"].startswith("["), "首句仍应带绝对时间前缀"
    # 后续 user 消息不重复注入；assistant 历史保持纯台词
    assert "【思维模式要求】推理式" not in msgs3i[3]["content"], msgs3i[3]
    assert msgs3i[2]["content"] == "哼哼～", msgs3i[2]
    assert msgs3i[4]["content"] == "好的～", msgs3i[4]
    assert "【当前时间】" in msgs3i[5]["content"], "时间锚点仍在序列末尾"
    # pre_time 动态尾部段：插在时间锚点之前（状态/环境/推进等高频变化内容）
    msgs_pt = ctx.build_messages("SYS", pre_time=["段A", "段B"])
    assert len(msgs_pt) == len(msgs) + 2, msgs_pt
    assert msgs_pt[-3]["role"] == "system" and msgs_pt[-3]["content"] == "段A", msgs_pt[-3]
    assert msgs_pt[-2]["role"] == "system" and msgs_pt[-2]["content"] == "段B", msgs_pt[-2]
    assert "【当前时间】" in msgs_pt[-1]["content"], "pre_time 应在时间锚点之前"
    # pre_time + 激活指令：激活仍为最后一条（时间锚点紧跟 pre_time）
    msgs_pt3 = ctx.build_messages("SYS", activation="ACT", pre_time=["段A"])
    assert msgs_pt3[-3]["content"] == "段A", msgs_pt3[-3]
    assert "【当前时间】" in msgs_pt3[-2]["content"], msgs_pt3[-2]
    assert msgs_pt3[-1]["role"] == "system" and "【本回合指令】" in msgs_pt3[-1]["content"], \
        "激活指令仍为最后一条"
    # pre_time 支持 None（不改变结构）
    msgs_pt2 = ctx.build_messages("SYS", pre_time=None)
    assert len(msgs_pt2) == len(msgs), "pre_time=None 不应改变结构"
    # 纯开场白（无 user 历史）：风格指令作为独立【思维链风格】system 消息
    ctx_open = ContextManager(rounds=2, opening=["开场A", "开场B"])
    msgs_open = ctx_open.build_messages("SYS", first_user_instr="【角色沉浸要求】沉浸式")
    style_msgs = [m for m in msgs_open if "【思维链风格】" in m["content"]]
    assert len(style_msgs) == 1 and style_msgs[0]["role"] == "system", msgs_open
    assert style_msgs[0]["content"].startswith("【思维链风格】"), style_msgs[0]

    # mid_static 半动态段：静态 system 之后、中期记忆之前
    msgs_mid = ctx.build_messages("SYS", mid_static=["段M1", "段M2"])
    assert msgs_mid[0]["role"] == "system" and msgs_mid[0]["content"] == "SYS", msgs_mid
    assert msgs_mid[1]["content"] == "段M1" and msgs_mid[2]["content"] == "段M2", msgs_mid
    assert msgs_mid[1]["role"] == "system" and msgs_mid[2]["role"] == "system", msgs_mid
    assert msgs_mid[3]["role"] == "user", "mid_static 之后才是历史"
    assert msgs_mid[-1]["role"] == "system" and "【当前时间】" in msgs_mid[-1]["content"], \
        "时间锚点仍在序列末尾（mid_static 不改变尾部布局）"
    # now 冻结：传入固定时刻，时间锚点逐字节稳定（多轮工具循环复用同一时刻）
    from datetime import datetime as _dt
    fixed = _dt(2026, 8, 12, 22, 15, 0)
    msgs_t1 = ctx.build_messages("SYS", now=fixed)
    msgs_t2 = ctx.build_messages("SYS", now=fixed)
    assert msgs_t1[-1]["content"] == msgs_t2[-1]["content"], "固定 now 时间锚点应逐字节稳定"
    assert "此刻是 2026年8月12日（周三）22:15" in msgs_t1[-1]["content"], msgs_t1[-1]
    assert current_time_text(fixed) == "此刻是 2026年8月12日（周三）22:15", current_time_text(fixed)
    assert current_time_text(None), "now=None 应回退当前时刻"

    # 合并流程：超过阈值（keep_recent + keep_mid）才触发，合并后只留
    # keep_recent 条原文 + 1 条已合并（默认阈值 20 + 40 = 60，回落 20）
    ctx2 = ContextManager(rounds=30, keep_recent=2, keep_mid=2)  # 阈值 4
    for i in range(10):
        ctx2.add_user(f"Q{i}")
        ctx2.add_assistant(f"A{i}")
    assert len(ctx2.history) == 20
    assert ctx2.merge_threshold == 4, ctx2.merge_threshold
    assert ctx2.ensure_merged(lambda msgs_, old: "【合并】这段对话发生于 3 小时前。")
    assert len(ctx2.history) == 2, "合并后只留最近 keep_recent 条"
    assert ctx2.merge and "【合并】" in ctx2.merge["summary"], ctx2.merge
    assert len(ctx2.archives) == 1 and len(ctx2.archives[0]["messages"]) == 18
    # 再超长 → 第二次合并，旧合并条目被归档
    for i in range(6):
        ctx2.add_user(f"P{i}")
        ctx2.add_assistant(f"B{i}")
    assert ctx2.ensure_merged(lambda msgs_, old: "新合并摘要")
    assert len(ctx2.history) == 2 and ctx2.merge["summary"] == "新合并摘要"
    assert len(ctx2.archives) == 2
    assert ctx2.archives[-1]["summary"] == "【合并】这段对话发生于 3 小时前。", "旧合并条目应归档"
    # 默认阈值：30 条以内不合并；超过后合并回落为 10 条
    ctx_def = ContextManager(rounds=30)
    for i in range(14):
        ctx_def.add_user(f"d{i}")
        ctx_def.add_assistant(f"e{i}")
    assert ctx_def.merge_threshold == 30, ctx_def.merge_threshold
    assert not ctx_def.ensure_merged(lambda msgs_, old: "不应触发"), "30 条以内不应合并"
    ctx_def.add_user("d30")
    ctx_def.add_assistant("e30")   # 28 + 2 = 30 条：仍不触发
    assert not ctx_def.ensure_merged(lambda msgs_, old: "仍不应触发"), "30 条（含）不应合并"
    ctx_def.add_user("d31")        # 31 条：触发，回落 10 条
    assert ctx_def.ensure_merged(lambda msgs_, old: "触发合并"), "超过 30 条应合并"
    assert len(ctx_def.history) == 10, f"默认合并后应回落为 10 条, 实际 {len(ctx_def.history)}"
    # 合并条目进入 build：system 角色、中期记忆标注（含防误用防御语义）
    # 布局：SYS + 合并条目 + 2 条历史 + 当前时间锚点（合并条目在 [1]）
    msgs3 = ctx2.build_messages("SYS")
    assert msgs3[1]["role"] == "system" and "中期记忆" in msgs3[1]["content"], msgs3[1]
    assert "新合并摘要" in msgs3[1]["content"]
    assert f"不是{USER_REFERENCE}刚说的话" in msgs3[1]["content"], "应保留防误用语义"
    assert "更早的历史合并" not in msgs3[1]["content"], "旧标注应替换"
    assert "非对话内容" not in msgs3[1]["content"], "旧标注应替换"
    assert msgs3[-1]["role"] == "system" and "【当前时间】" in msgs3[-1]["content"], \
        "时间锚点应在序列末尾（激活指令之前）"
    # mid_static 在合并条目之前：布局 SYS + mid_static + 合并条目 + 历史
    msgs_mid2 = ctx2.build_messages("SYS", mid_static=["段M"])
    assert "段M" in msgs_mid2[1]["content"] and "中期记忆" in msgs_mid2[2]["content"], \
        "合并条目应在 mid_static 之后（半动态段比合并条目更稳定）"

    # 合并失败：不合并、本回合不重试、新输入后重试
    ctx3 = ContextManager(rounds=30, keep_recent=1, keep_mid=1)  # 阈值 2
    for i in range(3):
        ctx3.add_user(f"q{i}")
        ctx3.add_assistant(f"a{i}")
    assert not ctx3.ensure_merged(lambda msgs_, old: None)
    assert len(ctx3.history) == 6 and ctx3.merge is None, "失败不应合并"
    assert not ctx3.ensure_merged(lambda msgs_, old: "x"), "失败后本回合不应重试"
    ctx3.add_user("新输入")
    assert ctx3.ensure_merged(lambda msgs_, old: "x"), "新输入后应重试合并"

    # 归档查询：默认只回摘要；detail=True 附原文；分词匹配；命中统计
    assert ctx2.archives[-1]["merged_summary"] == "新合并摘要", "归档应存本条时段摘要"
    r = ctx2.query_archive(query="Q0")
    assert "摘要" in r and "原始消息" not in r, "默认应只回摘要"
    assert "原始消息" in ctx2.query_archive(query="Q0", detail=True), \
        "detail=True 应附原文"
    assert ctx2.query_archive(query="不存在的关键词") == "归档中未找到相关记录。"
    r2 = ctx2.query_archive(query="合并")
    assert "摘要" in r2 and "命中 2/2 条归档" in r2, \
        "关键词应命中两段摘要并给出命中统计"
    # 多词查询：整串未命中时按词召回（Q0 命中，其余词不命中）
    r3 = ctx2.query_archive(query="Q0 不存在的词")
    assert "摘要" in r3 and "未找到" not in r3, "多词查询应任一命中即可"
    assert "命中 1/2 条归档" in r3, r3

    # 清空
    ctx2.clear()
    assert ctx2.history == [] and ctx2.merge is None and ctx2.archives == []
    print("[context.selftest] 通过 ✓ 绝对时间标注 / 当前时间锚点 / 合并与归档 / 归档查询 / messages 组装")


if __name__ == "__main__":
    selftest()
