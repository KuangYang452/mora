# -*- coding: utf-8 -*-
"""纯文本工具 —— textutil

与 LLM/配置/角色均无依赖的文本处理函数，供 llm.py / context.py 共用。
从 llm.py 拆出，断开「context → llm」的顶层依赖（llm 对 context 的函数级
延迟导入原本是潜在循环依赖，见 docs/1.0_RELEASE_PLAN.md §5.3）。

职责：
- strip_time_prefix：剥离开头连续的时间标注前缀（模型偶会把上下文里的
  时间标注当成自己的发言格式复制进输出，硬清洗兜底）；
- strip_paren_annotations：剥离台词中的括号旁白/动作描写（协议要求旁白
  进 update_state 字段，气泡与历史不应出现括号旁白）。
"""

import re

_TIME_PREFIX_RE = re.compile(
    r"^(?:[\[（]\s*(?:刚刚|[0-9]+分钟前|[0-9]+小时前|[0-9]+天前|[0-9]+个月前|"
    r"[0-9]{2}-[0-9]{2}[ ]+[0-9]{2}:[0-9]{2})"
    r"\s*[\]）]\s*)+")

_PAREN_ANNOT_RE = re.compile(r"[（(][^（()）]*[)）]")


def strip_time_prefix(text: str) -> str:
    """剥离开头连续的时间标注前缀（[刚刚] / （2分钟前） / [08-14 23:50] 等），无则原样返回。

    模型偶会把上下文消息里的时间标注（含历史遗留的全角括号样式）当成
    自己的发言格式复制进输出；这里是硬清洗兜底，不依赖指令。
    匹配「刚刚 / N分钟前 / N小时前 / N天前 / N个月前 / MM-DD HH:MM（绝对
    时间，上下文主通道标注）」的括号标注，不触碰台词内容（含（歪头凑近）
    这类动作描写）。
    """
    if not text:
        return text
    return _TIME_PREFIX_RE.sub("", text).strip()


def strip_paren_annotations(text: str) -> str:
    """剥离台词中的括号旁白/动作描写（（…）/ (…)），保留其余内容。

    协议要求括号旁白放进 update_state 的 inner_thought / emote 字段，
    台词本身不该含括号内容；这里对模型输出（say.text / content 降级 /
    历史旧风格台词）做硬清洗兜底，避免气泡与历史出现括号旁白。
    只匹配完整配对的圆括号（最多剥 3 层嵌套），不触碰方括号时间标注
    （由 strip_time_prefix 处理）；清洗后压缩多余空白，不碰换行。
    """
    if not text:
        return text
    for _ in range(3):
        new = _PAREN_ANNOT_RE.sub("", text)
        if new == text:
            break
        text = new
    return re.sub(r"[ \t]+", " ", text).strip()


def selftest() -> None:
    """离线自测：时间前缀与括号旁白清洗（原 llm.py selftest 对应段）。"""
    assert strip_time_prefix("[刚刚]（「哟～杂鱼」）") == "（「哟～杂鱼」）"
    assert strip_time_prefix("（刚刚）（刚刚）（2分钟前）台词") == "台词"
    assert strip_time_prefix("[5分钟前] 对方看向了角色") == "对方看向了角色"
    assert strip_time_prefix("[08-14 23:50] 莫拉，帮我看看这段剧情") == "莫拉，帮我看看这段剧情"
    assert strip_time_prefix("[08-14 23:50] [08-14 23:55] 台词") == "台词"
    assert strip_time_prefix("哼哼～杂鱼❤️") == "哼哼～杂鱼❤️"
    assert strip_time_prefix("（歪头凑近）看什么看") == "（歪头凑近）看什么看"
    assert strip_time_prefix("") == ""
    # 无括号原样返回；嵌套最多剥 3 层（strip_time_prefix 不碰括号，由本函数负责）
    assert strip_paren_annotations("（歪头凑近）看什么看") == "看什么看"
    assert strip_paren_annotations("哎呀呀～（得意）") == "哎呀呀～"
    assert strip_paren_annotations("（转圈）（眨眼）哼哼～") == "哼哼～"
    assert strip_paren_annotations("(轻笑) 杂鱼就是杂鱼") == "杂鱼就是杂鱼"
    assert strip_paren_annotations("（（小声）说什么）") == ""
    assert strip_paren_annotations("哼哼～杂鱼❤️") == "哼哼～杂鱼❤️"
    assert strip_paren_annotations("吾辈  （停顿）  才不怕") == "吾辈 才不怕"
    assert strip_paren_annotations("台词\n换行（动作）保持\n") == "台词\n换行保持"
    assert strip_paren_annotations("") == ""
    print("  textutil: 时间前缀 / 括号旁白清洗 ✓")


if __name__ == "__main__":
    selftest()
