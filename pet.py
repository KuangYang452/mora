# -*- coding: utf-8 -*-
"""桌宠角色 —— LLM 驱动的桌面宠物

- 透明置顶无边框窗口，显示角色立绘（character/<slug>/sprite.png）
- 左键拖拽移动，单击/双击弹出聊天输入框
- 气泡对话框显示角色的回复（语音色来自 profile [voice] speech_color），打字机效果
- LLM 每回合输出原生工具调用（say / update_state / 查询工具），本模块执行
- 呼吸浮动动画、右键菜单、可选主动闲聊
- 角色数据在 character/<slug>/，运行配置在 setting/*.ini，对话历史经 persist 落盘

运行：python pet.py
"""

import json
import math
import random
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont

from PIL import Image, ImageTk

import settings
import character as character_mod
from context import ContextManager
import logutil
import persist
import tools

# 运行配置（setting/app.ini）与用户指称（setting/user.ini）快照：
# 进程启动时从唯一配置入口读取一次，运行期不变。
CONFIG = settings.app_config()
USER_REFERENCE = settings.user_ref()
from llm import (
    ChatError,
    _build_tools,
    apply_state,
    build_activation,
    build_system_prompt,
    call_llm,
    extract_tool_calls,
    get_character,
    has_query_intent,
    parse_llm_response,
    summarize_history,
    thinking_style_for,
    tool_result_text,
)
from rmgame.bridge import execute_tool
from rmgame import facade as rmgame_facade
from notes import execute as notes_execute

KEY_COLOR = "#ff00ff"  # 透明键色（Windows 的 -transparentcolor）
BUBBLE_BG = "#fffaf2"  # 气泡底色（暖白）
BUBBLE_BORDER = "#c9d8c9"
ERR_COLOR = "#c0392b"
BUBBLE_MAX_W = 340      # 气泡最大像素宽

# 查询类工具（意向-动作校验判定：说了要查但没调这些 → 触发重试）
# 与游戏世界类工具（<game_data> 包裹回传）均派生自 tools.SPECS 注册表
# （唯一来源，见 tools.query_names / tools.game_world_names）。
_QUERY_TOOL_NAMES = tools.query_names(
    rmgame_enabled=CONFIG.get("rmgame_enabled", True))
_GAME_TOOL_NAMES = tools.game_world_names(
    rmgame_enabled=CONFIG.get("rmgame_enabled", True))

# 上次对话距今多久的语义化描述；过近（<60 秒）或无法解析返回 None
# ---------------------------------------------------------------------------

def idle_activation_prompt(env: dict or None) -> str:
    """闲置激活词：按是否有新鲜游戏环境分流。

    对方在玩游戏（env 含 game）→ 引导角色结合当前游戏画面与进度主动
    点评/调侃（贴合场景）；否则维持原泛问候/撒娇/戏弄引导。
    """
    gname = (env or {}).get("game_name") or (env or {}).get("game") or ""
    if gname:
        return (f"（现在没有人在跟你说话，但对方正在玩《{gname}》。"
                "你可以结合当前游戏画面与进度说一句贴合场景的话——"
                "点评、调侃或关心对方的游戏表现都行，让对方注意到你；"
                "通过 say 工具说出台词。）")
    return ("（现在没有人在跟你说话，你可以随意说一句问候、"
            "撒娇或戏弄对方的话，让对方注意到你；"
            "通过 say 工具说出台词。）")


def _resume_gap_text(saved_at) -> str or None:
    """上次对话距今多久的语义化描述；过近（<60 秒）或无法解析返回 None。"""
    from datetime import datetime
    try:
        gap = datetime.now() - datetime.fromisoformat(str(saved_at or ""))
    except (ValueError, TypeError):
        return None
    secs = gap.total_seconds()
    if secs < 60:
        return None
    mins = int(secs // 60)
    if mins < 60:
        return f"距上次对话 {mins} 分钟"
    hours = int(mins // 60)
    if hours < 24:
        return f"距上次对话 {hours} 小时 {mins % 60} 分"
    days = int(hours // 24)
    return f"距上次对话 {days} 天"


# emote 工具 → 气泡前缀表情（硬编码映射，LLM 只传情绪名称）
EMOTE_MARKS = {
    "害羞": "😳",
    "得意": "😏",
    "生气": "😠",
    "愉悦": "😊",
    "戏谑": "😼",
    "撒娇": "🥺",
}


class MoraPet:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.char = character_mod.current()   # 当前激活角色（character/<slug>/）
        self.scale = float(CONFIG.get("scale", 0.6))
        self.speech_color = self.char.speech_color

        self.card = get_character()      # 角色卡（card.json，不依赖原型）
        opening = self.char.opening      # 开场台词流（identity.json 契约，见 SCHEMA.md）
        # 持久化恢复：有存档 → 恢复状态（只取已知键）与对话历史（含 time）；
        # 无存档 → 初始状态 + 开场白台词作为上下文文本流的起始
        saved = persist.load_session()
        keep_recent = int(CONFIG.get("context_keep_recent", 10))
        keep_mid = int(CONFIG.get("context_keep_mid", 20))
        initial_state = self.char.initial_state()
        if saved:
            st = saved.get("state") or {}
            self.state = dict(initial_state)
            self.state.update({k: v for k, v in st.items() if k in initial_state})
            self.ctx = ContextManager(
                rounds=int(CONFIG.get("history_rounds", 30)),
                keep_recent=keep_recent, keep_mid=keep_mid)
            self.ctx.history = [
                m for m in saved.get("history") or []
                if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
            self.ctx.merge = saved.get("merge")            # 恢复合并条目
            self.ctx.archives = list(saved.get("archives") or [])  # 恢复归档
            self._resume_gap = _resume_gap_text(saved.get("saved_at"))
        else:
            self.state = dict(initial_state)
            self.ctx = ContextManager(
                rounds=int(CONFIG.get("history_rounds", 30)), opening=opening,
                keep_recent=keep_recent, keep_mid=keep_mid)
            self._resume_gap = None
        self.sys_prompt = build_system_prompt(self.card, state=self.state)

        self._sprite_img = None
        self._tk_img = None
        self._bubble = None
        self._bubble_queue = []       # 待显示气泡（FIFO，完整生命周期调度）
        self._bubble_phase = None     # 当前气泡生命周期：None|typing|hold|fade_out|waiting
        self._bubble_after = None     # 气泡后台链的 after id（打字/停留/淡出，统一可取消）
        self._bubble_timer = None     # 气泡停留计时（自动淡出前）的 after id
        self._tw_state = {}           # 打字机状态（lines/items/li/ci）
        self._last_bubble_text = ""   # 当前气泡文本（供停留时长计算）
        self._approve_win = None      # 游戏确认气泡（Toplevel，独立于台词气泡）
        self._approve_canvas = None
        self._approve_slug = None
        self._approve_ok_rect = None    # 确认按钮区域 (x1,y1,x2,y2)
        self._approve_later_rect = None # 稍后按钮区域
        self._input_win = None
        self._busy = False
        self._drag_off = (0, 0)
        self._click_job = None
        self._last_messages = []      # 本回合实际发送给 LLM 的完整消息（供日志）
        self._bob_phase = random.uniform(0, math.tau)
        self._bob_base_y = None

        self._install_exception_hooks()
        self._setup_window()
        self._load_sprite()
        self._build_menu()
        self._place_sprite()
        self._idle_loop()
        self._schedule_auto_chat()
        # RPG Maker 监控守护线程（写 current.json 供环境段注入/工具读取）
        self._monitor_stop = threading.Event()
        self._monitor_thread = None
        self._maybe_start_monitor()
        # 启动问候：延迟到窗口就绪后调用 LLM 生成开场白
        if CONFIG.get("greet_on_start", True):
            self.root.after(1200, self._greet)

    def _install_exception_hooks(self):
        """异常留痕：Tkinter 回调 / 主线程 / 后台线程的未捕获异常，
        除控制台原样输出外，同时写入 log/tk_exception_*.txt，便于
        复现后离线排查（tkinter 默认只打 stderr、不落盘）。"""
        import sys
        import traceback as _tb
        log_dir = Path(__file__).resolve().parent / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._exc_seq = 0

        def _dump(kind, exc_type, exc_value, exc_tb):
            _tb.print_exception(exc_type, exc_value, exc_tb)   # 保持控制台可见
            try:
                self._exc_seq += 1
                stamp = time.strftime("%Y%m%d_%H%M%S")
                path = log_dir / f"tk_exception_{stamp}_{kind}_{self._exc_seq}.txt"
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"kind={kind} time={stamp}\n")
                    f.write("".join(_tb.format_exception(exc_type, exc_value, exc_tb)))
            except Exception:
                pass    # 留痕本身失败不影响程序

        # Tkinter 事件回调（bind / after / command 抛出的未捕获异常）
        self.root.report_callback_exception = (
            lambda t, v, tb: _dump("tk_callback", t, v, tb))
        # 主线程顶层异常（mainloop 之外）
        sys.excepthook = lambda t, v, tb: _dump("main", t, v, tb)
        # 后台线程未捕获异常
        threading.excepthook = lambda args: _dump(
            "thread", args.exc_type, args.exc_value, args.exc_tb)

    def _maybe_start_monitor(self):
        """按配置拉起 rmgame 监控守护线程（多游戏轮询写 current.json）。

        桌宠常驻期间持续跟踪已注册游戏的运行状态（CDP → OCR 兜底），
        让【对方正在…】环境段与 read_current_text 始终有数据可用。
        守护线程异常不影响桌宠本体。
        """
        if not CONFIG.get("rmgame_enabled", True) \
                or not CONFIG.get("monitor_auto_start", True):
            return
        try:
            games = rmgame_facade.games()
        except Exception:
            return
        if not games:
            return
        interval = float(CONFIG.get("rmgame_monitor_interval", 2.0))

        def _guard():
            try:
                rmgame_facade.monitor_loop(
                    games, interval=interval,
                    stop_event=self._monitor_stop,
                    on_auto_register=self._on_game_discovered)
            except Exception:
                pass

        self._monitor_thread = threading.Thread(target=_guard, daemon=True)
        self._monitor_thread.start()

    # ---------------------------------------------------------- 游戏确认（M6f）

    def _on_game_discovered(self, info):
        """monitor 守护线程回调：新游戏自动入库（trust=auto）→ 主线程弹确认气泡。

        回调在守护线程内调用，经 root.after 调度回主线程（tkinter 非线程安全）；
        仅首次自动入库触发（auto_register 幂等）。开关
        CONFIG["rmgame_confirm_bubble"] 关闭时只保留右键菜单入口。
        """
        slug = getattr(info, "slug", "")
        if not slug:
            return
        if not CONFIG.get("rmgame_confirm_bubble", True):
            return
        try:
            self.root.after(0, lambda: self._show_approve_bubble(slug))
        except Exception:
            pass  # 退出中 root 已销毁等情况：静默丢弃

    def _load_pending_games(self) -> dict:
        """库中全部 trust=auto 的待确认游戏：{slug: GameInfo}。

        状态单一来源（runtime/games.json）：菜单每次重建时实时读取，
        无需维护内存副本，approve 后自动消失。
        """
        try:
            return {g.slug: g for g in rmgame_facade.games()
                    if (getattr(g, "trust", "user") or "user") == "auto"}
        except Exception:
            return {}

    def _show_approve_bubble(self, slug: str):
        """弹游戏确认气泡：检测到新游戏（trust=auto）→ 允许启动 / 稍后再说。

        独立于台词气泡系统（不排队、不自动淡出，等用户点按）；
        拖拽时经 _sync_followers 跟随。仅主线程调用。
        """
        g = next((x for x in rmgame_facade.games() if x.slug == slug), None)
        if g is None or (getattr(g, "trust", "user") or "user") == "user":
            return  # 已确认/已不在库：不弹
        self._close_approve_bubble()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-transparentcolor", KEY_COLOR)
        win.configure(bg=KEY_COLOR)
        cv = tk.Canvas(win, bg=KEY_COLOR, highlightthickness=0)
        cv.pack()
        fnt = tkfont.Font(family="Microsoft YaHei UI", size=10)
        title = f"🎮 检测到新游戏《{g.name}》"
        sub = f"（{g.engine.upper()}）要允许我读取并启动它吗？"
        ok_txt, later_txt = "✅ 允许", "💤 稍后"
        pad = 12
        tw = max(fnt.measure(title), fnt.measure(sub)) + pad * 2
        bw = fnt.measure(ok_txt) + fnt.measure(later_txt) + pad * 3
        w = max(tw, bw) + 16
        h = 98
        cv.configure(width=w, height=h)
        self._rounded_rect(1, 1, w - 1, h - 1, 12, cv=cv,
                           fill="#fffaf2", outline=BUBBLE_BORDER, width=1)
        cv.create_text(pad, 12, anchor="nw", text=title, font=fnt, fill="#222222")
        cv.create_text(pad, 37, anchor="nw", text=sub, font=fnt, fill="#555555")
        # 「允许」按钮（绿色实心）
        ok_w = fnt.measure(ok_txt) + 16
        ok_x1, ok_y1, ok_x2, ok_y2 = pad, 64, pad + ok_w, 88
        self._rounded_rect(ok_x1, ok_y1, ok_x2, ok_y2, 10, cv=cv,
                           fill="#2e7d32", outline="")
        cv.create_text((ok_x1 + ok_x2) / 2, (ok_y1 + ok_y2) / 2, text=ok_txt,
                       font=fnt, fill="#ffffff")
        # 「稍后」按钮（灰色）
        lx1 = ok_x2 + 8
        lw = fnt.measure(later_txt) + 16
        self._rounded_rect(lx1, ok_y1, lx1 + lw, ok_y2, 10, cv=cv,
                           fill="#e0e0e0", outline="")
        cv.create_text(lx1 + lw / 2, (ok_y1 + ok_y2) / 2, text=later_txt,
                       font=fnt, fill="#333333")
        self._approve_win = win
        self._approve_canvas = cv
        self._approve_slug = slug
        self._approve_ok_rect = (ok_x1, ok_y1, ok_x2, ok_y2)
        self._approve_later_rect = (lx1, ok_y1, lx1 + lw, ok_y2)
        win.geometry(f"+{x + self.w + 10}+{y + max(0, self.h - 150)}")
        cv.bind("<Button-1>", self._on_approve_click)

    def _on_approve_click(self, event):
        """确认气泡点击：按命中区域分发（允许 / 稍后）。"""
        if self._approve_canvas is None:
            return
        ok = self._approve_ok_rect
        later = self._approve_later_rect
        if ok and ok[0] <= event.x <= ok[2] and ok[1] <= event.y <= ok[3]:
            self._do_approve(self._approve_slug)
        elif later and later[0] <= event.x <= later[2] \
                and later[1] <= event.y <= later[3]:
            self._close_approve_bubble()

    def _do_approve(self, slug: str):
        """UI 确认入口：升级 trust auto→user，并用台词气泡反馈结果。

        仅主线程调用（气泡点击 / 右键菜单均已在主线程）。
        """
        g, msg = rmgame_facade.approve_game(slug)
        self._close_approve_bubble()
        if g is not None and "已确认" in msg:
            self._bubble_text(f"✅ {msg}", fg="#2e7d32", bg="#e8f5e9")
        else:
            self._bubble_text(msg, fg="#c0392b", bg="#fdecea")

    def _close_approve_bubble(self):
        """关闭并清理确认气泡。"""
        if self._approve_win is not None and self._approve_win.winfo_exists():
            self._approve_win.destroy()
        self._approve_win = None
        self._approve_canvas = None
        self._approve_slug = None
        self._approve_ok_rect = None
        self._approve_later_rect = None

    # ------------------------------------------------------------------ 窗口

    def _setup_window(self):
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", KEY_COLOR)
        self.root.configure(bg=KEY_COLOR)

    def _load_sprite(self):
        path = self.char.sprite
        if not path.exists():
            raise FileNotFoundError(f"找不到立绘: {path}")
        img = Image.open(path).convert("RGBA")
        if self.scale != 1.0:
            img = img.resize(
                (max(1, round(img.width * self.scale)), max(1, round(img.height * self.scale))),
                Image.LANCZOS,
            )
        self._sprite_img = img
        keyed = Image.new("RGB", img.size, KEY_COLOR)
        alpha = img.getchannel("A")
        rgb = img.convert("RGB")
        keyed.paste(rgb, (0, 0), alpha)
        px = keyed.load()
        a = alpha.load()
        w, h = keyed.size
        for y in range(h):
            for x in range(w):
                if a[x, y] < 40:
                    px[x, y] = (255, 0, 255)
        self._tk_img = ImageTk.PhotoImage(keyed)
        self.w, self.h = img.size

    def _place_sprite(self):
        self.root.geometry(f"{self.w}x{self.h}")
        self.canvas = tk.Canvas(
            self.root, width=self.w, height=self.h,
            bg=KEY_COLOR, highlightthickness=0, bd=0,
        )
        self.canvas.pack()
        self.canvas.create_image(0, 0, image=self._tk_img, anchor="nw")
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_right_click)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"+{sw - self.w - 60}+{sh - self.h - 40}")
        self._bob_base_y = sh - self.h - 40

    # ------------------------------------------------------------------ 交互

    def _on_press(self, event):
        self._drag_off = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _on_drag(self, event):
        x = event.x_root - self._drag_off[0]
        y = event.y_root - self._drag_off[1]
        self.root.geometry(f"+{x}+{y}")
        self._bob_base_y = y
        self._sync_followers()

    def _on_release(self, event):
        if self._click_job is not None:
            self.root.after_cancel(self._click_job)
            self._click_job = None
            self._open_chat()
            return
        self._click_job = self.root.after(240, self._do_single_click)

    def _do_single_click(self):
        self._click_job = None
        self._open_chat()

    def _on_right_click(self, event):
        try:
            self._build_menu()   # 重建：游戏确认项随库状态实时刷新
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _sync_followers(self):
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if self._bubble is not None and self._bubble.winfo_exists():
            self._bubble.geometry(f"+{x + self.w + 10}+{y + max(0, self.h // 2 - 80)}")
        if self._approve_win is not None and self._approve_win.winfo_exists():
            self._approve_win.geometry(
                f"+{x + self.w + 10}+{y + max(0, self.h - 150)}")
        if self._input_win is not None and self._input_win.winfo_exists():
            self._input_win.geometry(f"+{x}+{y - 90}")

    # ------------------------------------------------------------------ 菜单

    def _build_menu(self):
        """构建右键菜单（每次右键弹出前重建，游戏确认项实时反映库状态）。"""
        if getattr(self, "_menu", None) is not None:
            try:
                self._menu.destroy()   # 旧菜单销毁，避免 Tk 资源累积
            except Exception:
                pass
        self._menu = tk.Menu(self.root, tearoff=0)
        self._menu.add_command(label="💬 聊天", command=self._open_chat)
        self._menu.add_command(label="❤️ 好感度", command=self._show_affection)
        game_menu = tk.Menu(self._menu, tearoff=0)
        pending = self._load_pending_games()
        if pending:
            for slug, g in pending.items():
                game_menu.add_command(
                    label=f"✅ 允许启动《{g.name}》",
                    command=lambda s=slug: self._do_approve(s))
        else:
            game_menu.add_command(label="无待确认游戏", state="disabled")
        self._menu.add_cascade(label="🎮 游戏", menu=game_menu)
        self._menu.add_separator()
        self._menu.add_command(label="退出", command=self._quit)

    def _show_affection(self):
        aff = int(self.state.get("affection", self.char.initial_state()["affection"]))
        lines = [
            f"好感度: {aff}/100（{self.char.level_for(aff)}）",
            f"内心想法: {self.state.get('inner_thought', '')}",
        ]
        self._bubble_text("　\n".join(lines), fg="#333333", bg="#f4efe6")

    # ------------------------------------------------------------------ 聊天

    def _open_chat(self):
        if self._input_win is not None and self._input_win.winfo_exists():
            self._input_win.lift()
            self._input_win.focus_force()
            self._input_entry.focus_set()
            return
        x, y = self.root.winfo_x(), self.root.winfo_y()
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-transparentcolor", KEY_COLOR)
        win.configure(bg=KEY_COLOR)
        win.geometry(f"+{x}+{y - 90}")
        box = tk.Frame(win, bg="#ffffff", bd=1, relief="solid", highlightthickness=2,
                       highlightbackground="#336633")
        box.pack(padx=0, pady=0, fill="both", expand=True)
        entry = tk.Entry(box, width=34, font=("Microsoft YaHei UI", 11), relief="flat",
                         fg="#222222", insertbackground="#336633")
        entry.pack(side="left", padx=8, pady=6, ipady=4)
        entry.bind("<Return>", lambda e: self._send(entry.get()))
        entry.bind("<Escape>", lambda e: self._close_chat())
        entry.focus_set()
        self._input_win = win
        self._input_entry = entry

    def _close_chat(self):
        if self._input_win is not None and self._input_win.winfo_exists():
            self._input_win.destroy()
        self._input_win = None
        self._input_entry = None

    def _send(self, text: str):
        text = text.strip()
        if not text or self._busy:
            return
        self._close_chat()
        self.ctx.add_user(text)
        self._busy = True
        self._decay_idle_interval()   # 对方有操作：间隔平滑减少（下限初始），避免突变打扰
        self._bubble_text("…", fg="#888888", bg="#f4f4f4")
        threading.Thread(target=self._request_llm, args=(), daemon=True).start()

    def _env_snapshot(self) -> dict or None:
        """读取游戏环境快照（新鲜度过滤）；无有效快照返回 None。

        数据源：rmgame monitor 写入的 runtime/current.json；
        阈值 CONFIG["rmgame_env_fresh_seconds"]（<=0 或开关关闭 = 不注入）。
        """
        try:
            fresh = float(CONFIG.get("rmgame_env_fresh_seconds", 300))
            if fresh <= 0 or not CONFIG.get("rmgame_enabled", True):
                return None
            cur = rmgame_facade.snapshot()
        except Exception:
            return None
        if not cur:
            return None
        # 新鲜度基于"最后成功读取"（read_at 心跳）：画面静止时也保持环境有效
        ts = cur.get("read_at") or cur.get("updated_at") or ""
        try:
            from datetime import datetime
            age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
        except (ValueError, TypeError):
            age = fresh  # 时间戳无法解析 → 视为过期
        if age > fresh:
            return None
        # game 为 slug（数据语义）；game_name 用于环境段展示（库中找不到时回退 slug）
        game_slug = cur.get("game", "")
        game_name = game_slug
        try:
            for g in rmgame_facade.games():
                if g.slug == game_slug and g.name:
                    game_name = g.name
                    break
        except Exception:
            pass
        return {
            "game": game_slug,
            "game_name": game_name,
            "map_id": cur.get("map_id"),
            "map_name": cur.get("map_name", ""),
            "scene": cur.get("scene", ""),
            "text": cur.get("text", ""),
            "battle_troop": cur.get("battle_troop") or "",
            "battle_phase": cur.get("battle_phase") or "",
            "party_info": cur.get("party_info") or "",
            "actor_info": cur.get("actor_info") or "",
            "actor_commands": cur.get("actor_commands") or "",
            "skill_list": cur.get("skill_list") or "",
            "skill_current": cur.get("skill_current") or "",
            "menu_commands": cur.get("menu_commands") or "",
            "menu_current": cur.get("menu_current") or "",
            "list_current": cur.get("list_current") or "",
            "help_text": cur.get("help_text") or "",
            "source": cur.get("source", "cdp"),   # cdp=精确文本；ocr=需 matched 才可信
            "matched_text": cur.get("matched_text"),
            "match_id": cur.get("match_id"),
            "match_score": cur.get("match_score"),
            "event_context": cur.get("event_context"),
            "event_summary": cur.get("event_summary"),
            "match_page": cur.get("match_page"),
        }

    def _ensure_event_summary(self, env):
        """摘要缓存预热：当前事件摘要缺失时**同步生成**，写回 current.json。

        摘要不进游戏环境段（全文概述含当前位置之后的内容，可能剧透，见
        llm 环境段注释），仅供 read_current_text 工具输出使用；在回合前
        预热缓存，避免角色查询时同步等待 LLM 生成。本回合去重
        （_summary_pending）：生成失败同回合不再重试、下回合重试；失败时
        current.json 无摘要，read_current_text 仅返回原文上下文。
        """
        if not env or not env.get("event_context") or env.get("event_summary"):
            return
        mid = (env.get("match_id") or "").strip()
        if not mid:
            return
        ev_key = rmgame_facade.ev_key(mid, env.get("match_page"))
        if not ev_key:
            return
        if ev_key in self._summary_pending:
            return
        self._summary_pending.add(ev_key)
        try:
            full = rmgame_facade.event_context(env["game"], mid)
            if full:
                text = rmgame_facade.event_summary(env["game"], ev_key, full)
                if text:
                    env["event_summary"] = text
                    try:
                        cur = rmgame_facade.snapshot()
                        if cur and cur.get("game") == env["game"]:
                            cur["event_summary"] = text
                            rmgame_facade.save_snapshot(cur)
                    except Exception:
                        pass
        except Exception:
            pass

    def _request_llm(self):
        """一次对方输入 → 最多 agent_max_turns 轮自主 LLM 调用。

        模型每轮可调用 update_state（状态即时结算并回传结果），直到不再
        调用工具（输出最终台词）或到达轮数上限。中间轮次若有台词会即时
        显示并累积，回合结束时与最终台词合并为一条 assistant 消息进历史
        （一次输入的多轮推进在上下文里只算一条，中间对话内容不丢弃）。
        所有 UI 操作经 root.after 调度回主线程（tkinter 非线程安全）。
        """
        max_turns = max(1, int(CONFIG.get("agent_max_turns", 4)))
        self._summary_pending = set()   # 本回合摘要触发去重（生成失败下回合重试）
        agent_msgs = []          # 本回合累积：assistant(tool_calls) + tool 结果
        interim_lines = []       # 中间轮台词（回合末与最终台词合并为一条进历史）
        last_parsed = None
        last_content = ""
        finished = False
        try:
            # 首轮前：历史超长则合并最旧部分（LLM 压缩 + 归档），
            # 控制上下文长度；失败保持现状（下次输入再试）
            self.ctx.ensure_merged(self._merge_history)
            for turn in range(1, max_turns + 1):
                retried_this_turn = False
                # 每轮重建 system prompt，使【当前状态】反映最新结算结果，
                # 并注入游戏环境快照（对方正在玩的 RPG Maker 游戏）
                env = self._env_snapshot()
                self._ensure_event_summary(env)   # 需要回应前：缺失则同步生成摘要（生成完才组装提示词）
                self.sys_prompt = build_system_prompt(
                    self.card, state=self.state, env=env,
                    agent_turn=(turn, max_turns))
                messages = self.ctx.build_messages(
                    self.sys_prompt,
                    activation=build_activation(self.state, self.card),
                    first_user_instr=thinking_style_for(self.state)
                ) + agent_msgs  # 倒数第一条：激活指令（含心理 COT <think> 块）
                self._last_messages = messages
                raw = call_llm(messages, tools=_build_tools(), kind="round",
                               retry=True, note="回合对话")
                parsed = parse_llm_response(raw)
                state_before = dict(self.state)
                self.state = apply_state(self.state, parsed)   # 状态结算（硬编码规则优先）
                self._run_tools(parsed)                        # 工具副作用（UI 动作入队）
                # 子轮日志：完整消息 + 原始响应 + 解析 + 状态变化
                logutil.log_round(messages, raw_reply=raw, parsed=parsed,
                                  state_before=state_before, state_after=self.state)
                last_parsed = parsed
                msg = raw["choices"][0]["message"]
                last_content = (msg.get("content") or "").strip()
                calls = extract_tool_calls(raw)
                # 本回合此前是否已调用过查询工具（agent_msgs 中 assistant 消息的
                # tool_calls 是否命中查询类）——已查过说明查询动机已兑现，
                # 后续收尾台词引用"翻书/查过"等是修辞总结而非新意图，不再拦截。
                queried_before = any(
                    m["role"] == "assistant"
                    and any(
                        t.get("function", {}).get("name") in _QUERY_TOOL_NAMES
                        for t in (m.get("tool_calls") or [])
                    )
                    for m in agent_msgs
                )
                # 意向-动作校验（防"说了要查却只演翻书"）：台词出现查询意向
                # 且本回合此前未查过、本子轮也未调任何查询工具 → 追加修复指令重试一次。
                # 防死循环：每子轮最多重试 1 次；重试结果无条件接受，不递归校验。
                if (CONFIG.get("retry_on_vague_query", False)
                        and not retried_this_turn
                        and (env or {}).get("game")
                        and not queried_before
                        and not any(c["name"] in _QUERY_TOOL_NAMES for c in calls)
                        and has_query_intent(parsed.reply)):
                    fix_msgs = messages + agent_msgs + [
                        {"role": "assistant", "content": msg.get("content") or ""},
                        {"role": "user",
                         "content": "（你刚才提到翻书/查询，但没有调用任何查询工具。"
                                    "请先调用 query_wiki / read_current_text 之一完成查询"
                                    "（read_raw_text 仅用于按条目 id 核对逐字原文），"
                                    "再基于结果作答；"
                                    "若确实无需查询，请直接调用 say 工具说出台词。）"}]
                    self._last_messages = fix_msgs
                    raw = call_llm(fix_msgs, tools=_build_tools(), kind="round",
                                   retry=True, note="回合对话（意向-动作校验重试）")
                    parsed = parse_llm_response(raw)
                    state_before = dict(self.state)
                    self.state = apply_state(self.state, parsed)
                    self._run_tools(parsed)
                    logutil.log_round(fix_msgs, raw_reply=raw, parsed=parsed,
                                      state_before=state_before, state_after=self.state)
                    last_parsed = parsed
                    msg = raw["choices"][0]["message"]
                    last_content = (msg.get("content") or "").strip()
                    calls = extract_tool_calls(raw)
                    retried_this_turn = True
                # 工具收尾校验（防"查完直接 content 直出"）：本回合已调过工具
                # （agent_msgs 非空）但本轮未调任何工具、直接 content 输出 →
                # 模型决定收尾却没走 say 通道，追加修复指令重试一次强制引导。
                # 首轮 content 直出（agent_msgs 为空）是合法降级路径，不重试；
                # 防死循环：每子轮最多重试 1 次，重试结果无条件接受，不递归校验。
                if (CONFIG.get("force_say_to_finish", False)
                        and not retried_this_turn
                        and agent_msgs
                        and not calls
                        and last_content):
                    fix_msgs = messages + agent_msgs + [
                        {"role": "assistant", "content": msg.get("content") or ""},
                        {"role": "user",
                         "content": "（你已调用工具推进工作，请调用 say 工具说出"
                                    "台词结束本回合；不要直接输出文本。）"}]
                    self._last_messages = fix_msgs
                    raw = call_llm(fix_msgs, tools=_build_tools(), kind="round",
                                   retry=True, note="回合对话（工具收尾校验重试）")
                    parsed = parse_llm_response(raw)
                    state_before = dict(self.state)
                    self.state = apply_state(self.state, parsed)
                    self._run_tools(parsed)
                    logutil.log_round(fix_msgs, raw_reply=raw, parsed=parsed,
                                      state_before=state_before, state_after=self.state)
                    last_parsed = parsed
                    msg = raw["choices"][0]["message"]
                    last_content = (msg.get("content") or "").strip()
                    calls = extract_tool_calls(raw)
                    retried_this_turn = True
                retried_this_turn = True
                if any(c["name"] == "say" for c in calls):
                    # say = 台词通道：本回合到此结束（状态已在 apply_state 结算）
                    finished = True
                    break
                if not calls:
                    # 模型未走工具（纯 content 降级路径）：以当前输出结束
                    finished = True
                    break
                if last_content:
                    # 中间轮台词：即时显示 + 累积（回合末与最终台词合并为一条
                    # 进历史，保留全部对话内容但不拆散上下文）
                    interim_lines.append(parsed.reply)
                    self.root.after(0, lambda p=parsed: self._show_interim(p))
                # 模型要求继续：回传 assistant(tool_calls) + 各工具执行结果
                agent_msgs.append({
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": msg.get("tool_calls"),
                })
                for c in calls:
                    agent_msgs.append({
                        "role": "tool",
                        "tool_call_id": c["id"],
                        "content": self._tool_result(c),
                    })
            self.root.after(0, lambda p=last_parsed, f=finished, c=last_content,
                            il=list(interim_lines):
                            self._finish_agent(p, not f, c, il))
        except ChatError as exc:
            self.root.after(0, lambda: self._show_error(str(exc)))
            logutil.log_round(self._last_messages, error=str(exc))
        except Exception as exc:
            self.root.after(0, lambda: self._show_error(f"发生错误: {exc}"))
            logutil.log_round(self._last_messages, error=f"{type(exc).__name__}: {exc}")

    def _show_interim(self, parsed):
        """UI 线程：显示中间轮台词（多轮推进中，非最终回复）。

        即时呈现（不用打字机）——下一轮台词会快速覆盖它，避免打字机
        动画与重建中的气泡冲突。
        """
        if parsed is None or not parsed.reply:
            return
        mark = EMOTE_MARKS.get(parsed.emote, "")
        self._request_bubble((mark + " " + parsed.reply).strip() if mark else parsed.reply,
                             fg=self.speech_color, bg=BUBBLE_BG, typewriter=False)

    def _finish_agent(self, parsed, truncated: bool, last_content: str,
                      interim_lines: list = None):
        """UI 线程：呈现本回合最终台词（达上限且无台词时给出截断提示）。

        历史写入：中间轮台词 + 最终台词合并为一条 assistant 消息
        （保留全部对话内容，一次输入的多轮推进在上下文里只算一条）。
        """
        if parsed is None:
            reply = self.card.instantiate(
                self.card.fallback("silent") or "（{{char_self}}只是安静地看着你）")
        elif truncated and not last_content:
            reply = self.card.instantiate(
                self.card.fallback("working") or "（{{char_self}}还在忙…下次再来听结果吧）")
        else:
            reply = parsed.reply
        merged = "\n".join([t for t in (interim_lines or []) if t] + [reply])
        self.ctx.add_assistant(merged)
        mark = EMOTE_MARKS.get(parsed.emote if parsed else "none", "")
        self._request_bubble((mark + " " + reply).strip() if mark else reply,
                             fg=self.speech_color, bg=BUBBLE_BG, typewriter=True)
        persist.save_session(self.state, self.ctx.history,
                             self.ctx.merge, self.ctx.archives)   # 回合结束落盘
        self._busy = False

    def _merge_history(self, to_merge: list, old_merge: dict) -> str or None:
        """历史合并回调：把被合并的旧消息交给 LLM 压缩（带时间标注）。"""
        return summarize_history(to_merge, old_merge=old_merge)

    def _tool_result(self, call: dict) -> str:
        """工具执行结果回传：update_state 走状态文本；query_archive 走归档；
        其余走 rmgame bridge。调用参数与返回内容同时输出到控制台。

        call：extract_tool_calls 提取的 {id, name, arguments}。
        """
        name = call.get("name", "")
        args_raw = call.get("arguments") or "{}"
        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError:
            args = {}
        if name == "update_state":
            result = tool_result_text(self.state)   # 状态已由 apply_state 结算
        elif name in ("emote", "bounce"):
            # 兜底引导：旧版本 TOOLS 曾把 emote/bounce 列为独立工具，模型可能
            # 照旧习气调用；它们只是 update_state 的参数，直接指引正确通道，
            # 不要落进 rmgame bridge 报"未知工具"误导模型。
            result = ("emote / bounce 不是独立工具：请通过 update_state 工具的 "
                      "emote / bounce 参数表达情绪与动作。")
        elif name == "query_archive":
            result = self.ctx.query_archive(query=args.get("query"),
                                            limit=args.get("limit", 5),
                                            detail=args.get("detail", False))
        elif name == "mora_notes":
            result = notes_execute(args.get("action"), args)   # 角色私有笔记
        else:
            result = execute_tool(name, args)       # rmgame 点评工具（语义化结果）
        if name in _GAME_TOOL_NAMES:
            # 游戏世界内容统一包裹：wiki 词条 / raw 原文 / 当前快照 / 仲裁与
            # 重建结果都属游戏世界，用 <game_data> 标签与角色的身份、对话空间
            # 隔离（与环境段的 <game_environment> 同一套标签体系）。
            result = f"<game_data>\n{result}\n</game_data>"
        if name != "update_state":
            # 统一收尾指引：任何工具执行后都引导回 say 通道（update_state 的
            # tool_result_text 已自带类似指引，不重复追加），避免模型查询完
            # 直接 content 直出结束、绕过台词工具。
            result += ("\n\n你可以继续调用其他工具推进工作，"
                       "或调用 say 工具说出台词结束本回合。")
        print(f"\n{'=' * 20} 工具调用: {name} {'=' * 20}")
        print(f"参数: {args_raw}")
        print(f"{'=' * 20} 工具返回 {'=' * 20}")
        print(result)
        return result

    def _run_tools(self, parsed):
        """执行 LLM 调用的工具；UI 动作调度到主线程（tkinter 非线程安全）。"""
        if getattr(parsed, "bounce", False):
            self.root.after(0, self._bounce)

    def _show_error(self, msg: str):
        self._request_bubble("⚠️ " + msg, fg=ERR_COLOR, bg="#fdf0ef")
        self._busy = False

    # ------------------------------------------------------------------ 气泡

    def _request_bubble(self, text: str, fg: str, bg: str,
                        typewriter: bool = False):
        """气泡显示请求：入队（FIFO），按完整生命周期调度。

        每条台词完整走「淡入 → 打字/呈现 → 停留 → 淡出」后，才显示下一条；
        停留时长按文本长度自适应（阅读速度折算，夹在 min/max 之间），
        长台词不再被快速覆盖，最终台词自然排在队列末尾。
        仅 UI 线程调用。
        """
        self._bubble_queue.append((text, fg, bg, typewriter))
        self._pump_bubble()

    def _pump_bubble(self):
        """队列调度：当前气泡生命周期结束后才取下一条显示。"""
        if self._bubble_phase is not None:
            return  # 当前气泡仍在生命周期中（打字/停留/淡出），等它走完
        if not self._bubble_queue:
            return
        text, fg, bg, tw = self._bubble_queue.pop(0)
        self._bubble_text(text, fg=fg, bg=bg, typewriter=tw)

    def _hold_ms(self, text_len: int) -> int:
        """停留时长：按文本长度折算阅读时间，夹在配置的 min/max 之间。

        防呆：绝对下限 300ms、上限不低于下限 —— 即使个别配置解析异常
        （曾因秒类配置误归整数表回退 0），气泡也不会瞬间消失。
        """
        min_ms = max(300, int(float(CONFIG.get("bubble_min_seconds", 3.0)) * 1000))
        max_ms = max(min_ms, int(float(CONFIG.get("bubble_max_seconds", 15.0)) * 1000))
        read_ms = max(0, float(CONFIG.get("bubble_read_ms_per_char", 70)))
        return max(int(min_ms), min(int(max_ms), int(text_len * read_ms)))

    def _fade_to(self, target: float, ms: int, on_done=None):
        """气泡窗口透明度渐变（alpha 0.0~1.0）；不支持 alpha 的平台直接完成。"""
        if self._bubble is None or not self._bubble.winfo_exists():
            if on_done:
                on_done()
            return
        try:
            cur = float(self._bubble.attributes("-alpha"))
        except Exception:
            cur = 1.0
        steps = max(1, int(ms / 16))
        delta = (target - cur) / steps

        def step(i=0):
            self._bubble_after = None   # 本次 after 已触发
            if self._bubble is None or not self._bubble.winfo_exists():
                return
            val = cur + delta * (i + 1)
            try:
                self._bubble.attributes("-alpha", max(0.0, min(1.0, val)))
            except Exception:
                pass
            if i + 1 >= steps:
                if on_done:
                    on_done()
            else:
                self._bubble_after = self.root.after(16, lambda: step(i + 1))

        step()

    def _bubble_text(self, text: str, fg: str, bg: str, typewriter: bool = False):
        if self._bubble is None or not self._bubble.winfo_exists():
            x, y = self.root.winfo_x(), self.root.winfo_y()
            self._bubble = tk.Toplevel(self.root)
            self._bubble.overrideredirect(True)
            self._bubble.attributes("-topmost", True)
            self._bubble.attributes("-transparentcolor", KEY_COLOR)
            self._bubble.configure(bg=KEY_COLOR)
            self._bubble.geometry(f"+{x + self.w + 10}+{y + max(0, self.h // 2 - 80)}")
            self._bubble_canvas = tk.Canvas(self._bubble, bg=KEY_COLOR, highlightthickness=0)
            self._bubble_canvas.pack()
            self._bubble_canvas.bind("<Button-1>", lambda e: self._dismiss_bubble())
        # 打断旧后台链（打字/停留/淡出）：旧链可能仍在跑，统一取消避免操作新气泡
        if self._bubble_after is not None:
            try:
                self.root.after_cancel(self._bubble_after)
            except Exception:
                pass
            self._bubble_after = None
        self._last_bubble_text = text
        self._bubble_canvas.delete("all")
        fnt = tkfont.Font(family="Microsoft YaHei UI", size=11)
        lines = self._wrap(text, fnt)
        lh = int(fnt.metrics("linespace") * 1.15)   # 行距加宽，提升长句可读性
        pad_x, pad_y = 14, 10
        tail_h = 10
        w = min(BUBBLE_MAX_W, max(fnt.measure(line) for line in lines) + pad_x * 2)
        h = len(lines) * lh + pad_y * 2 + tail_h
        shadow = int(CONFIG.get("bubble_shadow", 3))
        corner = int(CONFIG.get("bubble_corner", 14))
        self._bubble_canvas.configure(width=w + shadow, height=h + shadow)
        # 阴影：先画偏移的深色圆角矩形（放在正文下层）
        if shadow > 0:
            self._rounded_rect(shadow, shadow, w + shadow - 1, h - tail_h - 1 + shadow,
                               corner, fill="#d8d2c4", outline="")
        # 尾巴（右下角小三角，指向立绘）
        self._bubble_canvas.create_polygon(
            w - 16 + shadow, h - tail_h - 2 + shadow,
            w - 4 + shadow, h - tail_h - 2 + shadow,
            w - 12 + shadow, h + shadow,
            fill=bg, outline=bg)
        # 主体：圆角矩形 + 边框
        self._rounded_rect(1, 1, w - 1, h - tail_h - 1,
                           corner, fill=bg, outline=BUBBLE_BORDER, width=1)
        y0 = pad_y
        if typewriter:
            self._typewrite(lines, fnt, lh, pad_x, y0, fg,
                            on_done=self._begin_hold)
            self._bubble_phase = "typing"
        else:
            for line in lines:
                self._bubble_canvas.create_text(pad_x, y0, anchor="nw", text=line, font=fnt, fill=fg)
                y0 += lh
            self._bubble_phase = "hold"
            self._begin_hold()
        # 淡入（新气泡从透明到不透明，避免突兀闪现）
        try:
            self._bubble.attributes("-alpha", 0.0)
        except Exception:
            pass
        self._fade_to(1.0, int(CONFIG.get("bubble_fade_ms", 150)))

    def _rounded_rect(self, x1, y1, x2, y2, r, cv=None, **kw):
        """canvas 圆角矩形（smooth 多边形近似），r 为圆角半径。

        cv 指定绘制画布（默认台词气泡 self._bubble_canvas；
        游戏确认气泡等独立窗口传入自己的 canvas）。
        """
        cv = cv or self._bubble_canvas
        if r <= 0 or x2 - x1 < r * 2 or y2 - y1 < r * 2:
            return cv.create_rectangle(x1, y1, x2, y2, **kw)
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return cv.create_polygon(pts, smooth=True, **kw)

    def _typewrite(self, lines, fnt, lh, pad_x, y0, fg, on_done=None):
        # 每行预创建独立的文本项（初始为空）；打字机只更新当前行，
        # 不删除其他行 —— 避免旧行随新行出现而消失
        items = [
            self._bubble_canvas.create_text(
                pad_x, y0 + i * lh, anchor="nw", text="", font=fnt, fill=fg)
            for i in range(len(lines))
        ]
        self._tw_state = {"lines": lines, "items": items, "li": 0, "ci": 0}
        type_ms = int(CONFIG.get("bubble_type_ms", 18))
        pause_chars = set(CONFIG.get("bubble_pause_chars", "。！？…，；：、."))
        pause_ms = int(CONFIG.get("bubble_pause_ms", 160))
        cursor = "▍"   # 打字光标（行尾竖条）

        def step():
            self._bubble_after = None   # 本次 after 已触发
            st = self._tw_state
            if self._bubble is None or not self._bubble.winfo_exists():
                return
            if st["li"] >= len(st["lines"]):
                if on_done:
                    on_done()
                return
            line = st["lines"][st["li"]]
            ci = st["ci"]
            shown = line[:ci + 1] + cursor if ci + 1 < len(line) else line[:ci + 1]
            self._bubble_canvas.itemconfigure(st["items"][st["li"]], text=shown)
            st["ci"] += 1
            delay = type_ms
            if ci < len(line) and line[ci] in pause_chars:
                delay += pause_ms     # 句读标点：多停一会，模拟自然朗读节奏
            if st["ci"] > len(line):
                st["ci"] = 0
                st["li"] += 1
            self._bubble_after = self.root.after(delay, step)

        step()

    def _begin_hold(self):
        """打字完成（或非打字直接呈现）后：按文本长度停留，然后淡出关闭。"""
        if self._bubble is None or not self._bubble.winfo_exists():
            self._bubble_phase = None
            self._pump_bubble()
            return
        self._bubble_phase = "hold"
        text_len = len(self._last_bubble_text or "")
        if self._bubble_timer is not None:
            try:
                self.root.after_cancel(self._bubble_timer)
            except Exception:
                pass
        self._bubble_timer = self.root.after(
            self._hold_ms(max(1, text_len)), self._begin_fade_out)

    def _begin_fade_out(self):
        """停留结束：淡出后销毁并继续队列。"""
        if self._bubble_timer is not None:
            self._bubble_timer = None
        self._bubble_phase = "fade_out"
        fade_ms = int(CONFIG.get("bubble_fade_ms", 150))
        self._fade_to(0.0, fade_ms, on_done=self._dismiss_bubble)

    def _wrap(self, text: str, fnt) -> list:
        lines, cur = [], ""
        for ch in text:
            if ch == "\n":
                lines.append(cur)
                cur = ""
                continue
            if fnt.measure(cur + ch) > BUBBLE_MAX_W - 28:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
        return lines or [""]

    def _dismiss_bubble(self):
        """立即销毁气泡并继续队列（点击关闭 / 淡出完成共用）。"""
        if self._bubble_timer is not None:
            try:
                self.root.after_cancel(self._bubble_timer)
            except Exception:
                pass
            self._bubble_timer = None
        self._bubble_phase = None
        if self._bubble is not None and self._bubble.winfo_exists():
            self._bubble.destroy()
        self._bubble = None
        self._pump_bubble()

    # ------------------------------------------------------------------ 动画

    def _idle_loop(self):
        if CONFIG.get("idle_bob", True) and not self._busy:
            dy = round(math.sin(self._bob_phase) * 5)
            x = self.root.winfo_x()
            self.root.geometry(f"+{x}+{self._bob_base_y + dy}")
            self._bob_phase += 0.08
            if self._bubble is not None and self._bubble.winfo_exists():
                self._sync_followers()
        self.root.after(80, self._idle_loop)

    def _bounce(self):
        """bounce 工具：立绘原地跳一下。"""
        x = self.root.winfo_x()
        base = self._bob_base_y
        steps = [(-18, 60), (-30, 55), (-36, 50), (-30, 55), (-18, 60), (0, 70)]

        def go(i):
            if i >= len(steps):
                self.root.geometry(f"+{x}+{base}")
                return
            dy, ms = steps[i]
            self.root.geometry(f"+{x}+{base + dy}")
            self.root.after(ms, lambda: go(i + 1))

        go(0)

    def _schedule_auto_chat(self):
        """闲置闲聊定时器：间隔双向自适应（2 分钟 ↔ 48 小时）。

        对方无操作时：每次闲置触发成功后间隔按 auto_chat_growth 倍递增
        （前密后疏，上限 auto_chat_max_minutes → 长时间离开进入休眠）；
        对方有操作（_send）时：间隔按同倍率减少（下限初始值），并**重排
        定时器**（取消已排定的检查、从互动时刻重新计时），避免"刚互动完
        又触发"；fire 还带保护窗口（auto_chat_min_gap_seconds 内跳过）。
        0 = 关闭。
        """
        self._idle_interval = float(CONFIG.get("auto_chat_minutes", 10) or 0)
        if self._idle_interval <= 0:
            return
        self._auto_chat_after = None
        self._last_user_at = time.monotonic()   # 上次对方互动时刻
        self._arm_auto_chat()

    def _arm_auto_chat(self):
        """按当前间隔排定闲置闲聊检查（fire 自递归；可被对方互动重排）。"""
        def fire():
            self._auto_chat_after = None
            min_gap = float(CONFIG.get("auto_chat_min_gap_seconds", 60))
            if (not self._busy
                    and time.monotonic() - self._last_user_at >= min_gap):
                self._auto_chat()
                # 闲置触发成功后间隔递增（对方没操作 → 越来越疏）
                growth = float(CONFIG.get("auto_chat_growth", 2.0))
                max_min = float(CONFIG.get("auto_chat_max_minutes", 2880))
                self._idle_interval = min(self._idle_interval * growth, max_min)
            self._auto_chat_after = self.root.after(
                int(self._idle_interval * 60 * 1000), fire)

        self._auto_chat_after = self.root.after(
            int(self._idle_interval * 60 * 1000), fire)

    def _decay_idle_interval(self):
        """对方操作后：间隔平滑减少（下限初始）+ 重排闲置定时器。

        取消已排定的检查并按新间隔重新计时（从互动时刻起算），
        避免"对方刚互动完又恰巧触发闲置激活"；同时记录互动时刻
        供 fire 的保护窗口判断。
        """
        initial = float(CONFIG.get("auto_chat_minutes", 10) or 0)
        growth = float(CONFIG.get("auto_chat_growth", 2.0))
        self._idle_interval = max(initial, self._idle_interval / growth)
        self._last_user_at = time.monotonic()
        if getattr(self, "_auto_chat_after", None) is not None:
            try:
                self.root.after_cancel(self._auto_chat_after)
            except Exception:
                pass
            self._arm_auto_chat()   # 用新间隔重新计时

    def _greet(self):
        """启动问候：调用 LLM 生成一句开场问候（气泡显示，走完整对话流程）。

        与 auto_chat 同模式：注入一条 user 角色的伪消息触发 _request_llm，
        最终台词经 _finish_agent 显示。问候台词会留在对话历史中，
        作为后续对话的上下文起点。
        恢复存档后重启：在消息里附上距上次对话的时间流逝（如
        （距上次对话 3 小时）对方看向了角色），让角色感知时间间隔。
        """
        if not CONFIG.get("greet_on_start", True) or self._busy:
            return
        self._busy = True
        gap = getattr(self, "_resume_gap", None)
        if gap:
            self.ctx.add_user(f"（{gap}）{USER_REFERENCE}看向了角色")
        else:
            self.ctx.add_user(f"{USER_REFERENCE}看向了角色")
        threading.Thread(target=self._request_llm, daemon=True).start()

    def _auto_chat(self):
        self._busy = True
        # 闲置激活词按游戏环境分流：对方在玩游戏时引导贴合场景的主动点评
        env = self._env_snapshot()
        self.ctx.add_user(idle_activation_prompt(env))
        threading.Thread(target=self._request_llm, daemon=True).start()

    # ------------------------------------------------------------------ 退出

    def _quit(self):
        # 停止 rmgame 监控守护线程（daemon 也会随进程退出，这里显式停止更干净）
        self._monitor_stop.set()
        # 状态与对话历史落盘（回合结束已保存，退出时再兜底一次）
        persist.save_session(self.state, self.ctx.history,
                             self.ctx.merge, self.ctx.archives)
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        MoraPet(root)
    except Exception as exc:
        print(f"[启动失败] {exc}")
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
