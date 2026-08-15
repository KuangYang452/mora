# -*- coding: utf-8 -*-
"""守护进程：CDP 实时读取 —— rmgame/monitor（M3）

职责（设计文档 §4.4）：
- start_game：以 `--remote-debugging-port=<port>` 注入参数启动游戏
- read_state：CDP Runtime.evaluate 读取游戏内部状态（当前文本/地图/场景）
- 快照 → runtime/current.json（原子写），新对话时更新 updated_at
- monitor_loop：轮询守护（可独立进程运行，也可被 CLI --once 单次调用）

CDP 传输可注入（evaluator）以支持离线自测；默认走 rmgame.cdp。
JS 表达式针对 MV/MZ 全局对象（$gameMessage/$gameMap/SceneManager），
版本差异由 _STATE_EXPR 维护（文档 R3）。
"""

import json
import re
import subprocess
import time
import datetime as _dt
from pathlib import Path

from .discovery import RUNTIME_DIR, GameInfo
import settings

# 外部命令（PowerShell 等控制台程序）在 pythonw（无控制台）环境下默认会为
# 每个子进程新建控制台窗口（一闪而过）；统一加 CREATE_NO_WINDOW 抑制。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 运行配置热键经 settings.app_get 运行期读取（M3 配置分层，见
# docs/REFACTOR_DESIGN.md §6：cdp/nwjs/auto_discover 修改后即时生效）；
# 不再持有模块级启动期快照。

CURRENT_FILE = RUNTIME_DIR / "current.json"

# 端口段：按 slug 哈希在 10240-60239 固定分配（跨进程稳定）。
# 1.0 起扩大端口空间（原 9222-9321 固定 100 端口易被"扫端口即得"命中，
# 见 docs/1.0_RELEASE_PLAN.md §5.1 T1 加固项 3）。
PORT_BASE = 10240
PORT_SPAN = 50000

# 游戏状态读取表达式（MV/MZ 全局对象；JSON.stringify 保证可序列化）
_STATE_EXPR = (
    "JSON.stringify({"
    "mapId: (function(){try{return (typeof $gameMap!=='undefined'&&$gameMap.mapId)?"
    "$gameMap.mapId():null}catch(e){return null}})(),"
    "mapName: (function(){try{return (typeof $gameMap!=='undefined'&&"
    "typeof $gameMap.displayName==='function')?($gameMap.displayName()||''):"
    "((typeof $gameMap!=='undefined'&&typeof $gameMap.displayName==='string')?"
    "$gameMap.displayName:'')}catch(e){return ''}})(),"
    "scene: (function(){try{return (typeof SceneManager!=='undefined'&&SceneManager._scene)?"
    "SceneManager._scene.constructor.name:''}catch(e){return ''}})(),"
    "text: (function(){try{return (typeof $gameMessage!=='undefined'&&"
    "typeof $gameMessage.allText==='function')?$gameMessage.allText():''}"
    "catch(e){return ''}})(),"
    "battleTroop: (function(){try{"
    "if(typeof $gameTroop!=='undefined'&&$gameTroop.members&&"
    "$gameTroop.members().length){"
    "var names=$gameTroop.members().map(function(m){return m.name();});"
    "var counts={};names.forEach(function(n){counts[n]=(counts[n]||0)+1;});"
    "var parts=[];for(var k in counts){parts.push(counts[k]>1?(k+'×'+counts[k]):k);}"
    "return parts.slice(0,4).join('、');}"
    "var t=(typeof $gameTroop!=='undefined'&&$gameTroop._troopId)?$gameTroop._troopId:null;"
    "if(t===null)return '';"
    "return (typeof $dataTroops!=='undefined'&&$dataTroops&&$dataTroops[t]&&"
    "$dataTroops[t].name)?$dataTroops[t].name:('部队'+t)}catch(e){return ''}})(),"
    "battlePhase: (function(){try{"
    "var bm=(typeof BattleManager!=='undefined')?BattleManager:null;"
    "if(bm&&bm._phase!==undefined&&bm._phase!==null)return String(bm._phase);"
    "var s=(typeof SceneManager!=='undefined'&&SceneManager._scene)?SceneManager._scene:null;"
    "return (s&&s.constructor.name==='Scene_Battle'&&s._phase)?String(s._phase):''"
    "}catch(e){return ''}})(),"
    "partyInfo: (function(){try{"
    "if(typeof $gameParty==='undefined'||!$gameParty.members)return '';"
    "var ms=$gameParty.members();if(!ms||!ms.length)return '';"
    "var parts=[];for(var i=0;i<ms.length;i++){var m=ms[i];"
    "var nm=(typeof m.name==='function')?m.name():String(m.name);"
    "var hp=(typeof m.hp==='function')?m.hp():m.hp;"
    "var mhp=(typeof m.mhp==='function')?m.mhp():m.mhp;"
    "var mp=(typeof m.mp==='function')?m.mp():m.mp;"
    "var mmp=(typeof m.mmp==='function')?m.mmp():m.mmp;"
    "var tp=(typeof m.tp==='function')?m.tp():m.tp;"
    "var mtp=(typeof m.maxTp==='function')?m.maxTp():(typeof m.maxTp==='number'?m.maxTp:undefined);"
    "var hpS=(hp===undefined||hp===null)?'?':hp;"
    "var mpS=(mp===undefined||mp===null)?'?':mp+'/'+(mmp===undefined||mmp===null?'?':mmp);"
    "var tpS=(tp===undefined||tp===null)?'':' TP:'+tp+'/'+(mtp===undefined||mtp===null?'?':mtp);"
    "parts.push(nm+'('+hpS+'/'+(mhp===undefined||mhp===null?'?':mhp)+' MP:'+mpS+tpS+')');}"
    "return parts.slice(0,6).join('、');"
    "}catch(e){return ''}})(),"
    "actorInfo: (function(){try{"
    "var bm2=(typeof BattleManager!=='undefined')?BattleManager:null;"
    "if(bm2&&bm2._actor&&bm2._actor.name)return (typeof bm2._actor.name==='function')?bm2._actor.name():String(bm2._actor.name);"
    "if(bm2&&bm2._actorIndex!==undefined&&bm2._actorIndex!==null&&typeof $gameParty!=='undefined'&&$gameParty.members){"
    "var a=$gameParty.members()[bm2._actorIndex];"
    "if(a&&a.name)return (typeof a.name==='function')?a.name():String(a.name);}"
    "return '';}catch(e){return ''}})(),"
    "actorCommands: (function(){try{"
    "var sc=(typeof SceneManager!=='undefined'&&SceneManager._scene)?SceneManager._scene:null;"
    "var w=sc&&sc._actorCommandWindow?sc._actorCommandWindow:null;"
    "if(!w||typeof w.commands!=='function')return '';"
    "var cmds=w.commands();"
    "return (cmds&&cmds.length)?cmds.join('、'):'';"
    "}catch(e){return ''}})(),"
    "skillList: (function(){try{"
    "var sw=(typeof SceneManager!=='undefined'&&SceneManager._scene&&SceneManager._scene._skillWindow)?SceneManager._scene._skillWindow:null;"
    "var ac=sw&&sw._actor?sw._actor:null;"
    "if(!ac||typeof ac.skills!=='function')return '';"
    "var names=ac.skills().map(function(s){"
    "var n=s.name||'';var mp=(typeof s.mpCost==='number'&&s.mpCost>0)?s.mpCost:0;"
    "var tp=(typeof s.tpCost==='number'&&s.tpCost>0)?s.tpCost:0;"
    "var cost='';if(mp>0)cost+=mp+'mp';if(tp>0)cost+=(cost?'+':'')+tp+'tp';"
    "return n+(cost?':'+cost:'');});"
    "return names.slice(0,8).join('、');"
    "}catch(e){return ''}})(),"
    "skillCurrent: (function(){try{"
    "var sw2=(typeof SceneManager!=='undefined'&&SceneManager._scene&&SceneManager._scene._skillWindow)?SceneManager._scene._skillWindow:null;"
    "var it=sw2&&typeof sw2.item==='function'?sw2.item():null;"
    "if(!it||!it.name)return '';"
    "var d=it.description?String(it.description).split(String.fromCharCode(10)).join(' '):'';"
    "return it.name+(d?'：'+d.slice(0,60):'');"
    "}catch(e){return ''}})(),"
    "menuCommands: (function(){try{"
    "var sc=(typeof SceneManager!=='undefined'&&SceneManager._scene)?SceneManager._scene:null;"
    "var w=sc&&sc._commandWindow&&sc._commandWindow._list?sc._commandWindow:null;"
    "if(!w)return '';"
    "var names=w._list.map(function(c){return c.name?c.name:(c.symbol?c.symbol:'');});"
    "return names.slice(0,8).join('、');"
    "}catch(e){return ''}})(),"
    "menuCurrent: (function(){try{"
    "var sc2=(typeof SceneManager!=='undefined'&&SceneManager._scene)?SceneManager._scene:null;"
    "var w2=sc2&&sc2._commandWindow&&sc2._commandWindow._list?sc2._commandWindow:null;"
    "if(!w2||typeof w2.index!=='function')return '';"
    "var c=w2._list[w2.index()];"
    "return c?(c.name?c.name:(c.symbol?c.symbol:'')):'';"
    "}catch(e){return ''}})(),"
    "listCurrent: (function(){try{"
    "var sc3=(typeof SceneManager!=='undefined'&&SceneManager._scene)?SceneManager._scene:null;"
    "if(!sc3)return '';"
    "var keys=Object.keys(sc3).filter(function(k){return k.indexOf('Window')>=0&&k!=='_commandWindow';});"
    "for(var i=0;i<keys.length;i++){var w=sc3[keys[i]];"
    "if(w&&typeof w.item==='function'&&typeof w.index==='function'){"
    "var it=w.item();if(it&&it.name)return (typeof it.name==='function'?it.name():String(it.name));}}"
    "return '';}catch(e){return ''}})(),"
    "helpText: (function(){try{"
    "var sc4=(typeof SceneManager!=='undefined'&&SceneManager._scene)?SceneManager._scene:null;"
    "var hw=sc4&&sc4._helpWindow?sc4._helpWindow:null;"
    "if(!hw)return '';"
    "var t=(hw._text!==undefined&&hw._text!==null)?String(hw._text):"
    "(typeof hw.text==='function'?String(hw.text()||''):'');"
    "var segs=String(t).split('\\V[');if(segs.length>1){"
    "var o=segs[0];for(var i=1;i<segs.length;i++){"
    "var s=segs[i];var m=/^(\\d+)\\]/.exec(s);"
    "if(m){var val='';try{val=(typeof $gameVariables!=='undefined'&&"
    "$gameVariables.value)?String($gameVariables.value(Number(m[1]))):'';}catch(e){val='';}"
    "o+=val+s.slice(m[0].length);}else{o+='\\V['+s;}}t=o;}"
    "return String(t).split(String.fromCharCode(10)).join(' ').slice(0,120);"
    "}catch(e){return ''}})()"
    "})"
)


# ---------------------------------------------------------------------------
# 端口分配
# ---------------------------------------------------------------------------

def port_for(slug: str) -> int:
    """按 slug 确定性分配调试端口（10240-60239），跨进程稳定。

    内置 hash() 受 PYTHONHASHSEED 影响（每次进程随机），会导致
    start_game 与 monitor 守护线程端口不一致；改用 md5 确定性哈希。
    """
    import hashlib
    h = int(hashlib.md5(slug.encode("utf-8")).hexdigest()[:8], 16)
    return PORT_BASE + (h % PORT_SPAN)


# ---------------------------------------------------------------------------
# 启动注入
# ---------------------------------------------------------------------------

def _cdp_alive(port: int) -> bool:
    from .cdp import cdp_version
    return cdp_version(port) is not None


def start_game(game: GameInfo, port: int = None,
               dry_run: bool = False) -> dict:
    """以 CDP 调试端口启动游戏（自动降级：正常启动 → 探测 → 旁路）。

    启动方式（GameInfo.launch_mode，auto 首次探测后记忆到 games.json）：
    - normal：Game.exe 带调试参数直接启动；
    - bypass：用 nwjs SDK 的 nw.exe 旁路运行游戏目录（绕过剥离参数的
      启动器，如 MTool 处理的游戏）—— CDP 注入同一端口；
    - auto：先试 normal，探测窗口内 CDP 未开放则终止并切换 bypass。

    返回：{"state": "running"|"would_start"|"started", "port", "pid", "mode"}。
    """
    port = port or port_for(game.slug)
    cdp_enabled = settings.app_get("rmgame_cdp_enabled", True)
    if not cdp_enabled:
        # CDP 开关关闭：普通启动，实时读取走 OCR
        cmd = [game.exe_path]
        if dry_run:
            return {"ok": True, "state": "would_start", "port": None, "cmd": cmd,
                    "mode": "normal"}
        try:
            proc = subprocess.Popen(cmd, cwd=game.dir)
        except OSError as exc:
            return {"ok": False, "error": f"启动失败: {exc}"}
        return {"ok": True, "state": "started", "port": None, "pid": proc.pid,
                "mode": "normal"}
    if _cdp_alive(port):
        return {"ok": True, "state": "running", "port": port, "pid": None}
    mode = getattr(game, "launch_mode", "auto") or "auto"  # None/缺字段 → auto 探测
    if mode == "bypass":
        # 记忆为旁路：直接旁路（无需再探测）
        res = _launch_bypass(game, port, dry_run=dry_run)
        if res is not None:
            return res
        # SDK 不可用 → 退回普通启动
        mode = "normal"
    cmd = [game.exe_path, f"--remote-debugging-port={port}"]
    if dry_run:
        return {"ok": True, "state": "would_start", "port": port, "cmd": cmd,
                "mode": mode}
    try:
        proc = subprocess.Popen(cmd, cwd=game.dir)
    except OSError as exc:
        return {"ok": False, "error": f"启动失败: {exc}"}
    if mode == "auto":
        # 首次：探测窗口内 CDP 未开放 → 判定启动器剥离参数 → 旁路降级
        if _wait_cdp(port, timeout=8.0):
            _set_launch_mode(game, "normal")
            return {"ok": True, "state": "started", "port": port, "pid": proc.pid,
                    "mode": "normal"}
        try:
            proc.terminate()
        except OSError:
            pass
        _set_launch_mode(game, "bypass")
        res = _launch_bypass(game, port)
        if res is not None:
            return res
    return {"ok": True, "state": "started", "port": port, "pid": proc.pid,
            "mode": mode}


def _nwjs_sdk_exe() -> str or None:
    """定位 nwjs SDK 的 nw.exe：app.ini 的 rmgame_nwjs_sdk 指定优先，否则探测
    %TEMP%\\nwjs-sdk-*。"""
    cfg = str(settings.app_get("rmgame_nwjs_sdk") or "").strip()
    if cfg and Path(cfg).exists():
        return cfg
    try:
        tmp = Path.home() / "AppData" / "Local" / "Temp"
        for d in sorted(tmp.glob("nwjs-sdk-*"), reverse=True):
            exe = d / "nw.exe"
            if exe.exists():
                return str(exe)
    except OSError:
        pass
    return None


def _wait_cdp(port: int, timeout: float = 8.0) -> bool:
    """轮询等待端口 CDP 可用（最多 timeout 秒）。"""
    end = time.time() + timeout
    while time.time() < end:
        if _cdp_alive(port):
            return True
        time.sleep(0.5)
    return False


def _launch_bypass(game, port: int, dry_run: bool = False):
    """用 nwjs SDK 旁路运行游戏目录（绕过剥离参数的 Game.exe）。

    返回 None 表示 SDK 不可用（调用方退回普通启动）。
    """
    nw = _nwjs_sdk_exe()
    if nw is None:
        return None
    cmd = [nw, game.dir, f"--remote-debugging-port={port}"]
    if dry_run:
        return {"ok": True, "state": "would_start", "port": port, "cmd": cmd,
                "mode": "bypass"}
    try:
        proc = subprocess.Popen(cmd, cwd=game.dir)
    except OSError as exc:
        return {"ok": False, "error": f"旁路启动失败: {exc}"}
    return {"ok": True, "state": "started", "port": port, "pid": proc.pid,
            "mode": "bypass"}


def _set_launch_mode(game, mode: str) -> None:
    """记忆启动方式到 games.json（auto 探测结果）。"""
    try:
        from .discovery import load_games, save_games
        games = load_games()
        for g in games:
            if g.slug == game.slug:
                g.launch_mode = mode
        save_games(games)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 状态读取与快照
# ---------------------------------------------------------------------------

def _parse_state(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
        if isinstance(data, str):
            data = json.loads(data)  # 解一层：value 为 JSON 字符串（_STATE_EXPR 已 stringify）
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def read_state(game: GameInfo, port: int = None, evaluator=None,
               ocr_fn=None, ocr_only: bool = False) -> dict:
    """读取游戏内部状态；CDP 失败时降级 OCR（source 标注 cdp/ocr）。

    evaluator：callable(expr) -> str（CDP 返回 JSON 字符串），离线自测注入；
    ocr_fn：callable(game) -> str（OCR 文本），离线自测注入；
    均为 None 时走真实链路：cdp.py 优先，失败自动降级 ocr.py；
    ocr_only=True 跳过 CDP 直接走 OCR（调试用）。
    """
    port = port or port_for(game.slug)
    # CDP 开关关闭（游戏拒绝调试参数时）→ 跳过 CDP 直接 OCR
    cdp_enabled = settings.app_get("rmgame_cdp_enabled", True)
    if not ocr_only and cdp_enabled:
        try:
            if evaluator is not None:
                raw = evaluator(_STATE_EXPR)
            else:
                from .cdp import cdp_evaluate, cdp_page_url
                ws_url = cdp_page_url(port)
                raw = cdp_evaluate(ws_url, _STATE_EXPR)
            data = _parse_state(raw)
            data["_source"] = "cdp"
            return data
        except Exception:
            pass  # CDP 不可用 → 降级到 OCR
    # CDP 失败或 ocr_only → OCR 兜底（只取文本；地图/场景未知）
    text = ""
    if ocr_fn is not None:
        try:
            text = ocr_fn(game) or ""
        except Exception:
            text = ""
    else:
        try:
            from .ocr import ocr_game_text
            text = ocr_game_text(game) or ""
        except Exception:
            text = ""
    return {"_source": "ocr", "text": text}


def build_snapshot(game: GameInfo, port: int = None, evaluator=None,
                   ocr_fn=None, now: str = None, ocr_only: bool = False) -> dict:
    """构建语义化快照（current.json 内容）。

    OCR 来源时对文本做 raw 模糊匹配，附加精确原文（matched_text/id/score），
    供环境段/点评引用精确台词（OCR 文本有噪声，不直接展示）。
    """
    data = read_state(game, port=port, evaluator=evaluator, ocr_fn=ocr_fn,
                      ocr_only=ocr_only)
    source = data.pop("_source", "cdp")
    now_s = now or _dt.datetime.now().isoformat(timespec="seconds")
    snap = {
        "game": game.slug,
        "dir": game.dir,
        "map_id": data.get("mapId"),
        "map_name": data.get("mapName") or "",
        "scene": data.get("scene") or "",
        "text": data.get("text") or "",
        "battle_troop": data.get("battleTroop") or "",   # 战斗：敌方部队（实际成员，非战斗空）
        "battle_phase": data.get("battlePhase") or "",   # 战斗阶段（init/start/input/battle/end）
        "party_info": data.get("partyInfo") or "",       # 玩家成员（名字(HP/最大HP)）
        "actor_info": data.get("actorInfo") or "",       # 当前行动者
        "actor_commands": data.get("actorCommands") or "",  # 可用行动（指令窗口）
        "skill_list": data.get("skillList") or "",          # 技能表（技能窗口行动者的技能名）
        "skill_current": data.get("skillCurrent") or "",    # 当前选中技能（名+描述截断）
        "menu_commands": data.get("menuCommands") or "",    # 菜单命令列表（Scene_Menu）
        "menu_current": data.get("menuCurrent") or "",      # 当前选中菜单命令
        "list_current": data.get("listCurrent") or "",      # 通用：当前选中列表项（任意场景）
        "help_text": data.get("helpText") or "",            # 通用：帮助窗口文本（可选）
        "updated_at": now_s,   # 内容更新时间（相同文本时保留旧值）
        "read_at": now_s,      # 最后成功读取时间（心跳：画面静止也保持新鲜）
        "source": source,
    }
    # 匹配 raw 精确条目 → 事件上下文/摘要（CDP 文本精确、OCR 噪声均可匹配）
    if snap["text"]:
        try:
            from .matcher import match_text
            m = match_text(snap["text"], game.slug)
        except Exception:
            m = []
        if m:
            snap["matched_text"] = m[0]["text"]
            snap["match_id"] = m[0]["id"]
            snap["match_score"] = m[0]["score"]
            ctx = m[0].get("event_context") or []
            if ctx:
                snap["event_context"] = _fmt_event_context(ctx)
                # 事件标识（Map001.40）+ 摘要缓存（若有）
                mid = m[0].get("id", "")
                page = m[0].get("page")
                from .matcher import event_key as _event_key
                ev_key = _event_key(mid, page) if mid else ""
                if ev_key:
                    snap["match_event"] = ev_key   # 含页面（Map.Ev.pN）
                    snap["match_page"] = page
                    try:
                        from .summarizer import load_summary
                        snap["event_summary"] = load_summary(game.slug, ev_key)
                    except Exception:
                        snap["event_summary"] = None
    return snap


def _fmt_event_context(ctx: list, max_chars: int = 1500) -> str:
    """事件上下文列表 → 紧凑文本（- [id] 说话人：文本，按序，截断）。"""
    from .llmfmt import is_noise_speaker
    lines = []
    total = 0
    for e in ctx:
        spk = (e.get("speaker") or "").strip()
        if is_noise_speaker(spk):
            spk = ""   # 占位事件名（EV001 等）不显示
        seg = (spk + "：" if spk else "") \
            + (e.get("text") or "").replace("\n", " ")
        if total + len(seg) > max_chars:
            lines.append(f"…（事件共 {len(ctx)} 段，以下截断）")
            break
        lines.append(f"- [{e.get('id', '')}] {seg}")
        total += len(seg)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# current.json 读写与轮询
# ---------------------------------------------------------------------------

def load_current() -> dict or None:
    """读 runtime/current.json；不存在或损坏返回 None。"""
    if not CURRENT_FILE.exists():
        return None
    try:
        d = json.loads(CURRENT_FILE.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def write_current(snapshot: dict) -> Path:
    """原子写 runtime/current.json。"""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CURRENT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(CURRENT_FILE)
    return CURRENT_FILE


def monitor_loop(game: GameInfo, port: int = None, interval: float = 1.0,
                 max_rounds: int = None, evaluator=None, ocr_fn=None,
                 ocr_only: bool = False) -> int:
    """轮询守护：读状态 → 写快照；文本变化时刷新 updated_at。

    evaluator / ocr_fn 可注入（离线自测）；均 None 时真实链路
    （CDP 优先，失败自动降级 OCR）；ocr_only=True 跳过 CDP。
    返回实际轮询次数。max_rounds 用于测试/单次调试；None 无限轮询。
    """
    port = port or port_for(game.slug)
    prev = load_current()
    prev_text = prev.get("text") if prev else None
    rounds = 0
    while max_rounds is None or rounds < max_rounds:
        snap = build_snapshot(game, port=port, evaluator=evaluator,
                              ocr_fn=ocr_fn, ocr_only=ocr_only)
        if prev_text is not None and snap["text"] == prev_text:
            # 无新对话：保留上次 updated_at（它表示"最后新内容时间"）
            last = load_current()
            if last:
                snap["updated_at"] = last.get("updated_at", snap["updated_at"])
        else:
            snap["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        write_current(snap)
        prev_text = snap["text"]
        rounds += 1
        if max_rounds is not None and rounds >= max_rounds:
            break
        time.sleep(max(0.1, float(interval)))
    return rounds


# 运行端口表刷新间隔（轮）：每 N 轮枚举一次进程（PowerShell 调用较重）
PORT_REFRESH_ROUNDS = 30
# 读取失败降频重试轮数（未运行游戏不再每轮无效尝试；30 轮 ≈ 60 秒重试一次）
FAIL_RETRY_ROUNDS = 30


# ---------------------------------------------------------------------------
# 运行中进程枚举（自动发现基础）
# ---------------------------------------------------------------------------

def _enum_processes(enum_fn=None) -> list:
    """枚举 Game.exe 进程 → [(exe_path, command_line, pid)]。

    默认 PowerShell Get-CimInstance（按进程名过滤）；enum_fn 可注入
    （离线自测 mock），签名 () -> [(exe, cmdline, pid)]。
    """
    if enum_fn is not None:
        return list(enum_fn() or [])
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='Game.exe'\" | "
             "ForEach-Object { $_.ExecutablePath + '|' + $_.CommandLine + '|' + $_.ProcessId }"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
    except Exception:
        return []
    rows = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        exe = (parts[0] if parts else "").strip()
        if not exe:
            continue
        cmd = parts[1] if len(parts) >= 2 else ""
        pid = parts[2] if len(parts) >= 3 else ""
        rows.append((exe, cmd, pid))
    return rows


def _parse_ports(rows: list) -> dict:
    """从进程行解析调试端口：{exe_path_lower: port}。

    游戏可能以任意端口启动（手动启动、历史会话残留、或被 MTool 等包装），
    不能只信 port_for 的默认端口；CommandLine 中的
    `--remote-debugging-port=N` 兼容空格/等号两种写法。
    """
    ports = {}
    for exe, cmd, _pid in rows:
        m = re.search(r"--remote-debugging-port[= ](\d+)", cmd or "")
        if m and exe:
            ports[exe.lower()] = int(m.group(1))
    return ports


def discover_running_ports() -> dict:
    """枚举运行中的 Game.exe 进程，解析实际调试端口。

    返回 {exe_path_lower: port}（全部带调试端口的 Game.exe，不限于已识别游戏）。
    """
    return _parse_ports(_enum_processes())


def enumerate_running(enum_fn=None):
    """枚举运行中的 Game.exe 进程并识别为游戏。

    返回 (running_games: list[GameInfo], ports: {exe_path_lower: port})。
    仅返回引擎判定通过的进程（discover_dir）；端口表覆盖全部带调试端口的进程。
    enum_fn 可注入（离线自测 mock），签名 () -> [(exe, cmdline, pid)]。
    """
    from .discovery import discover_dir
    rows = _enum_processes(enum_fn)
    ports = _parse_ports(rows)
    games, seen = [], set()
    for exe, _cmd, _pid in rows:
        exe = (exe or "").strip()
        if not exe or exe.lower() in seen:
            continue
        seen.add(exe.lower())
        info = discover_dir(exe)
        if info is not None:
            games.append(info)
    return games, ports


def monitor_loop_all(games, interval: float = 2.0, stop_event=None,
                     max_rounds: int = None, enum_fn=None,
                     on_auto_register=None) -> int:
    """守护：轮询所有已注册游戏，把能读到的游戏状态写入 current.json。

    每个游戏尝试 build_snapshot（CDP → OCR 兜底）；无实际内容（游戏未
    运行/画面无文字）则不覆盖旧快照。current.json 单文件语义：保留最后
    成功读取的快照。stop_event 用于守护线程停止；max_rounds 用于测试。

    端口策略：每 PORT_REFRESH_ROUNDS 轮刷新一次"运行中进程端口表"
    （enumerate_running），用实际调试端口连接；进程表中无该游戏时
    回退 port_for 默认端口。

    自动发现（app.ini 的 rmgame_auto_discover，settings.app_get 热读）：同一轮枚举中识别出
    运行中的未注册 RPG Maker 游戏 → auto_register 自动入库（trust=auto，
    只读能力，禁启动）；轮询列表每轮动态刷新（load_games），新入库游戏
    即时纳入监控。首次自动入库的游戏经 on_auto_register 回调通知
    （守护线程内调用；调用方负责线程安全调度，如 tkinter 的 root.after）。

    节流（1.0，T1 加固）：读取失败的游戏降频重试 —— 连续失败后每
    FAIL_RETRY_ROUNDS 轮才重试一次（未运行游戏不再每轮无效 CDP/OCR
    尝试），避免守护线程高频空转；游戏启动后首轮正常读（retry 表为空）。

    enum_fn：进程枚举注入（离线自测 mock），签名 () -> [(exe, cmdline, pid)]。
    on_auto_register：首次自动入库回调（GameInfo），幂等（已入库不触发）。
    """
    rounds = 0
    ports = {}
    prev_events = {}   # slug -> 上次事件标识（摘要生成去重）
    fail_retry = {}    # slug -> 距下次重试的剩余轮数（读取失败降频）
    auto_discover = bool(settings.app_get("rmgame_auto_discover", True))
    from .discovery import load_games
    while (max_rounds is None or rounds < max_rounds) \
            and not (stop_event is not None and stop_event.is_set()):
        if rounds % PORT_REFRESH_ROUNDS == 0:
            running, ports = enumerate_running(enum_fn)
            if auto_discover:
                from .discovery import auto_register as _auto_reg
                from .discovery import load_games as _load_games
                known = {g.slug for g in _load_games()}
                for info in running:
                    if info.slug not in known and _auto_reg(info):
                        known.add(info.slug)  # 本轮内避免重复尝试
                        if on_auto_register is not None:
                            try:
                                on_auto_register(info)
                            except Exception:
                                pass  # 回调异常不影响守护循环
        games = load_games()
        for g in games:
            if fail_retry.get(g.slug, 0) > 0:
                fail_retry[g.slug] -= 1
                continue
            port = ports.get(g.exe_path.lower()) or port_for(g.slug)
            try:
                snap = build_snapshot(g, port=port)
            except Exception:
                fail_retry[g.slug] = FAIL_RETRY_ROUNDS
                continue
            if not (snap.get("text") or snap.get("map_id") is not None
                    or snap.get("scene")):
                fail_retry[g.slug] = FAIL_RETRY_ROUNDS
                continue  # 无实际内容 → 不覆盖旧快照，降频重试
            fail_retry.pop(g.slug, None)
            # 新对话检测：文本变化才刷新 updated_at
            prev = load_current()
            if prev and snap["text"] == prev.get("text"):
                snap["updated_at"] = prev.get("updated_at", snap["updated_at"])
            else:
                snap["updated_at"] = _dt.datetime.now().isoformat(
                    timespec="seconds")
            write_current(snap)
            # 事件记录（摘要改为"角色需要回应前"按需生成，见 pet._request_llm）
            ev_key = snap.get("match_event") or ""
            if ev_key and ev_key != prev_events.get(g.slug):
                prev_events[g.slug] = ev_key
        rounds += 1
        if max_rounds is not None and rounds >= max_rounds:
            break
        time.sleep(max(0.5, float(interval)))
    return rounds
