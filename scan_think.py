# -*- coding: utf-8 -*-
"""扫描回合日志：检测「查询了没留存」——查询结果被下一次查询覆盖前，
未用 think 写入结论。

判定：turn i 调用了查询工具且未调 think，且 turn i+1 又调用查询工具
（非 say 收尾）且未调 think → turn i 的结果在留存前被覆盖 = 真实丢失。
查询后下一轮直接 say 的属正常消费（tool 结果在下一轮请求中可见）。
"""
import json
import re
from pathlib import Path

QUERY_TOOLS = {"discover_running", "read_current_text", "query_wiki", "scan_game",
               "read_raw_text", "wiki_arbitrate", "wiki_rebuild", "query_archive"}

LOGS = sorted(Path("log").glob("llm_round_*.txt"), key=lambda p: p.name)


def parse_log(path):
    text = path.read_text(encoding="utf-8")
    m = re.search(r"【交付给 LLM 的消息】\n(.*?)\n----------------------------------------\n【LLM 原始响应】", text, re.S)
    if not m:
        return None
    body = m.group(1)
    has_prev = "（tool_calls:" in body
    rm = re.search(r"【LLM 原始响应】\n(\{.*)", text, re.S)
    cur_calls = []
    if rm:
        try:
            resp = json.loads(rm.group(1))
            for tc in (resp.get("choices") or [{}])[0].get("message", {}).get("tool_calls") or []:
                fn = tc.get("function") or {}
                cur_calls.append(fn.get("name", ""))
        except Exception:
            pass
    return {"has_prev": has_prev, "cur": cur_calls}


inputs, cur = [], []
for p in LOGS:
    d = parse_log(p)
    if d is None:
        continue
    if not d["has_prev"] and cur:
        inputs.append(cur)
        cur = []
    cur.append((p.name, d["cur"]))
if cur:
    inputs.append(cur)

lost, benign = [], []
for idx, turns in enumerate(inputs):
    for i in range(len(turns)):
        name, calls = turns[i]
        queries = [c for c in calls if c in QUERY_TOOLS]
        think = "think" in calls
        if not queries:
            continue
        if think:
            continue                       # 同轮已 think，留存
        nxt = turns[i + 1][1] if i + 1 < len(turns) else []
        if nxt and not any(c == "say" for c in nxt) and any(c in QUERY_TOOLS for c in nxt) \
                and "think" not in nxt:
            lost.append((idx, name, calls, nxt))       # 覆盖前未留存
        else:
            benign.append((idx, name, calls, nxt))     # 结果被下一轮消费或已收尾

print(f"共 {len(inputs)} 个输入\n")
print(f"==== 真实丢失（{len(lost)} 处）：查询后未 think，又被后续查询覆盖 ====")
for idx, name, calls, nxt in lost:
    print(f"  输入{idx} {name.split('_')[3][:10]} 调={calls} → 下轮调={nxt}")
print(f"\n==== 正常消费（{len(benign)} 处）：查询后下一轮 say/think 收尾 ====")
for idx, name, calls, nxt in benign:
    print(f"  输入{idx} {name.split('_')[3][:10]} 调={calls} → 下轮调={nxt}")
