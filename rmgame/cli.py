# -*- coding: utf-8 -*-
"""命令行入口 —— rmgame/cli（M0-M3）

用法：
  python -m rmgame.cli scan <root_dir> [--recursive] [--yes]
  python -m rmgame.cli scan --running [--yes]        # 发现运行中游戏并入库
  python -m rmgame.cli scan --approve <slug>         # 升级 trust auto → user（解锁启动）
  python -m rmgame.cli extract <slug> [--all]
  python -m rmgame.cli wiki <slug> [--model M] [--temperature T]
  python -m rmgame.cli query <slug> <query> [--no-build] [--force]
  python -m rmgame.cli start <slug> [--port N] [--dry-run]
  python -m rmgame.cli monitor <slug> [--interval N] [--rounds N] [--ocr-only]
  python -m rmgame.cli current <slug>
  python -m rmgame.cli ocr <slug>
  python -m rmgame.cli status
  python -m rmgame.cli selftest
"""

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from .discovery import (
    RAW_DIR, RUNTIME_DIR, GameInfo, discover, load_games, register,
)
from .extract import extract_game, write_raw
from .monitor import load_current, monitor_loop, port_for, start_game
from .ocr import OcrUnavailableError, ocr_game_text
from .rewriter import (
    build_maps_summary, concept_discovery, concept_rewrite, write_skeleton,
)
from .wiki import ensure_concept, query_concept


# ---------------------------------------------------------------------------
# 游戏解析辅助
# ---------------------------------------------------------------------------

def _find_game(games, key: str):
    """按 slug / name / aliases 精确匹配游戏（CLI 参数解析）。

    主名（slug / name）优先，aliases 兜底（防多游戏别名冲突误命中）。
    """
    g = next((x for x in games if x.slug == key or x.name == key), None)
    if g is None:
        g = next((x for x in games if key in getattr(x, "aliases", [])), None)
    return g


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_scan(args) -> int:
    """发现游戏 → 人工确认 → 入库（root 目录 / 运行中进程 / trust 升级）。"""
    if args.approve:
        from .discovery import approve
        g, msg = approve(args.approve)
        print(msg)
        return 0 if g is not None else 1
    if args.running:
        from .monitor import enumerate_running
        try:
            found, _ = enumerate_running()
        except Exception as exc:
            print(f"[错误] 枚举运行中的游戏失败: {exc}")
            return 1
        if not found:
            print("当前未检测到运行中的 RPG Maker 游戏（Game.exe + 引擎特征）。")
            return 1
        print(f"发现 {len(found)} 个运行中的 RPG Maker 游戏：")
        for g in found:
            print(f"  {g.slug:20} {g.name:28} [{g.engine}]  {g.dir}")
        if not args.yes:
            ans = input("确认全部入库（写入 runtime/games.json，trust=user 可启动）？[y/N] ")\
                .strip().lower()
            if ans not in ("y", "yes"):
                print("未入库。")
                return 1
        merged = register(found)
        print(f"已入库 {len(found)} 个，游戏库现有 {len(merged)} 个。")
        return 0
    if not args.root:
        print("[错误] 需要 root 目录参数（或使用 --running / --approve）。")
        return 1
    try:
        found = discover(args.root, recursive=args.recursive)
    except FileNotFoundError as exc:
        print(f"[错误] {exc}")
        return 1
    if not found:
        print(f"在 {args.root} 未发现 RPG Maker 游戏。")
        return 1
    print(f"发现 {len(found)} 个 RPG Maker 游戏：")
    for g in found:
        print(f"  {g.slug:20} {g.name:28} [{g.engine}]  {g.dir}")
    if not args.yes:
        ans = input("确认全部入库（写入 runtime/games.json）？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("未入库。")
            return 1
    merged = register(found)
    print(f"已入库 {len(found)} 个，游戏库现有 {len(merged)} 个。")
    return 0


def cmd_extract(args) -> int:
    """按 slug 提取文本 → raw/<slug>/。"""
    games = load_games()
    g = _find_game(games, args.slug)
    if g is None:
        print(f"[错误] 游戏库中无「{args.slug}」（先运行 scan）。")
        return 1
    try:
        result = extract_game(g, all_text=args.all)
    except FileNotFoundError as exc:
        print(f"[错误] {exc}")
        return 1
    path = write_raw(result)
    print(f"已提取 → {path}")
    print(f"  地图 {result.map_count} 张 | 对话条目 {result.entry_count} 条"
          + (f" | 数据库文本 {len(result.database)} 条" if result.all_text else ""))
    return 0


def cmd_status(args) -> int:
    """游戏库与 raw 状态一览（含 trust 级别与运行中标记）。"""
    games = load_games()
    print(f"游戏库（{len(games)} 个）→ runtime/games.json")
    try:
        from .monitor import enumerate_running
        running_slugs = {g.slug for g, _ in enumerate_running()}
    except Exception:
        running_slugs = set()
    for g in games:
        meta = RAW_DIR / g.slug / "meta.json"
        info = "raw: 未提取"
        if meta.exists():
            try:
                m = json.loads(meta.read_text(encoding="utf-8-sig"))
                info = f"raw: {m.get('map_count', '?')} 地图 / {m.get('entry_count', '?')} 条目"
            except (json.JSONDecodeError, OSError):
                info = "raw: meta 损坏"
        mode = getattr(g, "launch_mode", "auto")
        trust = getattr(g, "trust", "user") or "user"
        mark = "●" if g.slug in running_slugs else " "
        print(f"  {mark} {g.slug:19} {g.name:26} [{g.engine}] trust:{trust:5} "
              f"启动:{mode:6} {info}")
    return 0


def cmd_wiki(args) -> int:
    """概念发现（阶段 1）→ 写 wiki/<slug>/ 骨架（概念全 pending）。"""
    games = load_games()
    g = _find_game(games, args.slug)
    if g is None:
        print(f"[错误] 游戏库中无「{args.slug}」（先运行 scan）。")
        return 1
    game_raw = RAW_DIR / g.slug
    if not (game_raw / "meta.json").exists():
        print(f"[错误] 尚未提取 raw（先运行 extract {g.slug}）。")
        return 1
    summary = build_maps_summary(game_raw)
    if not summary:
        print(f"[错误] raw 中无地图数据: {game_raw}/maps")
        return 1
    cfg = {}
    if args.model:
        cfg["model"] = args.model
    if args.temperature is not None:
        cfg["temperature"] = args.temperature
    print(f"概念发现中…（{len(summary)} 张地图摘要，分批喂给 LLM）")
    concepts = concept_discovery(g.slug, summary, llm_cfg=cfg or None)
    if not concepts:
        print("[错误] 概念发现无结果（LLM 未返回有效概念）。")
        return 1
    index = write_skeleton(g.slug, g.name, g.engine, concepts)
    print(f"已生成 wiki/<{g.slug}>/ 骨架：{len(index['concepts'])} 个概念（全部 pending）")
    for c in index["concepts"]:
        print(f"  - {c['id']:24} {c['title']:14} [{c['kind']}] {c['summary']}")
    return 0


def cmd_query(args) -> int:
    """按关键词定位概念条目；pending 时现场懒构建（M2）。"""
    cfg = {}
    if args.model:
        cfg["model"] = args.model
    if args.temperature is not None:
        cfg["temperature"] = args.temperature
    builder = None
    if not args.no_build:
        builder = lambda s, c: concept_rewrite(s, c, llm_cfg=cfg or None)
    if args.force:
        # 先定位拿 concept_id，再强制重建（覆盖 built 缓存）
        loc = query_concept(args.slug, args.query)
        res = ensure_concept(args.slug, loc["concept"]["id"], builder, force=True) \
            if loc["ok"] else loc
    else:
        res = query_concept(args.slug, args.query, builder=builder)
    if not res["ok"]:
        reason = {
            "no_wiki": "该游戏尚无 wiki（先运行 wiki 命令）",
            "no_concept": f"无匹配概念（query: {args.query}）",
            "build_failed": "概念构建失败（LLM 重试后仍失败，下次查询可重试）",
        }.get(res["reason"], res["reason"])
        print(f"[未命中] {reason}")
        return 1
    c = res["concept"]
    print(f"概念: {c['title']}（{c['kind']}） | 状态: {res['status']}")
    print(f"  摘要: {c.get('summary', '')}")
    print(f"  引用: {', '.join(c.get('refs', []))}")
    if res["content"]:
        print("----- 条目内容 -----")
        print(res["content"])
    elif args.no_build:
        print("  （条目未构建 —— 去掉 --no-build 可现场懒构建）")
    else:
        print("  （条目构建失败 —— 下次查询可重试）")
    return 0


def cmd_start(args) -> int:
    """以 CDP 调试端口启动游戏（注入 --remote-debugging-port）。"""
    games = load_games()
    g = _find_game(games, args.slug)
    if g is None:
        print(f"[错误] 游戏库中无「{args.slug}」（先运行 scan）。")
        return 1
    res = start_game(g, port=args.port, dry_run=args.dry_run)
    if not res["ok"]:
        print(f"[错误] {res.get('error', '启动失败')}")
        return 1
    if res["state"] == "running":
        print(f"游戏已在运行（端口 {res['port']} 可连 CDP）。")
    elif res["state"] == "would_start":
        print(f"[dry-run] 将执行: {' '.join(res['cmd'])}")
    else:
        print(f"已启动（pid={res['pid']}，CDP 端口 {res['port']}）。"
              "稍后可用 monitor 命令轮询。")
    return 0


def cmd_monitor(args) -> int:
    """轮询读取游戏状态 → runtime/current.json。"""
    games = load_games()
    g = _find_game(games, args.slug)
    if g is None:
        print(f"[错误] 游戏库中无「{args.slug}」（先运行 scan）。")
        return 1
    rounds = args.rounds if args.rounds is not None else 1 if args.once else None
    print(f"monitor {g.slug}（端口 {port_for(g.slug)}"
          + ("，OCR 模式" if args.ocr_only else "")
          + (f"，{rounds} 轮" if rounds else "，无限轮询") + "）")
    n = monitor_loop(g, port=args.port, interval=args.interval,
                     max_rounds=rounds, ocr_only=args.ocr_only)
    cur = load_current()
    if cur:
        print(f"已写 {n} 次快照 → runtime/current.json")
        print(f"  地图: {cur.get('map_name') or cur.get('map_id')} | "
              f"场景: {cur.get('scene')}")
        print(f"  文本: {(cur.get('text') or '')[:60]}")
        print(f"  更新: {cur.get('updated_at')}")
    return 0


def cmd_current(args) -> int:
    """显示 runtime/current.json 快照。"""
    cur = load_current()
    if cur is None or cur.get("game") != args.slug:
        print("[未命中] 尚无该游戏的实时快照（先 start + monitor）。")
        return 1
    print(f"游戏: {cur.get('game')} | 目录: {cur.get('dir')}")
    print(f"地图: {cur.get('map_name') or cur.get('map_id')} | 场景: {cur.get('scene')}")
    print(f"文本: {cur.get('text', '')}")
    print(f"来源: {cur.get('source')} | 更新: {cur.get('updated_at')}")
    return 0


def cmd_ocr(args) -> int:
    """直接 OCR 游戏窗口并打印文本（调试 / CDP 不可用时的兜底验证）。"""
    games = load_games()
    g = _find_game(games, args.slug)
    if g is None:
        print(f"[错误] 游戏库中无「{args.slug}」（先运行 scan）。")
        return 1
    try:
        text = ocr_game_text(g, engine=args.engine)
    except OcrUnavailableError as exc:
        print(f"[错误] OCR 不可用: {exc}")
        return 1
    except OSError as exc:
        print(f"[错误] {exc}")
        return 1
    if not text:
        print("[提示] OCR 未识别到文本（窗口画面可能无文字）。")
        return 1
    print(f"游戏: {g.slug} | 来源: ocr")
    print(f"文本: {text[:200]}")
    return 0


# ---------------------------------------------------------------------------
# 离线自测（不触碰真实 runtime/，全部走临时目录）
# ---------------------------------------------------------------------------

def _make_fake_game(base: Path, name: str, engine: str) -> Path:
    """构造一个最小可识别的假游戏目录；返回游戏根目录。"""
    game_dir = base / name
    game_dir.mkdir(parents=True)
    (game_dir / "Game.exe").write_bytes(b"MZ")  # 仅占位
    (game_dir / "package.json").write_text(
        json.dumps({"name": name, "main": "www/index.html" if engine == "mv"
                    else "index.html"}, ensure_ascii=False), encoding="utf-8")
    data_dir = game_dir / "www" / "data" if engine == "mv" else game_dir / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "Map001.json").write_text(json.dumps({
        "displayName": "序章·小镇",
        "events": [
            {"id": 1, "name": "村长", "pages": [{"list": [
                {"code": 101, "parameters": ["Actor1", 0, 0, 2]},
                {"code": 401, "parameters": ["欢迎来到小镇，旅行者。"]},
                {"code": 401, "parameters": ["这里很安全。"]},
                {"code": 102, "parameters": [["好的", "拒绝"]]},
                {"code": 402, "parameters": [0, "好的"]},
                {"code": 401, "parameters": ["那就先去旅馆休息吧。"]},
                {"code": 404, "parameters": []},
                {"code": 402, "parameters": [1, "拒绝"]},
                {"code": 401, "parameters": ["那随你便吧。"]},
                {"code": 404, "parameters": []},
            ]}]},
            {"id": 7, "name": "神秘少女", "pages": [{"list": [
                {"code": 101, "parameters": ["", 0, 0, 0]},
                {"code": 401, "parameters": ["（低声）你看见那道光了么？"]},
            ]}]},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    # 地图信息文件（顶层 list）：必须被提取器排除（回归用例）
    (data_dir / "MapInfos.json").write_text(json.dumps([
        None, {"id": 1, "name": "序章·小镇", "order": 0, "parentId": 0},
    ], ensure_ascii=False), encoding="utf-8")
    # 公共事件（CommonEvents.json）：开场/通用剧情文本，需被提取（回归用例）
    (data_dir / "CommonEvents.json").write_text(json.dumps([
        {"id": 218, "name": "", "list": [
            {"code": 401, "parameters": ["本游戏为同人作品。虽然已尽可能进行除错，"]},
            {"code": 401, "parameters": ["在物品栏中有几个除错用品。"]},
        ]},
    ], ensure_ascii=False), encoding="utf-8")
    # 战斗事件（Troops.json）：战斗对话，需被提取（回归用例）
    (data_dir / "Troops.json").write_text(json.dumps([
        {"id": 1, "name": "Boss战", "pages": [{"list": [
            {"code": 101, "parameters": ["", 0, 0, 0]},
            {"code": 401, "parameters": ["就凭你也想打败我？"]},
        ]}]},
    ], ensure_ascii=False), encoding="utf-8")
    # 武器（--all 扩展覆盖，回归用例）
    (data_dir / "Weapons.json").write_text(json.dumps([
        {"name": "铁剑", "description": "普通的铁剑。"},
    ], ensure_ascii=False), encoding="utf-8")
    (data_dir / "Items.json").write_text(json.dumps([
        {"name": "回复药", "description": "恢复 50 HP 的药水。"},
        None,
    ], ensure_ascii=False), encoding="utf-8")
    (data_dir / "System.json").write_text(
        json.dumps({"gameTitle": name}, ensure_ascii=False), encoding="utf-8")
    if engine == "mv":
        (game_dir / "js").mkdir()
        (game_dir / "js" / "rpg_core.js").write_text("", encoding="utf-8")
    else:
        (game_dir / "js").mkdir()
        (game_dir / "js" / "rmmz_core.js").write_text("", encoding="utf-8")
    return game_dir


def selftest() -> int:
    """离线自测：假游戏 → 发现/注册/提取全链路。"""
    tmp = Path(tempfile.mkdtemp(prefix="rmgame_test_"))
    # 重定向数据目录到临时位置，避免污染真实 runtime/
    import rmgame.discovery as disc
    import rmgame.extract as ext
    disc.RAW_DIR = tmp / "raw"
    disc.RUNTIME_DIR = tmp / "runtime"
    disc.WIKI_DIR = tmp / "wiki"
    disc.GAMES_FILE = disc.RUNTIME_DIR / "games.json"
    ext.RAW_DIR = disc.RAW_DIR
    try:
        mv = _make_fake_game(tmp / "games", "演示游戏", "mv")
        mz = _make_fake_game(tmp / "games", "MZDemo", "mz")

        # 发现
        found = discover(tmp / "games", recursive=True)
        assert len(found) == 2, f"应发现 2 个游戏，实际 {len(found)}"
        by_engine = {g.engine: g for g in found}
        assert by_engine["mv"].slug == "演示游戏", by_engine["mv"]
        assert by_engine["mz"].slug == "mzdemo", by_engine["mz"]
        assert by_engine["mz"].data_dir.endswith("data"), by_engine["mz"]
        print("  discovery: MV/MZ 识别 ✓")

        # 注册 + 幂等
        merged = register(found)
        assert len(merged) == 2 and len(register(found)) == 2, "重复注册应幂等"
        # launch_mode 默认 auto，保存/加载往返保留
        assert all(getattr(g, "launch_mode", "") == "auto" for g in merged), merged
        assert all(g.launch_mode == "auto" for g in load_games()), load_games()
        print("  register: 入库 + 幂等 + launch_mode 默认 auto ✓")

        # 提取（默认对话）
        g = next(x for x in load_games() if x.slug == "演示游戏")
        result = extract_game(g, all_text=False)
        assert result.map_count == 1, result.maps
        entries = result.maps[0]["entries"]
        assert len(entries) == 2, f"应 2 条对话，实际 {len(entries)}"
        e1 = entries[0]
        assert e1["speaker"] == "村长", e1
        assert e1["text"] == "欢迎来到小镇，旅行者。\n这里很安全。", e1["text"]
        assert e1["choices"] == ["好的", "拒绝"], e1["choices"]
        assert len(e1["branch_texts"]) == 2, e1["branch_texts"]
        assert e1["branch_texts"][0] == {"branch": "好的", "text": "那就先去旅馆休息吧。"}
        e2 = entries[1]
        assert e2["speaker"] == "神秘少女" and e2["text"] == "（低声）你看见那道光了么？", e2
        # 公共事件提取（CommonEvents.json，开场/通用剧情文本）
        assert len(result.common_events) == 1, result.common_events
        ce = result.common_events[0]
        assert ce["id"] == "CommonEvents.218.0", ce
        assert "同人作品" in ce["text"] and "除错用品" in ce["text"], ce
        # 战斗事件提取（Troops.json，战斗对话）
        assert len(result.troops) == 1, result.troops
        assert result.troops[0]["id"] == "Troops.1.0", result.troops
        assert "打败我" in result.troops[0]["text"], result.troops
        print("  extract: 对话聚合（多行/选项/分支/兜底）✓ | 公共事件 ✓ | 战斗事件 ✓")

        # 全量提取（--all 扩展：武器/防具/敌人等）
        result_all = extract_game(g, all_text=True)
        assert len(result_all.database) >= 3, result_all.database  # item + weapon + system
        kinds = {d["type"] for d in result_all.database}
        assert {"item", "weapon", "system"} <= kinds, kinds
        print("  extract --all: 数据库文本（道具/武器/技能/敌人等）✓")

        # 落盘
        path = write_raw(result)
        assert (path / "meta.json").exists() and (path / "maps" / "Map001.json").exists()
        assert (path / "CommonEvents.json").exists() and (path / "Troops.json").exists()
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8-sig"))
        assert meta["entry_count"] == 4, meta  # 2 地图 + 1 公共事件 + 1 战斗事件
        assert meta["troop_count"] == 1, meta
        print("  write_raw: meta/maps/CommonEvents/Troops 落盘 ✓")

        # ---- M1：概念发现（注入 fake LLM）→ 骨架 → 查询定位 → 引用解析 ----
        import rmgame.wiki as wiki_mod
        wiki_mod.WIKI_DIR = disc.WIKI_DIR          # 重定向到临时目录
        wiki_mod.RAW_DIR = disc.RAW_DIR            # resolve_raw_ref 用 wiki 模块的 RAW_DIR
        from .wiki import ensure_concept as ec, query_concept as qc, resolve_raw_ref

        def fake_llm(prompt):
            # 离线假响应：提示词要求 JSON 数组，返回固定概念清单
            return json.dumps([
                {"id": "c_village", "title": "村长", "kind": "character",
                 "summary": "小镇的村长，负责接待访客", "refs": ["Map001.1.0"]},
                {"id": "c_light", "title": "禁忌之光", "kind": "theme",
                 "summary": "反复出现的神秘意象", "refs": ["Map001.7.0"]},
            ], ensure_ascii=False)

        summary = build_maps_summary(disc.RAW_DIR / "演示游戏")
        assert len(summary) == 3 and summary[0]["map_name"] == "序章·小镇", summary
        assert summary[1]["map_file"] == "Troops.json", summary
        assert summary[2]["map_file"] == "CommonEvents.json", summary  # B：公共事件纳入概念发现
        concepts = concept_discovery("演示游戏", summary, llm_responder=fake_llm)
        assert len(concepts) == 2, concepts
        assert concepts[0]["title"] == "村长" and concepts[0]["kind"] == "character"
        assert concepts[0]["refs"] == ["Map001.1.0"], concepts[0]
        index = write_skeleton("演示游戏", "演示游戏", "mv", concepts)
        assert len(index["concepts"]) == 2, index
        assert all(c["status"] == "pending" for c in index["concepts"])
        assert (disc.WIKI_DIR / "演示游戏" / "index.md").exists()
        # 查询定位：命中 / 地图名匹配 / 未命中
        r1 = qc("演示游戏", "村长")
        assert r1["ok"] and r1["concept"]["title"] == "村长" and r1["status"] == "pending", r1
        r2 = qc("演示游戏", "map001")
        assert r2["ok"] and r2["concept"]["title"] == "村长", r2
        r3 = qc("演示游戏", "不存在的东西")
        assert not r3["ok"] and r3["reason"] == "no_concept", r3
        r4 = qc("无此游戏", "村长")
        assert not r4["ok"] and r4["reason"] == "no_wiki", r4
        # 多词查询（组合实体，如「契约之门 谢拉」）：拆词命中各自概念
        r_multi = qc("演示游戏", "村长 禁忌之光")
        assert r_multi["ok"] and r_multi["concept"]["title"] == "村长", r_multi
        assert r_multi["status"] == "pending", r_multi  # 未带 builder，不触发构建
        r_multi2 = qc("演示游戏", "禁忌之光 村长")  # 平分时按索引顺序（村长在前）
        assert r_multi2["ok"] and r_multi2["concept"]["title"] == "村长", r_multi2
        # 部分命中：多词中仅一词在库 → 命中该概念（不再整体落空）
        r_part = qc("演示游戏", "村长 完全不存在的词")
        assert r_part["ok"] and r_part["concept"]["title"] == "村长", r_part
        # 顿号分隔同样拆词命中
        r_dun = qc("演示游戏", "村长、禁忌之光")
        assert r_dun["ok"] and r_dun["concept"]["title"] == "村长", r_dun
        # 全部子词未命中 → 仍 no_concept
        r_none2 = qc("演示游戏", "完全不存在 也没有")
        assert not r_none2["ok"] and r_none2["reason"] == "no_concept", r_none2
        # query_concepts：多概念命中全部返回（高分在前，平分保持索引顺序）
        from .wiki import query_concepts as qcs
        r_cs = qcs("演示游戏", "村长 禁忌之光")
        assert r_cs["ok"] and len(r_cs["concepts"]) == 2, r_cs
        titles_cs = [x["concept"]["title"] for x in r_cs["concepts"]]
        assert titles_cs == ["村长", "禁忌之光"], titles_cs
        r_cs_none = qcs("演示游戏", "完全不存在 也没有")
        assert not r_cs_none["ok"] and r_cs_none["reason"] == "no_concept", r_cs_none
        print("  wiki 多词查询: 拆词命中/部分命中/全部落空/多概念返回 ✓")
        # raw:// 引用解析
        ref1 = resolve_raw_ref("raw://演示游戏/Map001.json#Map001.1.0")
        assert ref1["ok"] and ref1["data"]["speaker"] == "村长", ref1
        ref2 = resolve_raw_ref("raw://演示游戏/Map001.json")
        assert ref2["ok"] and ref2["entry_id"] is None and len(ref2["data"]["entries"]) == 2, ref2
        ref3 = resolve_raw_ref("raw://演示游戏/Map001.json#Map001.9.9")
        assert not ref3["ok"], ref3
        # 公共事件引用解析（CommonEvents.json）
        ref4 = resolve_raw_ref("raw://演示游戏/CommonEvents.json#CommonEvents.218.0")
        assert ref4["ok"] and "同人作品" in ref4["data"]["text"], ref4
        # 战斗事件引用解析（Troops.json）
        ref5 = resolve_raw_ref("raw://演示游戏/Troops.json#Troops.1.0")
        assert ref5["ok"] and "打败我" in ref5["data"]["text"], ref5
        print("  M1 wiki: 概念发现(注入LLM)/骨架/查询定位/引用解析(含公共/战斗事件) ✓")

        # ---- M2：懒构建 → 缓存命中 → 失败重试 → force 重建 ----
        calls = {"n": 0, "fail": False}

        def fake_builder(slug, concept):
            calls["n"] += 1
            if calls["fail"]:
                return None
            return (f"# 概念：{concept['title']}\n\n"
                    "## 概述\n（重写内容，不含原文）\n\n"
                    f"## 相关角色\n（{slug}）\n")

        # 1) pending 概念懒构建：查询触发 builder，条目落盘
        b1 = qc("演示游戏", "村长", builder=fake_builder)
        assert b1["ok"] and b1["status"] == "built" and calls["n"] == 1, b1
        assert b1["content"].startswith("# 概念：村长"), b1["content"]
        assert (disc.WIKI_DIR / "演示游戏" / "concepts" / "c-village.md").exists()
        # 2) 重复查询：直接命中缓存，builder 不再调用
        b2 = qc("演示游戏", "村长", builder=fake_builder)
        assert b2["ok"] and b2["status"] == "built" and calls["n"] == 1, b2
        # 3) 构建失败 → error 状态；下次查询重试成功
        calls["fail"] = True
        calls["n"] = 0
        b3 = qc("演示游戏", "禁忌之光", builder=fake_builder)
        assert not b3["ok"] and b3["reason"] == "build_failed", b3
        assert b3.get("status") == "error", b3
        assert calls["n"] == 1, calls  # 失败的那一次调用
        calls["fail"] = False
        b4 = qc("演示游戏", "禁忌之光", builder=fake_builder)
        assert b4["ok"] and b4["status"] == "built" and calls["n"] == 2, b4  # 重试成功
        # 4) force 强制重建 built 概念
        calls["n"] = 0
        b5 = ec("演示游戏", "c-village", fake_builder, force=True)
        assert b5["ok"] and b5["status"] == "built" and calls["n"] == 1, b5
        # 5) 概念条目文件名用 ASCII 安全 id
        idx2 = wiki_mod.load_index("演示游戏")
        assert all(c["file"] == f"concepts/{c['id']}.md" for c in idx2["concepts"]), idx2
        print("  M2 wiki: 懒构建/缓存命中/失败重试/force重建 ✓")

        # ---- 现场收录（A）：register_concept 追加新概念 + 懒构建 + 防重复 ----
        from .wiki import register_concept as rc
        calls2 = {"n": 0}

        def fake_builder2(slug, concept):
            calls2["n"] += 1
            return f"# 概念：{concept['title']}\n（自动收录内容）"

        n_before = len(wiki_mod.load_index("演示游戏")["concepts"])
        r_r1 = rc("演示游戏", {"title": "克己之诅咒", "kind": "lore",
                              "summary": "s", "refs": ["CommonEvents.218.0"]},
                  fake_builder2)
        assert r_r1["ok"] and r_r1["status"] == "built" and calls2["n"] == 1, r_r1
        assert r_r1["concept"]["id"].startswith("c-auto"), r_r1
        assert len(wiki_mod.load_index("演示游戏")["concepts"]) == n_before + 1
        # 同 title 再次收录 → 不重复追加（防重复），命中缓存不重调 builder
        r_r2 = rc("演示游戏", {"title": "克己之诅咒", "kind": "lore",
                              "summary": "s", "refs": ["CommonEvents.218.0"]},
                  fake_builder2)
        assert r_r2["ok"] and calls2["n"] == 1, r_r2
        assert len(wiki_mod.load_index("演示游戏")["concepts"]) == n_before + 1
        print("  wiki 现场收录: 未收录概念入索引+懒构建+防重复 ✓")

        # ---- 别名（aliases）：条目解析 / 查询命中 / 现场收录防分裂 ----
        from .wiki import _parse_aliases as pa, link_alias as la
        # 1) 条目 `> 别名：` 行解析（显式 / 无 / 未标注）
        assert pa("# 概念：X\n\n> 别名：甲、乙、丙") == ["甲", "乙", "丙"], pa
        assert pa("# 概念：X\n\n> 别名：无") == [], pa
        assert pa("# 概念：X\n\n## 概述\n正文") is None, pa
        # 2) 手动并入别名后，别名查询可命中该概念（_match_score alias 分支）
        idx_a = wiki_mod.load_index("演示游戏")
        c_curse = next(c for c in idx_a["concepts"] if c["title"] == "克己之诅咒")
        assert c_curse.get("aliases", []) == [], c_curse  # 假 builder 未标注 → 保留旧值
        c_curse["aliases"] = ["猎妻迷宫之诅咒"]
        wiki_mod.save_index(idx_a)
        r_alias = qc("演示游戏", "猎妻迷宫之诅咒")
        assert r_alias["ok"] and r_alias["concept"]["title"] == "克己之诅咒", r_alias
        # 3) link_alias 防分裂：refs 重叠 → 并入别名，不建新概念
        n_before2 = len(wiki_mod.load_index("演示游戏")["concepts"])
        r_la = la("演示游戏", "克己之咒", ["CommonEvents.218.0"])  # 与克己之诅咒 refs 重叠
        assert r_la.get("linked") and r_la["concept"]["title"] == "克己之诅咒", r_la
        assert "克己之咒" in (r_la.get("aliases") or []), r_la
        assert len(wiki_mod.load_index("演示游戏")["concepts"]) == n_before2, "防分裂不应新增概念"
        # 无重叠 → 不归属（现场收录应注册新概念）
        r_la2 = la("演示游戏", "完全无关词", ["Map001.9.9"])
        assert not r_la2.get("linked"), r_la2
        # 4) 匹配优先级：title 精确 > alias 子串
        r_prio = qc("演示游戏", "克己之诅咒")
        assert r_prio["ok"] and r_prio["concept"]["title"] == "克己之诅咒", r_prio
        print("  wiki 别名: 条目解析/别名命中/防分裂/优先级 ✓")

        # 概念名去重：write_skeleton 以 title 为键（重复词条消除，refs 并集）
        from .rewriter import write_skeleton as ws2
        idx3 = ws2("演示游戏", "演示游戏", "mv", [
            {"id": "c-a", "title": "重复概念", "kind": "character",
             "summary": "x", "refs": ["Map001.1.0"]},
            {"id": "c-b", "title": "重复概念", "kind": "character",
             "summary": "y", "refs": ["Map001.1.1"]},
            {"id": "c-c", "title": "独立概念", "kind": "theme",
             "summary": "z", "refs": ["Map001.1.2"]},
        ])
        titles3 = [c["title"] for c in idx3["concepts"]]
        assert titles3 == ["重复概念", "独立概念"], titles3
        r_dup = [c for c in idx3["concepts"] if c["title"] == "重复概念"][0]
        assert sorted(r_dup["refs"]) == ["Map001.1.0", "Map001.1.1"], r_dup
        print("  wiki 去重: 概念名唯一（refs 并集）✓")

        # query_wiki 空参数：返回现有概念列表（不触发 LLM）
        from .wiki import list_concepts as lc2
        lst3 = lc2("演示游戏")
        assert any(c["title"] == "重复概念" for c in lst3), lst3
        from .bridge import execute_tool as et3
        r_empty = et3("query_wiki", {"game": "演示游戏"})
        assert "概念列表" in r_empty and "重复概念" in r_empty, r_empty
        print("  query_wiki 空参数: 返回概念列表 ✓")

        # _rewrite_prompt：附既有 wiki（过期标注，以 RAW 为准）；无旧内容不附
        from .rewriter import _rewrite_prompt as rp
        p_rw = rp("演示游戏", {"title": "测试", "kind": "theme", "summary": "s"},
                  [], old_wiki="旧条目内容XYZ")
        assert "既有 wiki 内容" in p_rw and "RAW 内容为准" in p_rw, p_rw
        assert "旧条目内容XYZ" in p_rw, p_rw
        p_rw2 = rp("演示游戏", {"title": "测试", "kind": "theme", "summary": "s"}, [])
        assert "既有 wiki 内容" not in p_rw2, "无旧内容不应附"
        print("  wiki 重写: 附既有内容（过期标注/以RAW为准）✓")

        # ---- read_raw_text：raw → LLM 友好中间格式（地图/说话人/关键词/工具执行）----
        import rmgame.llmfmt as llmfmt_mod
        llmfmt_mod.RAW_DIR = disc.RAW_DIR
        from .llmfmt import build_friendly as bf
        from .bridge import execute_tool as et2

        t_all = bf("演示游戏")
        assert "【游戏：演示游戏】" in t_all and "Map001" in t_all, t_all[:200]
        assert "村长" in t_all and "同人作品" in t_all, t_all[:300]
        t_map = bf("演示游戏", query="Map001")
        assert "村长" in t_map and "同人作品" not in t_map, t_map[:200]
        t_spk = bf("演示游戏", query="神秘少女")
        assert "神秘少女" in t_spk and "村长" not in t_spk, t_spk[:200]
        t_ce = bf("演示游戏", query="CommonEvents")
        assert "同人作品" in t_ce and "村长" not in t_ce, t_ce[:200]
        r_rt = et2("read_raw_text", {"game": "演示游戏", "query": "同人作品"})
        assert "同人作品" in r_rt and "除错用品" in r_rt, r_rt
        r_rt2 = et2("read_raw_text", {"game": "演示游戏"})
        assert "Map001" in r_rt2, r_rt2[:200]
        print("  read_raw_text: raw→LLM友好格式（地图/说话人/关键词/工具执行）✓")

        # ---- wiki 写入确认：概念发现摘要纳入 战斗事件 + 数据库 ----
        from .rewriter import build_maps_summary as bms
        sum1 = bms(disc.RAW_DIR / "演示游戏")
        files1 = {s["map_file"] for s in sum1}
        assert "Troops.json" in files1, files1          # 战斗对话进入概念发现
        result_all2 = extract_game(g, all_text=True)     # --all 提取后
        write_raw(result_all2)
        sum2 = bms(disc.RAW_DIR / "演示游戏")
        files2 = {s["map_file"] for s in sum2}
        assert "database.json" in files2, files2         # 道具/技能进入概念发现
        db_seg = next(s for s in sum2 if s["map_file"] == "database.json")
        assert db_seg["events"].get("weapon") == 1, db_seg
        # llmfmt 数据库查询（角色可读道具/技能）
        r_db = et2("read_raw_text", {"game": "演示游戏", "query": "database"})
        assert "铁剑" in r_db, r_db
        r_db2 = et2("read_raw_text", {"game": "演示游戏", "query": "铁剑"})
        assert "铁剑" in r_db2 and "回复药" not in r_db2, r_db2
        # llmfmt 战斗事件查询（角色可读战斗对话）
        r_tp = et2("read_raw_text", {"game": "演示游戏", "query": "Troops"})
        assert "打败我" in r_tp, r_tp
        r_tp2 = et2("read_raw_text", {"game": "演示游戏", "query": "打败我"})
        assert "打败我" in r_tp2 and "村长" not in r_tp2, r_tp2
        print("  wiki 写入: 战斗事件+数据库进入概念发现摘要 / llmfmt 可查道具/技能/战斗 ✓")

        # ---- M3：monitor（fake evaluator）→ 启动注入 → 快照 → 轮询 ----
        import rmgame.monitor as mon
        mon.RUNTIME_DIR = disc.RUNTIME_DIR          # 重定向到临时目录
        mon.CURRENT_FILE = mon.RUNTIME_DIR / "current.json"
        from .monitor import (
            build_snapshot as bs, load_current as lc,
            monitor_loop as ml, port_for as pf, start_game as sg,
        )
        # 1) 端口分配：固定（跨进程稳定）且落在 10240-60239 段（1.0 扩大端口空间）
        p1, p2 = pf("演示游戏"), pf("演示游戏")
        assert p1 == p2 and 10240 <= p1 < 60240, p1
        assert pf("演示游戏") != pf("MZDemo") or True  # 允许碰撞，仅断言格式
        # 2) 启动注入 dry-run：命令含调试端口参数
        sr = sg(g, dry_run=True)
        assert sr["ok"] and sr["state"] == "would_start", sr
        assert any("--remote-debugging-port=" in c for c in sr["cmd"]), sr
        # 3) 快照构建（fake evaluator：超出序列后保持最后一个）
        m3 = {"texts": ["欢迎来到小镇……", "你好，旅行者。", "你好，旅行者。"], "i": 0}

        def fake_eval(expr):
            t = m3["texts"][min(m3["i"], len(m3["texts"]) - 1)]
            m3["i"] += 1
            return json.dumps({"mapId": 1, "mapName": "序章·小镇",
                               "scene": "Scene_Map", "text": t,
                               "battleTroop": "", "battlePhase": ""})

        snap = bs(g, evaluator=fake_eval)
        assert snap["map_id"] == 1 and snap["map_name"] == "序章·小镇", snap
        assert snap["scene"] == "Scene_Map" and snap["text"] == "欢迎来到小镇……", snap
        assert snap["game"] == g.slug and snap["source"] == "cdp", snap
        assert snap["battle_troop"] == "" and snap["battle_phase"] == "", snap  # 非战斗空
        # 战斗场景：battleTroop/battlePhase/玩家方信息映射到快照字段
        snap_bt = bs(g, evaluator=lambda e: json.dumps({
            "mapId": 2, "mapName": "", "scene": "Scene_Battle",
            "text": "", "battleTroop": "中级眷族Ａ、中级眷族Ｂ、中级眷族Ｃ",
            "battlePhase": "input",
            "partyInfo": "谢拉(2972/2972 MP:100/100 TP:3/100)", "actorInfo": "谢拉",
            "actorCommands": "攻击、技能、防御、道具",
            "skillList": "红莲地狱:15mp、狂龙气息:45mp", "skillCurrent": "狂龙气息：详情"}))
        assert snap_bt["scene"] == "Scene_Battle", snap_bt
        assert snap_bt["battle_troop"] == "中级眷族Ａ、中级眷族Ｂ、中级眷族Ｃ", snap_bt
        assert snap_bt["battle_phase"] == "input", snap_bt
        assert snap_bt["party_info"] == "谢拉(2972/2972 MP:100/100 TP:3/100)", snap_bt
        assert snap_bt["actor_info"] == "谢拉", snap_bt
        assert snap_bt["actor_commands"] == "攻击、技能、防御、道具", snap_bt
        assert snap_bt["skill_list"] == "红莲地狱:15mp、狂龙气息:45mp", snap_bt
        assert snap_bt["skill_current"] == "狂龙气息：详情", snap_bt
        # 菜单场景：menuCommands/menuCurrent 映射到快照字段
        snap_menu = bs(g, evaluator=lambda e: json.dumps({
            "mapId": 4, "mapName": "", "scene": "Scene_Menu", "text": "",
            "partyInfo": "谢拉(2972/2972 MP:100/100 TP:0/100)",
            "menuCommands": "物品、技能、装备", "menuCurrent": "物品"}))
        assert snap_menu["scene"] == "Scene_Menu", snap_menu
        assert snap_menu["menu_commands"] == "物品、技能、装备", snap_menu
        assert snap_menu["menu_current"] == "物品", snap_menu
        # 通用通配：自定义界面（图鉴等）列表选中 + 帮助文本
        snap_wild = bs(g, evaluator=lambda e: json.dumps({
            "mapId": 4, "mapName": "", "scene": "Scene_Glossary", "text": "",
            "listCurrent": "等级1", "helpText": "显示角色等级成长"}))
        assert snap_wild["scene"] == "Scene_Glossary", snap_wild
        assert snap_wild["list_current"] == "等级1", snap_wild
        assert snap_wild["help_text"] == "显示角色等级成长", snap_wild
        # 4) 轮询 3 轮：写 current.json，文本变化/不变时序
        n = ml(g, evaluator=fake_eval, interval=0, max_rounds=3)
        assert n == 3, n
        cur = lc()
        assert cur is not None and cur["game"] == g.slug, cur
        assert cur["text"] == "你好，旅行者。" and cur["map_name"] == "序章·小镇", cur
        assert cur["updated_at"], cur  # ISO 时间戳
        # 5) 无新对话时 updated_at 保留（读两次相同文本）
        t1 = lc()["updated_at"]
        ml(g, evaluator=fake_eval, interval=0, max_rounds=1)
        t2 = lc()["updated_at"]
        assert t1 == t2, f"相同文本不应刷新时间戳: {t1} != {t2}"
        # 6) CDP 快照解析：双重编码兼容（_STATE_EXPR stringify + cdp_evaluate dumps）
        from .monitor import _parse_state
        assert _parse_state('"{\\"mapId\\":0,\\"text\\":\\"x\\"}"') == {"mapId": 0, "text": "x"}
        assert _parse_state('{"mapId": 1}') == {"mapId": 1}
        assert _parse_state("not json") == {}
        print("  M3 monitor: 端口分配/启动注入/快照/轮询/新对话检测/双重编码解析 ✓")

        # ---- M4：OCR 兜底（CDP 失败自动降级 + ocr_only + 来源标注）----
        from .monitor import read_state as rs

        def cdp_fail_eval(expr):
            raise ConnectionError("CDP 不可用（模拟）")

        def fake_ocr(game):
            return "（OCR识别）欢迎来到小镇……"

        # 1) CDP 失败 → OCR 兜底：source=ocr，文本来自 ocr_fn
        st1 = rs(g, evaluator=cdp_fail_eval, ocr_fn=fake_ocr)
        assert st1["_source"] == "ocr" and st1["text"].startswith("（OCR识别）"), st1
        snap4 = bs(g, evaluator=cdp_fail_eval, ocr_fn=fake_ocr)
        assert snap4["source"] == "ocr" and snap4["text"].startswith("（OCR识别）"), snap4
        assert snap4["map_name"] == "" and snap4["scene"] == "", snap4  # OCR 无地图信息
        # 2) 完整降级轮询：CDP 失败 → OCR → current.json source=ocr
        n4 = ml(g, evaluator=cdp_fail_eval, ocr_fn=fake_ocr, interval=0, max_rounds=1)
        assert n4 == 1, n4
        cur4 = lc()
        assert cur4["source"] == "ocr" and cur4["text"].startswith("（OCR识别）"), cur4
        # 3) ocr_only：跳过 CDP 直接 OCR
        st5 = rs(g, evaluator=lambda e: "不应被调用", ocr_fn=fake_ocr, ocr_only=True)
        assert st5["_source"] == "ocr" and st5["text"].startswith("（OCR识别）"), st5
        # 4) CDP 可用时 source=cdp（回归）
        st6 = rs(g, evaluator=lambda e: json.dumps({"mapId": 1, "text": "CDP文本"}))
        assert st6["_source"] == "cdp" and st6["text"] == "CDP文本", st6
        print("  M4 ocr: CDP失败降级/OCR-only/来源标注 ✓")

        # ---- M6：自动发现入库 + 信任分级 + 确认解锁 ----
        import rmgame.monitor as mon6
        from .discovery import (
            auto_register as ar6, approve as ap6, discover_dir as dd6,
        )
        from .bridge import execute_tool as et6

        # 1) trust 字段：GameInfo 缺省 user；auto_register 幂等；旧库缺字段兼容
        auto_dir = _make_fake_game(tmp / "games6", "自动发现演示", "mv")
        g6 = GameInfo(slug="自动发现演示", name="自动发现演示",
                      exe_path=str(auto_dir / "Game.exe"), dir=str(auto_dir),
                      engine="mv", data_dir=str(auto_dir / "www" / "data"))
        assert g6.trust == "user" and g6.last_seen == "", g6
        assert ar6(g6) is True, "首次自动入库应写入"
        assert ar6(g6) is False, "重复自动入库应幂等"
        g6l = next(x for x in load_games() if x.slug == "自动发现演示")
        assert g6l.trust == "auto" and g6l.added_at and g6l.last_seen, g6l
        assert g6l.launch_mode == "auto", "auto_register 不改 launch_mode"
        g6_legacy = GameInfo(slug="legacy游戏", name="legacy游戏",
                             exe_path=str(auto_dir / "x" / "Game.exe"),
                             dir=str(auto_dir / "x"), engine="mv",
                             data_dir=str(auto_dir / "x" / "www" / "data"))
        g6_legacy_dict = {k: v for k, v in g6_legacy.to_dict().items()
                          if k not in ("trust", "last_seen")}
        assert GameInfo.from_dict(g6_legacy_dict).trust == "user", \
            "旧库缺 trust 字段应默认 user"
        # approve 升级：auto → user；重复/无记录
        _g6a, msg6 = ap6("自动发现演示")
        assert _g6a is not None and "已确认" in msg6, msg6
        assert next(x for x in load_games()
                    if x.slug == "自动发现演示").trust == "user"
        _g6b, msg6b = ap6("自动发现演示")
        assert "无需升级" in msg6b, msg6b
        _g6n, msg6c = ap6("不存在的游戏")
        assert _g6n is None and "无" in msg6c, msg6c
        print("  M6 trust: auto 入库幂等 / 旧库缺省 user / approve 升级 ✓")

        # 2) discover_dir：单目录引擎判定（复用 _is_engine）
        d6 = dd6(str(tmp / "games" / "MZDemo" / "Game.exe"))
        assert d6 is not None and d6.engine == "mz", d6
        assert dd6(str(tmp / "games6" / "无此目录" / "Game.exe")) is None
        print("  M6 discover_dir: 单目录引擎判定 ✓")

        # 3) enumerate_running：进程枚举 + 端口解析（注入 enum_fn）
        rows6 = [(str(auto_dir / "Game.exe"),
                  "--remote-debugging-port=9311", "501")]
        run6, ports6 = mon6.enumerate_running(lambda: rows6)
        assert len(run6) == 1 and run6[0].engine == "mv", run6
        assert ports6.get(str(auto_dir / "Game.exe").lower()) == 9311, ports6
        print("  M6 enumerate_running: 进程枚举+端口解析（注入）✓")

        # 4) monitor_loop_all 自动发现：未注册运行中游戏 → 自动入库 trust=auto
        #    （patch build_snapshot 快速返回空，避免真实 CDP/OCR 网络调用）
        _orig_build_snapshot = mon6.build_snapshot
        mon6.build_snapshot = lambda g, port=None: {}
        try:
            n6 = mon6.monitor_loop_all([], interval=0, max_rounds=1,
                                       enum_fn=lambda: rows6)
            assert n6 == 1, n6
            # 自动发现演示已 approve 为 user：auto_register 幂等不应降级
            assert next(x for x in load_games()
                        if x.slug == "自动发现演示").trust == "user", \
                "auto_register 不应降级已确认游戏"
            rows6b = [(str(_make_fake_game(tmp / "games7", "自动发现二号", "mz")
                          / "Game.exe"), "", "502")]
            n6b = mon6.monitor_loop_all([], interval=0, max_rounds=1,
                                        enum_fn=lambda: rows6b)
            assert n6b == 1
            g6b2 = next(x for x in load_games() if x.slug == "自动发现二号")
            assert g6b2.trust == "auto" and g6b2.last_seen, g6b2
            total_before = len(load_games())
            mon6.monitor_loop_all([], interval=0, max_rounds=1,
                                  enum_fn=lambda: rows6b)
            assert len(load_games()) == total_before, "重复发现不应重复入库"
            # 回调 on_auto_register：仅首次自动入库触发，重复轮次不触发
            rows6c = [(str(_make_fake_game(tmp / "games8", "自动发现三号", "mv")
                          / "Game.exe"), "", "503")]
            cb6 = []
            mon6.monitor_loop_all([], interval=0, max_rounds=1,
                                  enum_fn=lambda: rows6c,
                                  on_auto_register=cb6.append)
            assert len(cb6) == 1 and cb6[0].slug == "自动发现三号", cb6
            mon6.monitor_loop_all([], interval=0, max_rounds=1,
                                  enum_fn=lambda: rows6c,
                                  on_auto_register=cb6.append)
            assert len(cb6) == 1, "已入库不应重复回调"
        finally:
            mon6.build_snapshot = _orig_build_snapshot
        print("  M6 monitor 自动发现: 未注册运行中游戏入库 trust=auto / 幂等 / 不降级 / 回调 ✓")

        # 5) bridge discover_running：入库状态标注（monkeypatch 枚举避免真实 PowerShell）
        _orig_enum = mon6.enumerate_running
        mon6.enumerate_running = lambda enum_fn=None: (
            [next(x for x in load_games() if x.slug == "自动发现二号")], {})
        try:
            r_dr = et6("discover_running", {})
            assert "自动发现二号" in r_dr and "待允许启动" in r_dr, r_dr
            mon6.enumerate_running = lambda enum_fn=None: (
                [next(x for x in load_games() if x.slug == "自动发现演示")], {})
            r_dr2 = et6("discover_running", {})
            assert "可启动" in r_dr2, r_dr2
        finally:
            mon6.enumerate_running = _orig_enum
        # 6) start_game 信任闸门：trust=auto 拒绝并给 UI 确认指引
        r_sg = et6("start_game", {"game": "自动发现二号"})
        assert "还不能启动" in r_sg and "确认气泡" in r_sg \
            and "🎮 游戏" in r_sg, r_sg
        print("  M6 bridge: discover_running 状态标注 / start_game 信任闸门 ✓")

        # 7) CLI scan --approve：升级 / 无记录报错
        import argparse
        from .cli import cmd_scan as cs6
        ns_ap = argparse.Namespace(root=None, recursive=True, yes=False,
                                   running=False, approve="自动发现二号")
        assert cs6(ns_ap) == 0, "approve 应成功"
        assert next(x for x in load_games()
                    if x.slug == "自动发现二号").trust == "user"
        ns_no = argparse.Namespace(root=None, recursive=True, yes=False,
                                   running=False, approve="不存在游戏")
        assert cs6(ns_no) == 1, "无记录应报错"
        print("  M6 cli: scan --approve 升级 / 无记录报错 ✓")

        # 7) M7 引擎标题择优 / aliases 自动采集 / register 自愈
        #    （版本子目录 [JP][ver1.11] 场景：目录名无意义 → 用 System.json gameTitle）
        base7 = tmp / "games9"
        pkg7 = base7 / "[淫乱轮轴][輪淫のスピンドル][Spindle]" / "[JP][ver1.11]"
        pkg7.mkdir(parents=True)
        (pkg7 / "Game.exe").write_bytes(b"MZ")
        (pkg7 / "package.json").write_text(
            json.dumps({"name": "", "main": "www/index.html"}), encoding="utf-8")
        (pkg7 / "js").mkdir()
        (pkg7 / "js" / "rpg_core.js").write_text("", encoding="utf-8")
        d7 = pkg7 / "www" / "data"
        d7.mkdir(parents=True)
        (d7 / "System.json").write_text(
            json.dumps({"gameTitle": "輪淫のスピンドル"}, ensure_ascii=False),
            encoding="utf-8")
        found7 = discover(base7, recursive=True)
        assert len(found7) == 1, found7
        g7 = found7[0]
        from .discovery import make_slug as ms7
        assert g7.name == "輪淫のスピンドル", g7.name      # 目录名无意义 → 引擎标题
        assert g7.slug == ms7("輪淫のスピンドル"), g7.slug
        assert "[JP][ver1.11]" in g7.aliases, g7.aliases  # 目录名入别名
        assert "淫乱轮轴" in g7.aliases and "Spindle" in g7.aliases, g7.aliases
        print("  M7 discovery: 版本子目录 → System.json gameTitle 择优 + 父目录拆别名 ✓")

        # register 自愈：旧条目（版本号名）同目录命中 → 刷新 name/aliases，slug 稳定
        old7 = GameInfo(slug="jp-ver1-11", name="[JP][ver1.11]",
                        exe_path=str(pkg7 / "Game.exe"), dir=str(pkg7),
                        engine="mv", data_dir=str(d7))
        disc.save_games([old7])
        merged7 = register(found7)
        assert len(merged7) == 1, "不应重复入库"
        g7r = next(x for x in merged7 if x.dir == str(pkg7))
        assert g7r.slug == "jp-ver1-11", g7r.slug          # slug 稳定（数据目录不变）
        assert g7r.name == "輪淫のスピンドル", g7r.name
        assert "淫乱轮轴" in g7r.aliases, g7r.aliases
        print("  M7 register: 同目录自愈刷新 name/aliases（slug 稳定）✓")

        # _resolve_game：name / aliases / slug 三种叫法均命中
        from .bridge import _resolve_game as rg7
        for key7 in ("輪淫のスピンドル", "淫乱轮轴", "[JP][ver1.11]", "jp-ver1-11"):
            gg7, err7 = rg7({"game": key7})
            assert gg7 is not None and gg7.slug == "jp-ver1-11", (key7, err7)
        print("  M7 bridge: _resolve_game 命中 name / aliases / slug ✓")

        # 目录名有意义时保留目录名（防引擎标题带版本尾巴反而更差，猎妻迷宫场景）
        g7e = _make_fake_game(tmp / "games7b", "猎妻迷宫", "mv")
        (g7e / "www" / "data" / "System.json").write_text(
            json.dumps({"gameTitle": "猎妻迷宫_BokiBoki官方中文版ver107"},
                       ensure_ascii=False), encoding="utf-8")
        found7e = discover(tmp / "games7b", recursive=True)
        assert found7e[0].name == "猎妻迷宫", found7e[0].name
        assert "猎妻迷宫_BokiBoki官方中文版ver107" in found7e[0].aliases, \
            found7e[0].aliases
        print("  M7 discovery: 目录名有意义 → 保留目录名（engine_title 入别名）✓")

        print("[rmgame.selftest] 全部通过 ✓")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="rmgame", description="RPG Maker 游戏文本点评工具（M0：发现+提取）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="扫描目录/运行中进程发现游戏并入游戏库")
    p_scan.add_argument("root", nargs="?",
                        help="扫描根目录（--running 或 --approve 时省略）")
    p_scan.add_argument("--recursive", action="store_true", default=True,
                        help="递归扫描（默认开）")
    p_scan.add_argument("--yes", "-y", action="store_true", help="跳过确认直接入库")
    p_scan.add_argument("--running", action="store_true",
                        help="扫描运行中的 RPG Maker 进程（名称/引擎/目录）并入库")
    p_scan.add_argument("--approve", metavar="slug",
                        help="升级指定游戏 trust auto → user（解锁角色启动）")
    p_scan.set_defaults(func=cmd_scan)

    p_extract = sub.add_parser("extract", help="提取游戏文本 → raw/<slug>/")
    p_extract.add_argument("slug", help="游戏 slug 或名称（见 status）")
    p_extract.add_argument("--all", action="store_true",
                           help="同时提取物品/技能/角色等数据库文本")
    p_extract.set_defaults(func=cmd_extract)

    p_wiki = sub.add_parser("wiki", help="概念发现（阶段1）→ 生成 wiki 骨架")
    p_wiki.add_argument("slug", help="游戏 slug 或名称")
    p_wiki.add_argument("--model", help="覆盖 LLM 模型（默认 setting/llm.ini）")
    p_wiki.add_argument("--temperature", type=float, help="覆盖 temperature")
    p_wiki.set_defaults(func=cmd_wiki)

    p_query = sub.add_parser("query", help="按关键词定位概念条目（pending 时懒构建）")
    p_query.add_argument("slug", help="游戏 slug 或名称")
    p_query.add_argument("query", help="概念名/关键词/地图名")
    p_query.add_argument("--no-build", action="store_true",
                         help="只定位不构建（pending 不触发 LLM）")
    p_query.add_argument("--force", action="store_true",
                         help="已 built 也强制重建（覆盖缓存）")
    p_query.add_argument("--model", help="覆盖 LLM 模型（默认 setting/llm.ini）")
    p_query.add_argument("--temperature", type=float, help="覆盖 temperature")
    p_query.set_defaults(func=cmd_query)

    p_start = sub.add_parser("start", help="以 CDP 调试端口启动游戏")
    p_start.add_argument("slug", help="游戏 slug 或名称")
    p_start.add_argument("--port", type=int, help="覆盖调试端口（默认按 slug 分配）")
    p_start.add_argument("--dry-run", action="store_true", help="只打印命令不启动")
    p_start.set_defaults(func=cmd_start)

    p_monitor = sub.add_parser("monitor", help="轮询读取状态 → runtime/current.json")
    p_monitor.add_argument("slug", help="游戏 slug 或名称")
    p_monitor.add_argument("--port", type=int, help="覆盖调试端口")
    p_monitor.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒")
    p_monitor.add_argument("--once", action="store_true", help="只读一次")
    p_monitor.add_argument("--rounds", type=int, help="轮询次数（默认无限）")
    p_monitor.add_argument("--ocr-only", action="store_true",
                           help="跳过 CDP 强制走 OCR（兜底调试）")
    p_monitor.set_defaults(func=cmd_monitor)

    p_current = sub.add_parser("current", help="显示当前快照")
    p_current.add_argument("slug", help="游戏 slug 或名称")
    p_current.set_defaults(func=cmd_current)

    p_ocr = sub.add_parser("ocr", help="直接 OCR 游戏窗口并打印文本")
    p_ocr.add_argument("slug", help="游戏 slug 或名称")
    p_ocr.add_argument("--engine", default="auto", help="OCR 引擎（默认 auto）")
    p_ocr.set_defaults(func=cmd_ocr)

    p_status = sub.add_parser("status", help="游戏库与 raw 状态")
    p_status.set_defaults(func=cmd_status)

    p_test = sub.add_parser("selftest", help="离线自测")
    p_test.set_defaults(func=lambda a: selftest())

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
