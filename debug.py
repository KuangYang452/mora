# -*- coding: utf-8 -*-
"""调试工具 —— 桌宠角色

用途：确认「最终交付给 LLM 的提示词」以及查看运行时回合日志。

用法：
  python debug.py             # 打印组装好的完整 system prompt（逐段 + 统计）
  python debug.py --llm       # 打印当前生效的 LLM 配置（密钥脱敏）
  python debug.py --state     # 打印当前硬编码状态（好感度/内心想法/等级映射）
  python debug.py --list      # 列出 log/ 下的日志文件
  python debug.py --last [N]  # 打印最近 N 条日志全文（默认 1，最大 10）
  python debug.py --clean     # 清空 log/ 目录

提示词组装逻辑与 pet.py 完全一致（复用 llm.build_system_prompt），
此命令离线运行，不需要 API Key。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data import AFFECTION_MAX_DELTA
from llm import build_system_prompt, get_character, load_llm_config
import logutil
import settings
import character as character_mod


def print_prompt() -> None:
    card = get_character()          # 当前激活角色卡（character/<slug>/card.json）
    prompt = build_system_prompt(card, state=character_mod.current().initial_state())
    print("=" * 60)
    print(f"最终交付给 LLM 的 system prompt（长度 {len(prompt)} 字符）")
    print("=" * 60)
    for seg in prompt.split("\n\n"):
        if not seg.strip():
            continue
        title = seg.split("\n", 1)[0]
        print(f"\n———— {title} ————")
        print(seg)
    print("\n" + "=" * 60)
    print(f"总计 {len(prompt)} 字符 | 人设来源: character/{character_mod.current().slug}/"
          f"（card.json + profile.ini） | 状态来源: 角色初始状态（profile [state]）")
    print("=" * 60)


def print_llm_config() -> None:
    cfg = load_llm_config()
    key = cfg.get("api_key") or ""
    masked = (key[:4] + "…" + str(len(key)) + " 字符") if key else "（未配置）"
    print("当前生效的 LLM 配置（唯一生效位置：setting/llm.ini）：")
    print(f"  base_url:   {cfg.get('base_url', '?')}")
    print(f"  model:      {cfg.get('model', '?')}")
    print(f"  temperature:{cfg.get('temperature', '?')}")
    print(f"  max_tokens: {cfg.get('max_tokens', '?')}")
    print(f"  reasoning:  {cfg.get('reasoning', '?')} | reasoning_effort: {cfg.get('reasoning_effort', '?')}")
    print(f"  api_key:    {masked}   （唯一生效位置：llm_config.json）")


def print_state() -> None:
    char = character_mod.current()
    st = char.initial_state()
    aff = int(st["affection"])
    print(f"当前角色: {char.slug}（{char.display_name}）｜初始状态（profile [state]）：")
    print(f"  好感度: {aff}/100 → 等级「{char.level_for(aff)}」")
    print(f"  内心想法: {st['inner_thought']}")
    print(f"  已激活技能: {st.get('skills', [])}（LLM 通过 skills 工具管理）")
    print("好感等级映射（profile [affection_levels]，LLM 无权直接指定）：")
    for lo, hi, name in char.affection_levels:
        print(f"  {lo:>3} ~ {hi:>3}  {name}")
    print(f"单回合好感度变化边界: ±{AFFECTION_MAX_DELTA}")
    print(f"日志开关: log_enabled = {settings.app_config().get('log_enabled', True)}")


def print_log_list() -> None:
    logs = logutil.list_logs()
    if not logs:
        print("log/ 目录暂无日志（运行 pet.py 对话后生成）。")
        return
    print(f"log/ 下共 {len(logs)} 条日志：")
    for i, p in enumerate(logs, 1):
        size = p.stat().st_size
        print(f"  {i:>2}. {p.name}  ({size} B)")


def print_last_logs(n: int) -> None:
    logs = logutil.list_logs()
    if not logs:
        print("log/ 目录暂无日志（运行 pet.py 对话后生成）。")
        return
    n = max(1, min(int(n), 10))
    for p in logs[-n:]:
        print(f"\n{'=' * 60}\n文件: {p.name}\n{'=' * 60}")
        print(logutil.read_log(p))


def clean_logs() -> None:
    logs = logutil.list_logs()
    for p in logs:
        p.unlink()
    print(f"已清空 log/（删除 {len(logs)} 条日志）。")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print_prompt()
        return
    cmd = args[0]
    if cmd == "--llm":
        print_llm_config()
    elif cmd == "--state":
        print_state()
    elif cmd == "--list":
        print_log_list()
    elif cmd == "--last":
        n = args[1] if len(args) > 1 else 1
        print_last_logs(n)
    elif cmd == "--clean":
        clean_logs()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
