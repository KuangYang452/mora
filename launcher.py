# -*- coding: utf-8 -*-
"""启动器 / 控制台 —— launcher（1.2 易用性，下载即用）

三个页签（启动 / 游戏库 / 配置）+ 首次运行向导，纯 tkinter，零新增依赖：
- 启动页：依赖自检（缺失一键 pip 安装）、LLM 配置检查、一键启动桌宠（子进程）；
- 游戏库页：选目录 / 选 Game.exe 扫描入库（复用 rmgame/discovery），库管理
  （删除 / approve 解锁启动 / 刷新）；
- 配置页：LLM / 应用 / 用户三 tab，文本级键值写回（保留 ini 注释），
  必填与合法值校验，可重置为模板。

用法：
  python launcher.py             # 打开控制台
  python launcher.py --selftest  # 离线自检（不弹 GUI）
"""

import configparser
import importlib.util
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox
from pathlib import Path
from tkinter import ttk

import settings
from rmgame import discovery

ROOT = Path(__file__).resolve().parent
SETTING_DIR = ROOT / "setting"
ASSETS_DIR = ROOT / "assets"
ICO_PATH = ASSETS_DIR / "launcher.ico"

# 各 ini 的解析模式（与 settings.py 一致：带行内注释的文件 inline=True）
_INI_INLINE = {"app.ini": True, "user.ini": True, "llm.ini": False}

# reasoning_effort 合法值（DeepSeek thinking）
REASONING_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def _pythonw() -> str:
    """优先返回 pythonw.exe（无控制台窗口的 Python）；非 Windows 或无 pythonw
    时回退 sys.executable。用于启动桌宠等 GUI 子进程，避免弹黑窗。"""
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand) if cand.exists() else str(exe)


def _ps_quote(s: str) -> str:
    """PowerShell 单引号转义（路径含引号时防注入）。"""
    return str(s).replace("'", "''")


def create_desktop_shortcut() -> tuple:
    """桌面创建「莫拉桌宠」快捷方式（pythonw 无黑窗 + launcher.ico 图标）。

    返回 (ok, msg)。中文快捷方式名由 Python 传入 PowerShell（UTF-8 安全）；
    供启动页按钮与 `launcher.py --make-shortcut`（bat 委托入口）共用。
    """
    if os.name != "nt":
        return False, "桌面快捷方式仅支持 Windows。"
    icon = ICO_PATH if ICO_PATH.exists() else ROOT / "launcher.py"
    ps = (
        "$ws = New-Object -ComObject WScript.Shell;"
        "$lnk = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop')"
        " + '\\莫拉桌宠.lnk');"
        f"$lnk.TargetPath = '{_ps_quote(_pythonw())}';"
        "$lnk.Arguments = 'launcher.py';"
        f"$lnk.WorkingDirectory = '{_ps_quote(ROOT)}';"
        f"$lnk.IconLocation = '{_ps_quote(icon)}';"
        "$lnk.Description = '莫拉桌宠控制台';"
        "$lnk.Save()"
    )
    _no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                              capture_output=True, text=True, timeout=60,
                              creationflags=_no_window)
        if proc.returncode == 0:
            return True, "已在桌面创建「莫拉桌宠」快捷方式（带图标）。"
        return False, (proc.stderr or "").strip() or f"退出码 {proc.returncode}"
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# 配置读写（文本级键值替换，保留注释）
# ---------------------------------------------------------------------------

def _set_ini_value(path: Path, section: str, key: str, value: str) -> None:
    """文本级键值替换：定位 [section] 下 `key =` 行，仅替换值并保留行内注释。

    configparser 写回会丢弃注释，故用文本替换（值中不出现 ; / #——
    app.ini 的解析本就将其视为行内注释，读写一致）。键不存在时在节尾追加；
    节不存在时追加新节。
    """
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    sec_idx = None
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*\[(.+?)\]\s*$", ln)
        if m and m.group(1).strip() == section and sec_idx is None:
            sec_idx = i
    if sec_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        lines.append(f"{key} = {value}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    end = len(lines)
    for i in range(sec_idx + 1, len(lines)):
        if re.match(r"^\s*\[", lines[i]):
            end = i
            break
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*)$")
    for i in range(sec_idx + 1, end):
        m = key_re.match(lines[i])
        if m:
            rest = m.group(1)
            cm = re.search(r"\s*[;#].*$", rest)
            comment = cm.group(0) if cm else ""
            lines[i] = f"{key} = {value}{comment}"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return
    lines.insert(end, f"{key} = {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_config_section(file_name: str, section: str) -> dict:
    """读取 ini 指定节为 dict；文件缺失时回退 .example 模板；仍缺失返回 {}。"""
    for name in (file_name, file_name + ".example"):
        p = SETTING_DIR / name
        if p.exists():
            cp = configparser.RawConfigParser(
                inline_comment_prefixes=("#", ";") if _INI_INLINE.get(file_name) else None)
            cp.read(p, encoding="utf-8")
            if cp.has_section(section):
                return {k: v for k, v in cp.items(section)}
            return {}
    return {}


def _ensure_ini_from_template(file_name: str) -> Path:
    """配置文件缺失时基于 .example 模板创建（无模板则建空 [section]）。"""
    p = SETTING_DIR / file_name
    if p.exists():
        return p
    tpl = SETTING_DIR / (file_name + ".example")
    p.write_text(tpl.read_text(encoding="utf-8") if tpl.exists() else "",
                 encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 校验（纯函数，供 selftest）
# ---------------------------------------------------------------------------

def validate_value(kind: str, raw, options=None, minv=None, maxv=None):
    """字段值校验：返回错误消息或 None。kind: str/float/int/bool/enum。"""
    if kind == "str":
        return None if str(raw).strip() else "不能为空"
    if kind == "bool":
        return None
    if kind == "float":
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return "必须是数字"
        if minv is not None and v < minv:
            return f"不能小于 {minv}"
        if maxv is not None and v > maxv:
            return f"不能大于 {maxv}"
        return None
    if kind == "int":
        try:
            v = int(str(raw).strip())
        except (TypeError, ValueError):
            return "必须是整数"
        if minv is not None and v < minv:
            return f"不能小于 {minv}"
        if maxv is not None and v > maxv:
            return f"不能大于 {maxv}"
        return None
    if kind == "enum":
        return None if str(raw) in (options or []) else f"必须是 {' / '.join(options or [])} 之一"
    return None


# ---------------------------------------------------------------------------
# 表单字段模型
# ---------------------------------------------------------------------------

class _Field:
    """配置表单字段：kind=str/float/int/bool/enum；widget 由 ConfigPage 创建。"""

    def __init__(self, section, key, label, kind="str", options=None,
                 minv=None, maxv=None, required=False, default=""):
        self.section, self.key, self.label = section, key, label
        self.kind = kind
        self.options = options or []
        self.minv, self.maxv = minv, maxv
        self.required = required
        self.default = default
        self.widget = None

    def create(self, parent, row):
        ttk.Label(parent, text=self.label).grid(row=row, column=0, sticky="w", padx=6, pady=3)
        if self.kind == "bool":
            var = tk.BooleanVar(value=False)
            self.widget = ttk.Checkbutton(parent, variable=var)
            self.widget.var = var
        elif self.kind == "enum":
            self.widget = ttk.Combobox(parent, values=self.options, state="readonly",
                                       width=28)
        else:
            self.widget = ttk.Entry(parent, width=32)
        self.widget.grid(row=row, column=1, sticky="w", padx=6, pady=3)
        return row + 1

    def set(self, val):
        if self.kind == "bool":
            self.widget.var.set(str(val).strip().lower() in ("1", "true", "yes", "on"))
        elif self.kind == "enum":
            if val in self.options:
                self.widget.set(val)
        else:
            self.widget.delete(0, tk.END)
            self.widget.insert(0, str(val or self.default))

    def get(self):
        if self.kind == "bool":
            return "true" if self.widget.var.get() else "false"
        return str(self.widget.get()).strip() or str(self.default)

    def validate(self):
        return validate_value(self.kind, self.get(), self.options, self.minv, self.maxv)


# ---------------------------------------------------------------------------
# 页签 1：启动页
# ---------------------------------------------------------------------------

class LauncherPage(ttk.Frame):
    def __init__(self, master, on_goto_config):
        super().__init__(master, padding=12)
        self._on_goto_config = on_goto_config
        self._proc = None
        self._items = []          # (名称, 状态函数, 动作按钮)
        self._build()

    def _build(self):
        # 信息行
        info = ttk.Label(self, text="", font=("", 11, "bold"))
        info.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self._info = info
        # 自检清单
        ttk.Label(self, text="启动前检查：").grid(row=1, column=0, sticky="nw")
        box = ttk.Frame(self)
        box.grid(row=2, column=0, columnspan=2, sticky="ew", pady=4)
        self._check_frame = box
        # 动作区
        acts = ttk.Frame(self)
        acts.grid(row=3, column=0, columnspan=2, sticky="w", pady=10)
        ttk.Button(acts, text="🚀 启动桌宠", command=self._launch).pack(side="left", padx=4)
        ttk.Button(acts, text="创建桌面快捷方式", command=self._make_shortcut).pack(side="left", padx=4)
        ttk.Button(acts, text="重新检查", command=self._refresh_all).pack(side="left", padx=4)
        self._run_var = tk.StringVar(value="")
        ttk.Label(acts, textvariable=self._run_var).pack(side="left", padx=10)
        # 说明
        ttk.Label(self, text="首次使用：装好依赖、填好 LLM 配置后，点「启动桌宠」即可。\n"
                             "桌宠为独立进程运行，本窗口可随时关闭。",
                  foreground="#666").grid(row=4, column=0, columnspan=2, sticky="w")

    def refresh(self):
        import character as character_mod
        self._info.config(text=f"v{settings.VERSION}  |  当前角色：{_char_name()}  |  "
                               f"游戏库 {len(discovery.load_games())} 个")
        for w in self._check_frame.winfo_children():
            w.destroy()
        self._items = []
        self._add_check("Python 依赖（requests / pillow）", self._dep_ok,
                        "一键安装", self._install_deps)
        self._add_check("LLM 配置（setting/llm.ini）", self._llm_ok, "去配置",
                        self._on_goto_config)
        self._add_check("角色包（character/）", self._char_ok, None, None)

    def _add_check(self, label, ok_fn, btn_text, btn_cmd):
        row = ttk.Frame(self._check_frame)
        row.pack(fill="x", pady=2)
        var = tk.StringVar(value="…")
        ok, msg = ok_fn()
        var.set(("✓ " if ok else "✗ ") + msg)
        ttk.Label(row, textvariable=var, width=44, anchor="w").pack(side="left")
        if btn_text and btn_cmd:
            ttk.Button(row, text=btn_text, command=btn_cmd).pack(side="left", padx=4)
        self._items.append((var, ok_fn))

    def _dep_ok(self):
        missing = [m for m in ("requests", "PIL") if importlib.util.find_spec(m) is None]
        if not missing:
            return True, "依赖完整"
        return False, "缺少 " + ", ".join(missing)

    def _llm_ok(self):
        try:
            cfg = settings.llm_config()
            ok = bool(cfg.get("base_url") and cfg.get("api_key") and cfg.get("model"))
            return ok, ("配置完整" if ok else "配置不完整（去配置页补全）")
        except Exception as exc:
            return False, f"未配置（{type(exc).__name__}）"

    def _char_ok(self):
        import character as character_mod
        try:
            c = character_mod.current()
            return True, f"{c.slug}（{c.display_name}）"
        except Exception:
            return False, "无可用角色包（character/<slug>/）"

    def _install_deps(self):
        deps = ["requests", "pillow"]
        if messagebox.askyesno("一键安装", "将执行:\n\n"
                                f"  python -m pip install {' '.join(deps)}\n\n"
                                "（可选 OCR 依赖 pytesseract 可稍后自行安装）\n继续吗？"):
            self._run_var.set("正在安装依赖…（窗口可能短暂无响应）")
            threading.Thread(target=self._pip_worker, args=(deps,), daemon=True).start()

    def _pip_worker(self, deps):
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # pythonw 下防控制台闪窗
            proc = subprocess.run([sys.executable, "-m", "pip", "install", *deps],
                                  capture_output=True, text=True, timeout=600,
                                  creationflags=flags)
            ok = proc.returncode == 0
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
            msg = "依赖安装完成 ✓" if ok else f"安装失败：{tail[-1] if tail else proc.returncode}"
        except Exception as exc:
            ok, msg = False, f"安装失败：{exc}"
        self.after(0, lambda: (self._run_var.set(msg), self.refresh()))

    def _launch(self):
        if self._proc is not None and self._proc.poll() is None:
            self._run_var.set("桌宠已在运行")
            return
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                [_pythonw(), "pet.py"], cwd=str(ROOT), creationflags=flags)
            self._run_var.set(f"桌宠已启动（PID {self._proc.pid}，无控制台窗口）")
        except Exception as exc:
            self._run_var.set(f"启动失败：{exc}")
        self._poll_proc()

    def _make_shortcut(self):
        ok, msg = create_desktop_shortcut()
        if ok:
            messagebox.showinfo("完成", msg)
        else:
            messagebox.showerror("失败", msg)

    def _poll_proc(self):
        if self._proc is not None and self._proc.poll() is None:
            self.after(2000, self._poll_proc)

    def _refresh_all(self):
        self.refresh()


def _char_name() -> str:
    try:
        import character as character_mod
        return character_mod.current().display_name
    except Exception:
        return "（无）"


# ---------------------------------------------------------------------------
# 页签 2：游戏库页
# ---------------------------------------------------------------------------

class _NameDialog(tk.Toplevel):
    """游戏命名/改名对话框。

    existing=False（入库前）：显示名可编辑，slug 随名字实时预览推导；
    existing=True（库内重命名）：slug 保持稳定（数据目录不迁移），
    旧名自动并入别名供匹配。
    """

    def __init__(self, master, title, current_name, current_slug, existing=False):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self.result = None
        self._existing = existing
        ttk.Label(self, text="游戏名（可自定义；入库后作为显示名与匹配名）：")\
            .grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 2))
        self._name_var = tk.StringVar(value=current_name)
        ent = ttk.Entry(self, textvariable=self._name_var, width=40)
        ent.grid(row=1, column=0, columnspan=2, sticky="w", padx=10)
        self._hint_var = tk.StringVar()
        ttk.Label(self, textvariable=self._hint_var, foreground="#666")\
            .grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(4, 10))
        self._name_var.trace_add("write", lambda *_: self._update_hint())
        self._update_hint()
        btns = ttk.Frame(self)
        btns.grid(row=3, column=0, columnspan=2, pady=(0, 10))
        ttk.Button(btns, text="确定", command=self._ok).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left", padx=6)
        self.transient(master)
        self.grab_set()
        ent.focus_set()
        ent.select_range(0, tk.END)
        self.bind("<Return>", lambda *_: self._ok())
        self.bind("<Escape>", lambda *_: self.destroy())

    def _update_hint(self):
        name = self._name_var.get().strip()
        if self._existing:
            self._hint_var.set("改名后标识（slug）随新名更新，已提取的 raw / wiki / "
                               "事件摘要目录会自动迁移；原名字自动加入别名。")
        else:
            self._hint_var.set("标识（slug）预览：" + discovery.make_slug(name))

    def _ok(self):
        name = self._name_var.get().strip()
        if not name:
            messagebox.showwarning("提示", "游戏名不能为空。", parent=self)
            return
        self.result = name
        self.destroy()


class GamesPage(ttk.Frame):
    COLS = ("name", "engine", "dir", "status")

    def __init__(self, master):
        super().__init__(master, padding=12)
        self._found = []          # 最近一次扫描结果（GameInfo 列表）
        self._build()

    def _build(self):
        # 添加入库区
        add = ttk.LabelFrame(self, text="添加入库", padding=8)
        add.pack(fill="x", pady=(0, 8))
        ttk.Button(add, text="选择目录…", command=self._scan_dir).pack(side="left", padx=4)
        ttk.Button(add, text="选择 Game.exe…", command=self._scan_exe).pack(side="left", padx=4)
        self._scan_var = tk.StringVar(value="未扫描")
        ttk.Label(add, textvariable=self._scan_var).pack(side="left", padx=8)
        self._tree = ttk.Treeview(self, columns=self.COLS, show="headings", height=7,
                                  selectmode="extended")
        for c, w, t in (("name", 140, "名称"), ("engine", 60, "引擎"),
                        ("dir", 300, "目录"), ("status", 70, "状态")):
            self._tree.heading(c, text=t)
            self._tree.column(c, width=w, anchor="w")
        self._tree.pack(fill="x")
        scan_btns = ttk.Frame(self)
        scan_btns.pack(anchor="w", pady=4)
        ttk.Button(scan_btns, text="入库选中项（trust=user，可直接启动）",
                   command=self._register_selected).pack(side="left", padx=4)
        ttk.Button(scan_btns, text="改名选中（游戏名不标准时）",
                   command=self._edit_found_name).pack(side="left", padx=4)
        self._tree.bind("<Double-1>", lambda e: self._edit_found_name())
        # 游戏库管理区
        lib = ttk.LabelFrame(self, text="游戏库（runtime/games.json）", padding=8)
        lib.pack(fill="both", expand=True)
        self._lib = ttk.Treeview(lib, columns=("name", "engine", "trust", "raw", "run"),
                                 show="headings", height=8, selectmode="extended")
        for c, w, t in (("name", 140, "名称"), ("engine", 60, "引擎"),
                        ("trust", 60, "信任"), ("raw", 70, "raw"), ("run", 60, "运行中")):
            self._lib.heading(c, text=t)
            self._lib.column(c, width=w, anchor="w")
        self._lib.pack(fill="both", expand=True)
        btns = ttk.Frame(lib)
        btns.pack(anchor="w", pady=4)
        ttk.Button(btns, text="刷新", command=self._refresh_library).pack(side="left", padx=4)
        ttk.Button(btns, text="改名", command=self._rename_library_item).pack(side="left", padx=4)
        ttk.Button(btns, text="删除选中", command=self._delete_selected).pack(side="left", padx=4)
        ttk.Button(btns, text="确认选中（解锁启动）", command=self._approve_selected).pack(side="left", padx=4)
        self._lib.bind("<Double-1>", lambda e: self._rename_library_item())

    # ---- 扫描 ----

    def _scan_dir(self):
        d = filedialog.askdirectory(title="选择游戏目录（含 Game.exe 的目录或其上级）")
        if not d:
            return
        self._scan_var.set("扫描中…")
        threading.Thread(target=self._scan_worker,
                         args=(lambda: discovery.discover(d, recursive=True),),
                         daemon=True).start()

    def _scan_exe(self):
        f = filedialog.askopenfilename(title="选择 Game.exe", filetypes=[("Game.exe", "Game.exe")])
        if not f:
            return
        self._scan_var.set("识别中…")
        threading.Thread(target=self._scan_worker,
                         args=(lambda: [g] if (g := discovery.discover_dir(f)) else [],),
                         daemon=True).start()

    def _scan_worker(self, fn):
        try:
            found, err = fn(), None
        except Exception as exc:
            found, err = [], str(exc)
        self.after(0, lambda: self._scan_done(found, err))

    def _scan_done(self, found, err):
        if err:
            self._scan_var.set(f"扫描失败：{err}")
            return
        self._found = found
        self._tree.delete(*self._tree.get_children())
        lib_dirs = {g.dir for g in discovery.load_games()}
        for g in found:
            status = "已入库" if g.dir in lib_dirs else "新发现"
            self._tree.insert("", tk.END, values=(g.name, g.engine, g.dir, status))
        self._scan_var.set(f"发现 {len(found)} 个（勾选/多选后入库；已入库的无需重复添加）")

    def _register_selected(self):
        sel = {self._tree.item(i, "values")[2] for i in self._tree.selection()}
        if not sel:
            messagebox.showinfo("提示", "先在扫描结果中选中要入库的游戏（可多选）。")
            return
        targets = [g for g in self._found if g.dir in sel]
        merged = discovery.register(targets)
        messagebox.showinfo("完成", f"已入库 {len(targets)} 个，游戏库现有 {len(merged)} 个。")
        self._refresh_library()
        self._scan_var.set(f"发现 {len(self._found)} 个（已入库的无需重复添加）")

    def _edit_found_name(self):
        """入库前改名：扫描结果中选中项（可多选）→ 命名对话框 → 更新 GameInfo。

        改名后 slug 按新名重算（尚未入库，无数据目录迁移问题），并标记
        name_manual——之后重扫/自动发现不会用自动名覆盖。
        """
        sel = {self._tree.item(i, "values")[2] for i in self._tree.selection()}
        if not sel:
            messagebox.showinfo("提示", "先在扫描结果中选中要命名的游戏（可多选）。")
            return
        for g in [g for g in self._found if g.dir in sel]:
            dlg = _NameDialog(self, "游戏命名（入库前）", g.name, g.slug)
            self.wait_window(dlg)
            new = (dlg.result or "").strip()
            if not new or new == g.name:
                continue
            g.name = new
            g.slug = discovery.make_slug(new)
            g.name_manual = True
            # 刷新该行显示
            for iid in self._tree.get_children():
                if self._tree.item(iid, "values")[2] == g.dir:
                    self._tree.item(iid, values=(g.name, g.engine, g.dir,
                                                 self._tree.item(iid, "values")[3]))
        self._scan_var.set(f"已按自定义名命名；发现 {len(self._found)} 个（入库后不被自动名覆盖）")

    # ---- 库管理 ----

    def _refresh_library(self):
        self._lib.delete(*self._lib.get_children())
        try:
            from rmgame.monitor import enumerate_running
            running = {g.slug for g, _ in enumerate_running()}
        except Exception:
            running = set()
        for g in discovery.load_games():
            raw = (discovery.RAW_DIR / g.slug / "meta.json").exists()
            self._lib.insert("", tk.END, values=(
                g.name, g.engine, g.trust,
                "已提取" if raw else "未提取",
                "是" if g.slug in running else ""))
        if hasattr(self, "_info_bar"):
            self._info_bar.config(text=f"游戏库共 {len(discovery.load_games())} 个")

    def _rename_library_item(self):
        """库内重命名：统一走 discovery.rename_game（slug 重算 + 数据目录迁移）。"""
        sel = self._lib.selection()
        if not sel:
            messagebox.showinfo("提示", "先在游戏库列表中选中要改名的条目。")
            return
        games = discovery.load_games()
        name = self._lib.item(sel[0], "values")[0]
        g = next((x for x in games if x.name == name), None)
        if g is None:
            return
        dlg = _NameDialog(self, "重命名（库中条目）", g.name, g.slug, existing=True)
        self.wait_window(dlg)
        new = (dlg.result or "").strip()
        if not new or new == g.name:
            return
        ng, msg = discovery.rename_game(g.slug, new)
        if ng is None:
            messagebox.showerror("改名失败", msg)
            return
        messagebox.showinfo("完成", msg + "；旧名已保留为别名。")
        self._refresh_library()

    def _delete_selected(self):
        sel = {self._lib.item(i, "values")[0] for i in self._lib.selection()}
        if not sel:
            messagebox.showinfo("提示", "先在游戏库列表中选中要删除的条目（可多选）。")
            return
        if not messagebox.askyesno("确认删除",
                                   f"从游戏库移除 {len(sel)} 个条目？（不影响游戏文件与 raw/wiki 数据）"):
            return
        games = [g for g in discovery.load_games() if g.name not in sel]
        discovery.save_games(games)
        self._refresh_library()

    def _approve_selected(self):
        for i in self._lib.selection():
            name = self._lib.item(i, "values")[0]
            g = next((x for x in discovery.load_games() if x.name == name), None)
            if g is not None:
                discovery.approve(g.slug)
        self._refresh_library()


# ---------------------------------------------------------------------------
# 页签 3：配置页
# ---------------------------------------------------------------------------

_LLM_FIELDS = [
    _Field("llm", "base_url", "API 地址 base_url", "str", required=True,
           default="https://api.deepseek.com/v1"),
    _Field("llm", "api_key", "API Key", "str", required=True),
    _Field("llm", "model", "模型 model", "str", required=True,
           default="deepseek-chat"),
    _Field("llm", "temperature", "temperature", "float", minv=0, maxv=2,
           default="0.95"),
    _Field("llm", "max_tokens", "max_tokens", "int", minv=64, maxv=32768,
           default="1024"),
    _Field("llm", "reasoning", "原生思考模式 reasoning", "bool", default="false"),
    _Field("llm", "reasoning_effort", "推理强度", "enum", options=REASONING_EFFORTS,
           default="medium"),
]

_APP_FIELDS = [
    _Field("app", "tool_choice", "工具强制模式", "enum", options=["required", "auto"],
           default="required"),
    _Field("app", "agent_max_turns", "单回合自主轮数上限", "int", minv=1, maxv=32,
           default="8"),
    _Field("app", "speech_as_tool", "台词强制走 say 工具", "bool", default="true"),
    _Field("app", "retry_on_vague_query", "意向-动作校验重试", "bool", default="true"),
    _Field("app", "force_say_to_finish", "工具收尾校验重试", "bool", default="true"),
    _Field("app", "retry_on_repeated_query", "重复查询校验", "bool", default="true"),
    _Field("app", "greet_on_start", "启动问候", "bool", default="true"),
    _Field("app", "auto_chat_minutes", "闲置闲聊间隔（分钟，0=关）", "int", minv=0,
           default="10"),
    _Field("app", "history_rounds", "上下文保留轮数", "int", minv=2, maxv=200,
           default="30"),
    _Field("app", "scale", "立绘缩放", "float", minv=0.1, maxv=3, default="0.6"),
    _Field("app", "idle_bob", "呼吸浮动动画", "bool", default="true"),
    _Field("app", "rmgame_enabled", "游戏点评工具开关", "bool", default="true"),
    _Field("app", "rmgame_cdp_enabled", "CDP 读取开关", "bool", default="true"),
    _Field("app", "monitor_auto_start", "启动时自动拉起监控", "bool", default="true"),
    _Field("app", "log_enabled", "回合日志", "bool", default="true"),
]

_USER_FIELDS = [
    _Field("user", "ref", "对你的称呼（{{user}} 实例化值）", "str", required=True,
           default="对方"),
]


class ConfigPage(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=12)
        self._banner_var = tk.StringVar(value="")
        self._tabs = []            # [(file, section, fields, frame)]
        self._build()

    def _build(self):
        ttk.Label(self, textvariable=self._banner_var, foreground="#b45309").pack(anchor="w")
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        for title, file_name, fields in (("LLM", "llm.ini", _LLM_FIELDS),
                                         ("应用", "app.ini", _APP_FIELDS),
                                         ("用户", "user.ini", _USER_FIELDS)):
            frame = ttk.Frame(nb, padding=8)
            nb.add(frame, text=title)
            self._tabs.append((file_name, fields, frame))
            row = 0
            for f in fields:
                row = f.create(frame, row)
            ttk.Label(frame, text="保存后：app.ini 需重启桌宠生效；llm.ini / user.ini 下回合生效。",
                      foreground="#666").grid(row=row + 1, column=0, columnspan=2, sticky="w", pady=8)
        btns = ttk.Frame(self)
        btns.pack(anchor="w", pady=8)
        ttk.Button(btns, text="保存配置", command=self._save).pack(side="left", padx=4)
        ttk.Button(btns, text="重置为模板", command=self._reset_template).pack(side="left", padx=4)
        ttk.Button(btns, text="重新载入", command=self.load).pack(side="left", padx=4)

    def load(self):
        for file_name, fields, frame in self._tabs:
            section = fields[0].section
            cfg = _read_config_section(file_name, section)
            for f in fields:
                f.set(cfg.get(f.key, f.default))

    def _save(self):
        errors = [f"{f.label}：{f.validate()}" for _, fields, _ in self._tabs
                  for f in fields if f.validate()]
        if errors:
            messagebox.showerror("配置校验失败", "\n".join(errors[:12]))
            return
        for file_name, fields, _ in self._tabs:
            p = _ensure_ini_from_template(file_name)
            for f in fields:
                _set_ini_value(p, f.section, f.key, f.get())
        messagebox.showinfo("已保存", "配置已保存。\napp.ini 需重启桌宠生效；\nllm.ini / user.ini 下回合生效。")

    def _reset_template(self):
        if not messagebox.askyesno("重置为模板",
                                   "用 .example 模板覆盖当前配置文件？（会丢失手动修改）"):
            return
        for file_name, _, _ in self._tabs:
            tpl = SETTING_DIR / (file_name + ".example")
            if tpl.exists():
                (SETTING_DIR / file_name).write_text(tpl.read_text(encoding="utf-8"),
                                                     encoding="utf-8")
        self.load()
        messagebox.showinfo("完成", "已从模板重置并重新载入。")

    def set_banner(self, text):
        self._banner_var.set(text)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class LauncherApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"莫拉桌宠控制台 v{settings.VERSION}")
        root.geometry("780x620")
        root.minsize(680, 540)
        self._set_icon(root)
        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.launcher = LauncherPage(nb, self._goto_config)
        self.games = GamesPage(nb)
        self.config = ConfigPage(nb)
        nb.add(self.launcher, text="启动")
        nb.add(self.games, text="游戏库")
        nb.add(self.config, text="配置")
        self._nb = nb
        self._first_run()

    def _set_icon(self, root: tk.Tk):
        """窗口 / 任务栏图标：优先 assets/launcher.ico（iconbitmap + iconphoto）。"""
        try:
            if ICO_PATH.exists():
                root.iconbitmap(str(ICO_PATH))
                img = tk.PhotoImage(file=str(ICO_PATH))
                root.iconphoto(True, img)
                self._icon_img = img      # 防 GC
        except Exception:
            pass

    def _first_run(self):
        self.launcher.refresh()
        self.config.load()
        self.games._refresh_library()
        try:
            settings.llm_config()
            ok = True
        except Exception:
            ok = False
        if not ok:
            # 首次运行：LLM 配置缺失 → 直接落配置页引导
            self._nb.select(self.config)
            self.config.set_banner("⚠ 首次运行：请填写下方 LLM 配置（API 地址 / Key / 模型），"
                                   "保存后到「启动」页一键启动桌宠。")
            self.config.load()

    def _goto_config(self):
        self._nb.select(self.config)


# ---------------------------------------------------------------------------
# 离线自检
# ---------------------------------------------------------------------------

def selftest() -> None:
    import tempfile

    # 1) _set_ini_value：更新值保留注释、追加缺失键、追加缺失节
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "app.ini"
        p.write_text(
            "; 顶部注释\n"
            "[app]\n"
            "scale = 0.6     ; 立绘缩放\n"
            "log_enabled = true\n"
            "\n"
            "[paths]\n"
            "save_dir = runtime\n",
            encoding="utf-8")
        _set_ini_value(p, "app", "scale", "0.8")
        _set_ini_value(p, "app", "agent_max_turns", "12")
        _set_ini_value(p, "llm", "model", "deepseek-chat")
        text = p.read_text(encoding="utf-8")
        assert "scale = 0.8     ; 立绘缩放" in text, text      # 注释保留
        assert "log_enabled = true" in text, text              # 未编辑键不动
        assert "[llm]" in text and "model = deepseek-chat" in text, text  # 新节追加
        cp = configparser.RawConfigParser(inline_comment_prefixes=("#", ";"))
        cp.read(p, encoding="utf-8")
        assert cp.get("app", "scale") == "0.8"
        assert cp.get("app", "agent_max_turns") == "12"
        assert cp.get("llm", "model") == "deepseek-chat"
    print("  _set_ini_value: 注释保留 / 键值替换 / 缺失键与节追加 ✓")

    # 2) 校验函数
    assert validate_value("str", "") == "不能为空"
    assert validate_value("str", "x") is None
    assert validate_value("float", "abc") is not None
    assert validate_value("float", "0.95", minv=0, maxv=2) is None
    assert validate_value("float", "3", minv=0, maxv=2) is not None
    assert validate_value("int", "8", minv=1, maxv=32) is None
    assert validate_value("enum", "required", options=["required", "auto"]) is None
    assert validate_value("enum", "x", options=["required", "auto"]) is not None
    print("  validate_value: str/float/int/enum 校验 ✓")

    # 3) 配置读取（真实模板/配置文件，只读不改）
    llm = _read_config_section("llm.ini", "llm")
    assert "model" in llm, "llm.ini 应含 model（缺文件则回退 example 模板）"
    app = _read_config_section("app.ini", "app")
    assert "tool_choice" in app, "app.ini 应含 tool_choice"
    print(f"  _read_config_section: llm({len(llm)} 键) / app({len(app)} 键) ✓")

    # 4) 游戏库可读（runtimes/games.json 可能存在或为空）
    games = discovery.load_games()
    print(f"  discovery.load_games: 游戏库 {len(games)} 个 ✓")

    # 5) 无控制台启动与快捷方式辅助
    assert Path(_pythonw()).name in ("pythonw.exe", "python.exe"), _pythonw()
    assert _ps_quote("a'b") == "a''b", _ps_quote("a'b")
    assert _ps_quote("C:\\mora") == "C:\\mora"
    print(f"  _pythonw: {_pythonw()} | _ps_quote 转义 ✓")

    # 6) 图标存在（assets/launcher.ico，由 make_icon.py 生成）
    assert ICO_PATH.exists(), "缺少 assets/launcher.ico（运行 python make_icon.py 生成）"
    print(f"  launcher.ico: {ICO_PATH.stat().st_size} B ✓")

    # 7) 手动命名持久性：name_manual 条目不被重扫自动名覆盖，新名并入别名
    with tempfile.TemporaryDirectory() as td:
        _orig = discovery.GAMES_FILE
        try:
            discovery.GAMES_FILE = Path(td) / "games.json"
            mk = lambda n, d: discovery.GameInfo(
                slug=discovery.make_slug(n), name=n, exe_path=rf"{d}\Game.exe",
                dir=d, engine="mv", data_dir=rf"{d}\data")
            discovery.register([mk("自动名", r"D:\g")])
            games = discovery.load_games()
            games[0].name = "手动名"
            games[0].name_manual = True
            discovery.save_games(games)
            discovery.register([mk("自动名2", r"D:\g")])   # 重扫：自动名不应覆盖
            gs = discovery.load_games()
            assert gs[0].name == "手动名", gs[0].name
            assert "自动名2" in gs[0].aliases, gs[0].aliases
            # 非手动条目仍被自动名刷新
            discovery.register([mk("普通", r"D:\g2")])
            discovery.register([mk("普通改", r"D:\g2")])
            g2 = next(x for x in discovery.load_games() if x.dir == r"D:\g2")
            assert g2.name == "普通改", g2.name
        finally:
            discovery.GAMES_FILE = _orig
    print("  name_manual: 手动名不被重扫覆盖 / 新检测名并入别名 / 非手动条目仍刷新 ✓")

    # 8) rename_game：改名 + 数据目录迁移（raw/wiki/event_summary）+ 内部字段同步 + slug 冲突拒绝
    import json
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _orig = (discovery.GAMES_FILE, discovery.RAW_DIR, discovery.WIKI_DIR,
                 discovery.RUNTIME_DIR)
        try:
            discovery.GAMES_FILE = td / "games.json"
            discovery.RAW_DIR = td / "raw"
            discovery.WIKI_DIR = td / "wiki"
            discovery.RUNTIME_DIR = td / "runtime"
            mk = lambda n, d: discovery.GameInfo(
                slug=discovery.make_slug(n), name=n, exe_path=rf"{d}\Game.exe",
                dir=d, engine="mv", data_dir=rf"{d}\data")
            discovery.register([mk("暂定名", r"D:\g")])
            (discovery.RAW_DIR / "暂定名" / "maps").mkdir(parents=True)
            (discovery.RAW_DIR / "暂定名" / "meta.json").write_text(
                '{"slug": "暂定名", "name": "暂定名"}', encoding="utf-8")
            (discovery.WIKI_DIR / "暂定名").mkdir(parents=True)
            (discovery.WIKI_DIR / "暂定名" / "index.json").write_text(
                '{"slug": "暂定名", "name": "暂定名", "concepts": []}',
                encoding="utf-8")
            (discovery.RUNTIME_DIR / "event_summary" / "暂定名").mkdir(parents=True)
            # 角色改名：目录迁移 + 内部字段同步 + 旧名进别名
            g, msg = discovery.rename_game("暂定名", "正式名")
            assert g is not None and g.name == "正式名" and g.slug == "正式名", msg
            assert g.name_manual is True
            assert not (discovery.RAW_DIR / "暂定名").exists()
            assert (discovery.RAW_DIR / "正式名" / "maps").is_dir()
            meta = json.loads((discovery.RAW_DIR / "正式名" / "meta.json")
                              .read_text(encoding="utf-8"))
            assert meta["slug"] == "正式名" and meta["name"] == "正式名", meta
            idx = json.loads((discovery.WIKI_DIR / "正式名" / "index.json")
                             .read_text(encoding="utf-8"))
            assert idx["slug"] == "正式名" and idx["name"] == "正式名", idx
            assert (discovery.RUNTIME_DIR / "event_summary" / "正式名").is_dir()
            gs = discovery.load_games()
            assert "暂定名" in gs[0].aliases, gs[0].aliases
            # slug 冲突拒绝（不动任何数据）
            discovery.register([mk("甲", r"D:\a")])
            discovery.register([mk("乙", r"D:\b")])
            g2, msg2 = discovery.rename_game("甲", "乙")
            assert g2 is None and "冲突" in msg2, msg2
            assert (discovery.RAW_DIR / "甲").exists() is False
        finally:
            (discovery.GAMES_FILE, discovery.RAW_DIR, discovery.WIKI_DIR,
             discovery.RUNTIME_DIR) = _orig
    print("  rename_game: 改名+目录迁移+meta/index 同步+slug 冲突拒绝 ✓")

    print("[launcher.selftest] 全部通过 ✓")


# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
        return
    if "--make-shortcut" in args:
        ok, msg = create_desktop_shortcut()
        if ok:
            messagebox.showinfo("完成", msg)
        else:
            messagebox.showerror("失败", msg)
        sys.exit(0 if ok else 1)
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
