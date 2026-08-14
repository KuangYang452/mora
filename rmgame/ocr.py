# -*- coding: utf-8 -*-
"""OCR 兜底 —— rmgame/ocr（M4）

职责（设计文档 §4.4 / R4）：
- find_game_window：按游戏可执行文件完整路径定位前台窗口（ctypes + Win32 API）
- capture_window：GetWindowRect → PIL ImageGrab 截取窗口画面
- ocr_image：OCR 引擎抽象 —— 默认 Windows.Media.Ocr（经 winsdk/winrt 包），
  未安装时抛 OcrUnavailableError（调用方可降级/提示）
- ocr_game_text：组合流程（定位 → 截屏 → 识别 → 文本）

依赖：ctypes（标准库）+ Pillow（项目已有）；OCR 引擎为可选依赖
（pip install winsdk），未安装不影响其余功能与离线自测（自测注入 fake）。
"""

import ctypes
import ctypes.wintypes as wt
from pathlib import Path

from PIL import Image, ImageGrab

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class OcrUnavailableError(RuntimeError):
    """OCR 引擎不可用（未安装 winsdk / 系统无中文 OCR 包等）。"""


# ---------------------------------------------------------------------------
# 窗口定位
# ---------------------------------------------------------------------------

def _process_path(pid: int) -> str:
    """进程可执行文件完整路径（QueryFullProcessImageNameW）。

    Windows 7+ 该 API 由 kernel32 导出（psapi 为转发 DLL，ctypes
    惰性加载可能取不到），优先 kernel32，psapi 兜底。
    """
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        func = getattr(kernel32, "QueryFullProcessImageNameW", None) \
            or getattr(psapi, "QueryFullProcessImageNameW", None)
        if func is not None and func(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def _cmdline_pids_contain(game_dir: str) -> set:
    """旁路启动：枚举 nw/Game 进程命令行，收集含游戏目录的 pid。

    旁路模式（launch_mode=bypass）下游戏进程是 nw.exe，命令行形如
    `nw.exe <游戏目录> --remote-debugging-port=...` —— 按目录匹配。
    """
    pids = set()
    if not game_dir:
        return pids
    try:
        import json
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='nw.exe' OR Name='Game.exe'\" | "
             "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=10)
        data = json.loads(out.stdout or "[]")
        items = data if isinstance(data, list) else [data] if data else []
        for it in items:
            if game_dir.lower() in str(it.get("CommandLine") or "").lower():
                try:
                    pids.add(int(it["ProcessId"]))
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass
    return pids


def find_game_window(exe_path: str, game_dir: str = None):
    """查找游戏主窗口句柄；未找到返回 None。

    枚举顶层窗口 → 取进程 pid → 先按可执行文件完整路径匹配（区分同名
    Game.exe）；再按游戏目录匹配旁路启动的 nw.exe 进程（命令行含目录）。
    """
    target = exe_path.lower()
    dir_pids = _cmdline_pids_contain(game_dir) if game_dir else set()
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _process_path(pid.value).lower() == target \
                or pid.value in dir_pids:
            found.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else None


# ---------------------------------------------------------------------------
# 截屏
# ---------------------------------------------------------------------------

gdi32 = ctypes.windll.gdi32

PW_RENDERFULLCONTENT = 2


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


def _capture_printwindow(hwnd) -> Image.Image:
    """PrintWindow 捕获目标窗口（可穿透遮挡层，拿到窗口自身内容）。"""
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError("GetWindowRect 失败")
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise OSError(f"窗口尺寸异常: {w}x{h}")

    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
    old = gdi32.SelectObject(mem_dc, bmp)
    try:
        if not user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT):
            user32.PrintWindow(hwnd, mem_dc, 0)  # 回退基本模式
        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h            # 负 = 自顶向下
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0        # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)\
            .convert("RGB")
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)


def capture_window(hwnd) -> Image.Image:
    """截取窗口画面 → PIL Image。

    PrintWindow 优先（可捕获被遮挡窗口，文档 R4 改进）；失败时回退
    GetWindowRect + ImageGrab（前台窗口场景）。
    """
    try:
        return _capture_printwindow(hwnd)
    except Exception:
        rect = wt.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise OSError("GetWindowRect 失败")
        box = (rect.left, rect.top, rect.right, rect.bottom)
        if box[2] <= box[0] or box[3] <= box[1]:
            raise OSError(f"窗口尺寸异常: {box}")
        return ImageGrab.grab(bbox=box)


# ---------------------------------------------------------------------------
# OCR 引擎
# ---------------------------------------------------------------------------

def ocr_image_windows(img: Image.Image) -> str:
    """Windows.Media.Ocr 引擎（经 winsdk 包调用 WinRT API）。

    需要 `pip install winsdk`；系统需安装中文 OCR 语言包。
    中文引擎可用时返回识别文本；失败抛 OcrUnavailableError。
    """
    try:
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.globalization import Language
        from winsdk.windows.graphics.imaging import BitmapDecoder
        from winsdk.windows.storage.streams import (
            InMemoryRandomAccessStream, DataWriter)
    except ImportError:
        raise OcrUnavailableError(
            "Windows OCR 需要 winsdk 包：pip install winsdk")
    import io

    def _find_engine():
        for lang in ("zh-Hans-CN", "zh-CN", "en-US"):
            try:
                eng = OcrEngine.try_create_from_language(Language(lang))
                if eng is not None:
                    return eng
            except Exception:
                continue
        return OcrEngine.try_create_from_user_profile_languages()

    engine = _find_engine()
    if engine is None:
        raise OcrUnavailableError("系统没有可用的 OCR 语言（需中文语言包）")

    # PIL Image → PNG bytes → 内存流 → BitmapDecoder → SoftwareBitmap
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    data = buf.getvalue()

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(data)
    writer.store_async().get()
    stream.seek(0)

    decoder = BitmapDecoder.create_async(stream).get()
    bitmap = decoder.get_software_bitmap_async().get()
    result = engine.recognize_async(bitmap).get()
    return (result.text or "").strip()


def _find_tesseract_cmd() -> str:
    """定位 tesseract.exe：PATH 优先，回退常见安装路径。"""
    import shutil
    exe = shutil.which("tesseract")
    if exe:
        return exe
    for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if Path(p).exists():
            return p
    return "tesseract"  # 交 pytesseract 报错


def ocr_image_tesseract(img: Image.Image, lang: str = "chi_sim+eng") -> str:
    """Tesseract OCR 引擎（pytesseract + tesseract.exe）。

    需要 `pip install pytesseract` + 安装 Tesseract OCR（含中文语言包
    chi_sim）。失败抛 OcrUnavailableError。
    """
    try:
        import pytesseract
    except ImportError:
        raise OcrUnavailableError(
            "Tesseract OCR 需要 pytesseract：pip install pytesseract")
    try:
        pytesseract.pytesseract.tesseract_cmd = _find_tesseract_cmd()
        return (pytesseract.image_to_string(img, lang=lang) or "").strip()
    except Exception as exc:
        raise OcrUnavailableError(f"Tesseract 不可用: {exc}")


def ocr_image(img: Image.Image, engine: str = "auto") -> str:
    """OCR 引擎分发；统一抛 OcrUnavailableError。

    auto 优先 Tesseract（无编译依赖、支持中文）；windows 为
    Windows.Media.Ocr（需 winsdk，Python 3.13 暂不可用）。
    """
    if engine in ("auto", "tesseract"):
        return ocr_image_tesseract(img)
    if engine == "windows":
        return ocr_image_windows(img)
    raise OcrUnavailableError(f"未知 OCR 引擎: {engine}")


# ---------------------------------------------------------------------------
# 组合流程
# ---------------------------------------------------------------------------

def ocr_game_text(game, engine: str = "auto") -> str:
    """定位游戏窗口 → 截屏 → OCR → 文本。

    任一环节失败抛异常（调用方决定如何降级/提示）。
    """
    hwnd = find_game_window(game.exe_path, game_dir=game.dir)
    if hwnd is None:
        raise OSError(f"未找到游戏窗口: {game.exe_path}")
    img = capture_window(hwnd)
    return ocr_image(img, engine=engine)
