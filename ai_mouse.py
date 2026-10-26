# -*- coding: utf-8 -*-
"""ai_mouse —— AI 专用的"第二只鼠标"。

原理：Windows 官方输入注入通道（Win10 1809+，无驱动、无硬件依赖）
    CreateSyntheticPointerDevice(PT_PEN, 1, ...)   → 创建一只"合成笔"
    InjectSyntheticPointerInput(device, info, n)   → 注入移动/按下/抬起事件

目标特性：
  · 与物理鼠标互不干扰：使用独立的合成指针，不需要移动系统光标（selftest 实测验证）
  · 事件直达坐标下的窗口：以 WM_POINTER 系列消息送达（传统窗口由系统自动"提升"为鼠标消息）
  · 网络上的官方通道：微软为输入注入设计的正规 API，不是钩子或驱动

已知局限（v1）：
  · 官方设备类型只支持 PT_TOUCH / PT_PEN，没有 PT_MOUSE → 本工具用"笔"语义模拟鼠标
  · 笔没有滚轮语义 → v1 不支持滚轮
  · 右键为实验性（笔没有原生右键）
  · 对"只读系统光标位置"的老程序可能无效——这类程序需走其他通道

实测结论（2026-10-14，本机 Win11 build 26300，完整证据见 _selftest*_result.json）：
  · 对现代应用（浏览器等会处理 WM_POINTER 的程序）：事件全链路送达
    （pointerover/move/down/up + 派生 click），物理光标零位移——完全隔离 ✓
  · 对传统窗口（不处理 WM_POINTER 的程序）：系统"鼠标提升"机制会同时
    发送指针消息与鼠标消息，物理光标被瞬时搬到注入点——操作有效，隔离打折
  · 跨进程可达：可直接操作其他应用的窗口
  · 前提：目标窗口需处于前台（click 默认内置自动激活）

安全授权（授权闸门，2026-10-14 接入；2026-10-24 支持独立运行）：
  · 动键鼠前必须先拿到【人】的授权——这是设计红线，不是可选项。
  · 两种闸门，按环境自动选择：
      ① 桌宠闸门 gate.py：在 hud/gate.py 或 %AIM_HUD_DIR%/gate.py，
         弹卡片让人点「✅ 允许 / ⛔ 拒绝」，授权落 consent.json；
      ② 内置 SimpleConsent：找不到 gate.py 时，在终端问一次 y/N，
         仅本进程有效、不落任何文件。
  · 强制指定：AIM_GATE=hud（严格：找不到 gate.py 直接报错中止）
              AIM_GATE=simple（强制命令行确认）
  · 创建 SyntheticMouse 时自动过闸门：未授权会【中止】。
  · AI 绝不允许自己写 consent.json 去授权；干完活应主动 revoke()
    （撤销属失败安全方向，允许 AI 做）。
  · 只读命令（consent / pos / find / monitors / doctor）不需要授权。

坐标约定：屏幕物理像素坐标（虚拟屏幕左上角为原点），与 GetCursorPos 同一坐标系。

命令行用法（除 consent / pos / find 外，均需先获授权）：
    python ai_mouse.py consent                     # 查看授权状态（只读）
    python ai_mouse.py find "记事本"                # 按标题找窗口 hwnd（只读）
    python ai_mouse.py move 800 500                # 悬停移动到 (800, 500)
    python ai_mouse.py click 800 500               # 移动到 (800,500) 并单击
    python ai_mouse.py click 800 500 --hwnd 1234   # 指定目标窗口（推荐）
    python ai_mouse.py drag 300 300 900 600        # 从 (300,300) 拖到 (900,600)
    python ai_mouse.py script "move 100 100; click; move 400 400; dblclick"
    python ai_mouse.py selftest                    # 全自动自检
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes as wt

# ============================ 基础绑定 ============================

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------- 常量 ----------

# POINTER_INPUT_TYPE
PT_POINTER = 0x00000001
PT_TOUCH = 0x00000002
PT_PEN = 0x00000003
PT_MOUSE = 0x00000004
PT_TOUCHPAD = 0x00000005

# POINTER_FEEDBACK_MODE（CreateSyntheticPointerDevice 的视觉反馈模式）
POINTER_FEEDBACK_DEFAULT = 0x00000001
POINTER_FEEDBACK_INDIRECT = 0x00000002
POINTER_FEEDBACK_NONE = 0x00000003

# POINTER_FLAGS
POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_FIRSTBUTTON = 0x00000010
POINTER_FLAG_SECONDBUTTON = 0x00000020
POINTER_FLAG_PRIMARY = 0x00002000
POINTER_FLAG_CONFIDENCE = 0x00004000
POINTER_FLAG_DOWN = 0x00010000
POINTER_FLAG_UPDATE = 0x00020000
POINTER_FLAG_UP = 0x00040000

# PEN_MASK
PEN_MASK_NONE = 0x00000000
PEN_MASK_PRESSURE = 0x00000001
PEN_MASK_ROTATION = 0x00000002
PEN_MASK_TILT_X = 0x00000004
PEN_MASK_TILT_Y = 0x00000008

# POINTER_BUTTON_CHANGE_TYPE
POINTER_CHANGE_NONE = 0
POINTER_CHANGE_FIRSTBUTTON_DOWN = 1
POINTER_CHANGE_FIRSTBUTTON_UP = 2
POINTER_CHANGE_SECONDBUTTON_DOWN = 3
POINTER_CHANGE_SECONDBUTTON_UP = 4

# 消息名映射（记录/调试用）
POINTER_MSG_NAMES = {
    0x0238: "WM_POINTERDEVICECHANGE",
    0x0239: "WM_POINTERDEVICEINRANGE",
    0x023A: "WM_POINTERDEVICEOUTOFRANGE",
    0x0241: "WM_NCPOINTERUPDATE",
    0x0242: "WM_NCPOINTERDOWN",
    0x0243: "WM_NCPOINTERUP",
    0x0245: "WM_POINTERUPDATE",
    0x0246: "WM_POINTERDOWN",
    0x0247: "WM_POINTERUP",
    0x0249: "WM_POINTERENTER",
    0x024A: "WM_POINTERLEAVE",
    0x024B: "WM_POINTERACTIVATE",
    0x024C: "WM_POINTERCAPTURECHANGED",
    0x024E: "WM_POINTERWHEEL",
    0x024F: "WM_POINTERHWHEEL",
}

MOUSE_MSG_NAMES = {
    0x0200: "WM_MOUSEMOVE",
    0x0201: "WM_LBUTTONDOWN",
    0x0202: "WM_LBUTTONUP",
    0x0204: "WM_RBUTTONDOWN",
    0x0205: "WM_RBUTTONUP",
    0x0207: "WM_MBUTTONDOWN",
    0x0208: "WM_MBUTTONUP",
    0x020A: "WM_MOUSEWHEEL",
}


def msg_name(code: int) -> str:
    if code in POINTER_MSG_NAMES:
        return POINTER_MSG_NAMES[code]
    if code in MOUSE_MSG_NAMES:
        return MOUSE_MSG_NAMES[code]
    return f"0x{code:04X}"


# ---------- 结构体 ----------

class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wt.LONG),
        ("top", wt.LONG),
        ("right", wt.LONG),
        ("bottom", wt.LONG),
    ]


class POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType", wt.DWORD),          # POINTER_INPUT_TYPE
        ("pointerId", wt.DWORD),
        ("frameId", wt.DWORD),
        ("pointerFlags", wt.DWORD),         # POINTER_FLAGS
        ("sourceDevice", wt.HANDLE),
        ("hwndTarget", wt.HWND),
        ("ptPixelLocation", POINT),
        ("ptHimetricLocation", POINT),
        ("ptPixelLocationRaw", POINT),
        ("ptHimetricLocationRaw", POINT),
        ("dwTime", wt.DWORD),
        ("historyCount", wt.DWORD),
        ("InputData", ctypes.c_int32),
        ("dwKeyStates", wt.DWORD),
        ("PerformanceCount", ctypes.c_uint64),
        ("ButtonChangeType", ctypes.c_uint),  # POINTER_BUTTON_CHANGE_TYPE
    ]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("touchFlags", wt.DWORD),
        ("touchMask", wt.DWORD),
        ("rcContact", RECT),
        ("rcContactRaw", RECT),
        ("orientation", wt.DWORD),
        ("pressure", wt.DWORD),
    ]


class POINTER_PEN_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("penFlags", wt.DWORD),
        ("penMask", wt.DWORD),
        ("pressure", wt.DWORD),
        ("rotation", wt.DWORD),
        ("tiltX", ctypes.c_int32),
        ("tiltY", ctypes.c_int32),
    ]


class _PointerTypeUnion(ctypes.Union):
    # 与 winuser.h 中 POINTER_TYPE_INFO 的 union 对应（含 touchInfo 保证整体尺寸一致）
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("touchInfo", POINTER_TOUCH_INFO),
        ("penInfo", POINTER_PEN_INFO),
    ]


class POINTER_TYPE_INFO(ctypes.Structure):
    _fields_ = [
        ("type", wt.DWORD),                 # POINTER_INPUT_TYPE
        ("u", _PointerTypeUnion),
    ]


# ---------- API 原型 ----------

user32.CreateSyntheticPointerDevice.argtypes = [wt.DWORD, ctypes.c_ulong, wt.DWORD]
user32.CreateSyntheticPointerDevice.restype = wt.HANDLE

user32.InjectSyntheticPointerInput.argtypes = [
    wt.HANDLE,
    ctypes.POINTER(POINTER_TYPE_INFO),
    wt.UINT,
]
user32.InjectSyntheticPointerInput.restype = wt.BOOL

user32.DestroySyntheticPointerDevice.argtypes = [wt.HANDLE]
user32.DestroySyntheticPointerDevice.restype = None

user32.GetPointerInfo.argtypes = [wt.UINT, ctypes.POINTER(POINTER_INFO)]
user32.GetPointerInfo.restype = wt.BOOL

# 窗口辅助（激活目标窗口用）
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.AttachThreadInput.restype = wt.BOOL
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
kernel32.GetCurrentThreadId.restype = wt.DWORD

GA_ROOT = 2

EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindow.restype = wt.BOOL
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wt.BOOL
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(POINT)]
user32.ClientToScreen.restype = wt.BOOL


def find_windows(title_substr: str = "", class_name: str = ""):
    """枚举顶层可见窗口，按标题子串 / 类名过滤，返回 [hwnd, ...]。

    推荐用法：先找到目标窗口的 hwnd，再 click(..., hwnd=hwnd)——
    比"从坐标反查窗口"可靠（坐标可能被其他窗口遮挡而反查到错的窗口）。
    """
    result = []

    def cb(hwnd, lp):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if class_name:
                cbuf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cbuf, 256)
                if cbuf.value != class_name:
                    return True
            if title_substr:
                tbuf = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, tbuf, 512)
                if title_substr not in tbuf.value:
                    return True
            result.append(int(hwnd))
        except Exception:
            pass
        return True

    ref = EnumWindowsProc(cb)
    user32.EnumWindows(ref, 0)
    return result


def window_from_point(x: int, y: int):
    """取屏幕坐标点下的窗口句柄。"""
    return user32.WindowFromPoint(POINT(int(x), int(y)))


def root_window(hwnd) -> int:
    """取窗口的顶层祖先。"""
    if not hwnd:
        return 0
    return int(user32.GetAncestor(hwnd, GA_ROOT) or hwnd)


def activate_window(hwnd) -> tuple:
    """把窗口带到前台（AttachThreadInput 技巧，绕过前台锁定限制）。

    返回 (调用是否成功, 现在是否前台)。实测：现代应用（如 Chrome）
    必须处于前台才接收指针输入，注入前需要先激活目标窗口。
    """
    if not hwnd:
        return False, False
    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)
    ok = False
    try:
        if fg_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, True)
        if tgt_tid:
            user32.AttachThreadInput(cur_tid, tgt_tid, True)
        user32.BringWindowToTop(hwnd)
        ok = bool(user32.SetForegroundWindow(hwnd))
    finally:
        if tgt_tid:
            user32.AttachThreadInput(cur_tid, tgt_tid, False)
        if fg_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
    return ok, (user32.GetForegroundWindow() == hwnd)


# ============================ 授权闸门 ============================
# 设计红线：动键鼠前必须有人点头。闸门只决定"怎么问人"，不决定"要不要问"。
# 创建设备即视为"要开始控制电脑"——未授权一律中止（fail-closed，失败安全方向）。

GATE = None

GATE_ENV = "AIM_GATE"    # auto（默认）| hud（严格）| simple（强制命令行确认）
HUD_ENV = "AIM_HUD_DIR"  # 指向含 gate.py 的目录


class AiMouseError(RuntimeError):
    """工具的可预期失败（窗口校验 / 前台复核等），消息可以直接给人看。"""


class SimpleConsent:
    """无桌宠环境下的内置授权闸门（命令行 y/N）。

    接口与 hud/gate.py 保持一致（require / granted / describe / revoke），
    语义也保持一致：**每次进程启动都要人点一次头**。
    不落盘、不产生 consent.json —— 所以不存在"AI 自己给自己发许可证"的路径；
    非交互环境（stdin 不是终端）一律拒绝，宁严不松。
    """

    _granted = False
    _what = ""

    class NoConsent(Exception):
        """未获授权。类名会被 CLI 识别（退出码 4）。"""

    @classmethod
    def describe(cls):
        if cls._granted:
            return f"已授权（SimpleConsent：本进程有效，用途「{cls._what or 'AI 鼠标操作'}」）"
        return "未授权（SimpleConsent：注入前会在本终端询问 y/N）"

    @classmethod
    def require(cls, scope="gui", what=""):
        if cls._granted:
            return True
        try:
            interactive = sys.stdin is not None and sys.stdin.isatty()
        except Exception:
            interactive = False
        if not interactive:
            raise cls.NoConsent(
                "未授权：当前不是交互终端，无法完成人工确认。"
                "请在终端里手动运行一次；或接桌宠闸门（AIM_GATE=hud + AIM_HUD_DIR）。"
            )
        print("")
        print("=" * 62)
        print("  AI 鼠标请求控制这台电脑的指针")
        print(f"  用途：{what or 'AI 鼠标操作'}")
        print("  动作：移动 / 单击 / 双击 / 拖拽（合成笔通道，不搬动你的光标）")
        print("=" * 62)
        try:
            ans = input("  允许？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("y", "yes"):
            raise cls.NoConsent("已拒绝授权，操作已中止（未注入任何事件）。")
        cls._granted = True
        cls._what = what
        return True

    @classmethod
    def granted(cls, scope="gui"):
        return bool(cls._granted)

    @classmethod
    def revoke(cls):
        cls._granted = False
        return True


def _load_gate():
    """加载授权闸门（三级优先，永不静默放行）。

    1. AIM_GATE=simple → 直接用内置 SimpleConsent
    2. 依次找 gate.py：$AIM_HUD_DIR/gate.py → ../hud/gate.py → 旧版硬编码路径
    3. 都找不到：AIM_GATE=hud 时**报错中止**；否则退化为 SimpleConsent（仍要人点头）
    """
    global GATE
    if GATE is not None:
        return GATE

    mode = (os.environ.get(GATE_ENV) or "auto").strip().lower()
    if mode == "simple":
        GATE = SimpleConsent
        return GATE

    import importlib.util
    import pathlib
    here = pathlib.Path(__file__).resolve().parent
    cands = []
    hud_env = (os.environ.get(HUD_ENV) or "").strip()
    if hud_env:
        cands.append(pathlib.Path(hud_env) / "gate.py")
    cands.append(here.parent / "hud" / "gate.py")
    cands.append(pathlib.Path(r"D:\workbuddy-gui\hud\gate.py"))

    for p in cands:
        try:
            if not p.is_file():
                continue
            spec = importlib.util.spec_from_file_location("hud_gate", str(p))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            GATE = mod
            return GATE
        except Exception:
            continue  # 坏了就换下一个候选，别让加载异常变成"放行"

    if mode == "hud":
        raise AiMouseError(
            "未找到控制授权闸门 gate.py（已查找：%s）。"
            "当前 AIM_GATE=hud 要求必须接桌宠闸门，已中止。"
            % "、".join(str(p) for p in cands)
        )

    GATE = SimpleConsent
    return GATE


def require_consent(what: str = "AI 鼠标操作"):
    """要求 gui 授权；未授权抛 NoConsent（同时自动向桌宠发申请）。"""
    return _load_gate().require("gui", what)


def gate_status() -> str:
    """人类可读的当前授权状态（只读查询，不需要授权）。"""
    try:
        return _load_gate().describe()
    except Exception as e:
        return f"（授权闸门不可用：{e}）"


# ---------- 辅助函数 ----------

DPI_MODE = "unset"


def set_dpi_aware() -> str:
    """把当前进程设为 DPI 感知（保证屏幕坐标与物理像素 1:1，不受 150% 缩放干扰）。

    必须在创建窗口、查询坐标之前调用，进程级一次生效。
    """
    global DPI_MODE
    if DPI_MODE != "unset":
        return DPI_MODE
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            DPI_MODE = "per-monitor-v2"
            return DPI_MODE
    except Exception:
        pass
    try:
        # DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -2
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-2)):
            DPI_MODE = "system"
            return DPI_MODE
    except Exception:
        pass
    DPI_MODE = "none"
    return DPI_MODE


def get_cursor_pos():
    """读取系统（物理鼠标）光标位置。"""
    pt = POINT()
    if user32.GetCursorPos(ctypes.byref(pt)):
        return [pt.x, pt.y]
    return None


def list_monitors():
    """枚举所有显示器（物理坐标；进程已 DPI 感知，多屏/混合缩放友好）。

    返回 {"count": n, "virtual": [x, y, w, h], "monitors": [
        {"device", "rect": [l,t,r,b], "work": [l,t,r,b], "primary": bool,
         "dpi": int|0, "scale_pct": int|None, "size": [w, h]} ...]}
    ——副屏适配的基础设施（接屏后用 `python ai_mouse.py monitors` 查看）。
    """
    set_dpi_aware()
    monitors = []

    MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.POINTER(RECT), wt.LPARAM)

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                    ("dwFlags", wt.DWORD), ("szDevice", wt.WCHAR * 32)]

    def _cb(hmon, hdc, lprc, lparam):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            dpi = 0
            try:
                shcore = ctypes.windll.shcore
                shcore.GetDpiForMonitor.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                    ctypes.POINTER(ctypes.c_uint),
                                                    ctypes.POINTER(ctypes.c_uint)]
                dx, dy_ = ctypes.c_uint(0), ctypes.c_uint(0)
                if shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy_)) == 0:
                    dpi = int(dx.value)     # MDT_EFFECTIVE_DPI
            except Exception:
                pass
            r = mi.rcMonitor
            w = mi.rcWork
            monitors.append({
                "device": mi.szDevice,
                "rect": [r.left, r.top, r.right, r.bottom],
                "work": [w.left, w.top, w.right, w.bottom],
                "primary": bool(mi.dwFlags & 1),
                "dpi": dpi,
                "scale_pct": round(dpi / 96 * 100) if dpi else None,
                "size": [r.right - r.left, r.bottom - r.top],
            })
        return True

    try:
        user32.EnumDisplayMonitors.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT),
                                               MONITORENUMPROC, wt.LPARAM]
        user32.EnumDisplayMonitors.restype = wt.BOOL
    except Exception:
        pass
    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    return {
        "count": len(monitors),
        "virtual": [user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
                    user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)],
        "monitors": monitors,
    }


def get_pointer_info(pointer_id: int):
    """在处理 WM_POINTER 消息时，读取指针详情（坐标/标志/按键变化）。"""
    pi = POINTER_INFO()
    if user32.GetPointerInfo(pointer_id, ctypes.byref(pi)):
        return {
            "flags": pi.pointerFlags,
            "flags_hex": hex(pi.pointerFlags),
            "x": pi.ptPixelLocation.x,
            "y": pi.ptPixelLocation.y,
            "btn_change": pi.ButtonChangeType,
            "type": pi.pointerType,
            "hwnd": int(pi.hwndTarget or 0),
        }
    return None


# ---------- 光标守护（擦除"笔接触吸光标"副作用） ----------

class CursorGuard:
    """守护物理光标：擦除"合成笔接触把光标吸到笔点"的副作用（2026-10-14 实测定案）。

    背景：合成笔的 promotion 机制会把物理光标瞬移到笔点（实测 600~1600px / 7~44ms，
    约每 0.3s 一次）——"AI 一干活，你的鼠标就被抢"。本守护在后台以 ~1.2ms 周期采样
    GetCursorPos，把"人手不可能"的位移判定为瞬移，立即 SetCursorPos 还原到瞬移前位置。
    检测+还原通常 2~6ms 完成（60Hz 屏幕常在一帧之内），用户无感。

    判定（二者之一）：
      · speed —— 相邻采样 ≤15ms 且位移 >200px（= 13,300px/s，远超人手上限 ~5,000px/s）
      · pen   —— 落在笔点 ±30px 且距上次注入 ≤0.2s，且上一采样在笔点 200px 之外

    生命周期随 SyntheticMouse（创建/关闭）；不需要可传 cursor_guard=False。
    统计：yanks（判定数）/ restores（实际还原数）/ log（最近 300 条明细，供审计）。
    """

    JUMP_PX = 200        # 位移阈值（px）
    JUMP_DT = 0.015      # 时间阈值（s）
    PEN_RADIUS = 30      # "落在笔点附近"半径（px）
    PEN_MEMORY = 0.20    # 注入后多久内仍认为"可能被吸"（s）

    def __init__(self):
        self.yanks = 0
        self.restores = 0
        self.samples = 0
        self.log = []            # (e, dt, from(x,y), to(x,y), cause)
        self._pen_xy = None
        self._pen_t = 0.0
        self._prev = None
        self._prev_t = 0.0
        self._stop = False
        self._th = None
        self._fine_timer = False
        try:
            user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
            user32.GetCursorPos.restype = wt.BOOL
            user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
            user32.SetCursorPos.restype = wt.BOOL
        except Exception:
            pass

    def start(self):
        if self._th is not None:
            return
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)   # 让 ~1ms 级 sleep 生效
            self._fine_timer = True
        except Exception:
            self._fine_timer = False
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def stop(self):
        self._stop = True
        if self._fine_timer:
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass
            self._fine_timer = False

    def note_injection(self, x, y):
        """每次注入后调用：记录笔点（供"落点关联"判定用）。"""
        self._pen_xy = (int(x), int(y))
        self._pen_t = time.perf_counter()

    @staticmethod
    def _d(a, b):
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

    def _loop(self):
        while not self._stop:
            p = POINT(0, 0)
            if not user32.GetCursorPos(ctypes.byref(p)):
                time.sleep(0.002)
                continue
            t = time.perf_counter()
            cur = (p.x, p.y)
            if self._prev is not None:
                dt = t - self._prev_t
                d = self._d(cur, self._prev)
                cause = None
                if dt <= self.JUMP_DT and d > self.JUMP_PX:
                    cause = "speed"
                elif (self._pen_xy is not None
                      and (t - self._pen_t) <= self.PEN_MEMORY
                      and self._d(cur, self._pen_xy) <= self.PEN_RADIUS
                      and self._d(self._prev, self._pen_xy) > self.JUMP_PX):
                    cause = "pen"
                if cause:
                    self.yanks += 1
                    if user32.SetCursorPos(self._prev[0], self._prev[1]):
                        self.restores += 1
                        if len(self.log) < 300:
                            self.log.append((round(t, 4), round(dt, 5),
                                             self._prev, cur, cause))
                        cur = self._prev
            self._prev = cur
            self._prev_t = t
            self.samples += 1
            time.sleep(0.0012)


# ---------- 核心类 ----------

class SyntheticMouse:
    """AI 的鼠标：基于官方"合成笔指针"的独立输入通道。

    用法：
        with SyntheticMouse() as m:
            m.move(500, 300)
            m.click(500, 300)
    """

    def __init__(self, feedback: int = POINTER_FEEDBACK_DEFAULT, pressure: int = 400,
                 purpose: str = "AI 鼠标操作", cursor_guard: bool = True):
        require_consent(purpose)   # ← 未授权直接中止（同时自动向桌宠发申请）
        set_dpi_aware()
        self._pressure = int(pressure)
        # 光标守护：擦除"笔接触把物理光标吸到笔点"的副作用（2026-10-14 实测）；
        # 不需要可传 cursor_guard=False
        self._guard = CursorGuard() if cursor_guard else None
        if self._guard is not None:
            self._guard.start()
        self._device = user32.CreateSyntheticPointerDevice(PT_PEN, 1, feedback)
        if not self._device:
            err = ctypes.get_last_error()
            raise OSError(
                f"CreateSyntheticPointerDevice 失败（GetLastError={err}）"
                f"——请确认系统为 Win10 1809+，且 feedback 参数合法"
            )

    # ---- 生命周期 ----
    def close(self):
        # ★ 2026-10-15 修复（用户实战反馈"鼠标还被抢"，证据：退出瞬间漏网）：
        #   原顺序 = 先停守护、再销毁设备——而"设备销毁瞬间"可能触发最后一次
        #   promotion（笔消失把光标最后拽一下），此时守护已下班 → 漏网。
        #   新顺序 = 先销毁设备 → 守护继续站岗 1.5 秒收拾残局 → 最后下班。
        if self._device:
            user32.DestroySyntheticPointerDevice(self._device)
            self._device = None
        if self._guard is not None:
            time.sleep(1.5)        # 设备已消失，守护仍在跑（promotion 是 ms 级，1.5s 富余）
            self._guard.stop()
            self._guard = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 底层注入 ----
    def _emit(self, x: int, y: int, flags: int,
              button_change: int = POINTER_CHANGE_NONE,
              in_contact: bool = False):
        info = POINTER_TYPE_INFO()
        info.type = PT_PEN
        p = info.u.penInfo
        pi = p.pointerInfo
        pi.pointerType = PT_PEN
        pi.pointerId = 0
        pi.frameId = 0
        pi.pointerFlags = flags
        pi.ptPixelLocation = POINT(int(x), int(y))
        pi.ptPixelLocationRaw = POINT(int(x), int(y))
        pi.dwTime = 0
        pi.ButtonChangeType = button_change
        p.penFlags = 0
        p.penMask = PEN_MASK_PRESSURE
        p.pressure = self._pressure if in_contact else 0
        ok = user32.InjectSyntheticPointerInput(self._device, ctypes.byref(info), 1)
        if not ok:
            err = ctypes.get_last_error()
            raise OSError(f"InjectSyntheticPointerInput 失败（GetLastError={err}），目标=({x},{y}) flags=0x{flags:X}")
        if self._guard is not None:
            self._guard.note_injection(x, y)   # 供光标守护做"落点关联"判定

    # ---- 目标窗口解析 / 激活复核 ----
    @staticmethod
    def _resolve_target(x: int, y: int, hwnd=None, verify_target: bool = True) -> int:
        """解析目标窗口；显式给 hwnd 时校验注入点确实落在该窗口内。

        校验必要性（实测教训）：注入点可能被其他窗口遮挡、或坐标写错，
        WindowFromPoint 会反查到"错的窗口"——不校验就会静默点到无关窗口。
        """
        if hwnd:
            target = root_window(int(hwnd))
            if verify_target:
                under = root_window(window_from_point(x, y))
                if under != target:
                    raise AiMouseError(
                        f"注入点 ({x}, {y}) 下的窗口(hwnd={under})与目标窗口"
                        f"(hwnd={target})不一致——可能被其他窗口遮挡，或坐标不在目标窗口内。"
                        f"已中止（未发送任何事件）。请调整坐标，或用 find 重新取 hwnd。"
                    )
            return target
        return int(root_window(window_from_point(x, y)) or 0)

    def _ensure_foreground(self, target: int, tries: int = 3, wait: float = 0.2) -> bool:
        """把 target 带到前台并复核；复核不过绝不注入（实测教训）。"""
        for _ in range(max(1, tries)):
            _, is_fg = activate_window(target)
            if is_fg:
                return True
            time.sleep(wait)
        return bool(target) and user32.GetForegroundWindow() == target

    def _prepare_target(self, x: int, y: int, activate: bool, hwnd=None,
                        verify_target: bool = True) -> int:
        """解析（并校验）目标窗口；activate 时激活并**复核前台**，复核不过抛错中止。

        返回 target hwnd（可能为 0：未给 hwnd 且未激活时无从确定，调用方自负）。
        """
        target = self._resolve_target(x, y, hwnd=hwnd, verify_target=verify_target) \
            if (hwnd or activate) else 0
        if activate:
            if not target:
                raise AiMouseError(
                    f"无法确定 ({x}, {y}) 下的目标窗口，已中止。"
                    f"请用 find 找到目标窗口后传 hwnd=。"
                )
            if not self._ensure_foreground(target):
                fg = int(user32.GetForegroundWindow() or 0)
                raise AiMouseError(
                    f"目标窗口(hwnd={target})未能带到前台（当前前台 hwnd={fg}）——"
                    f"可能被其他窗口或用户操作抢占。已中止（未发送任何事件）。"
                )
            time.sleep(0.15)
            if user32.GetForegroundWindow() != target:
                raise AiMouseError("注入前一刻前台被抢占，已中止（未发送任何事件）。")
        return target

    # ---- 动作 ----
    def move(self, x: int, y: int):
        """悬停移动（不按下）——笔的 hover 语义。"""
        self._emit(x, y, POINTER_FLAG_INRANGE | POINTER_FLAG_UPDATE)

    def down(self, x: int, y: int, button: str = "left"):
        """主键按下（笔尖接触）。button: left | right（right 为实验性）。"""
        if button == "right":
            self._emit(
                x, y,
                POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT | POINTER_FLAG_DOWN | POINTER_FLAG_SECONDBUTTON,
                button_change=POINTER_CHANGE_SECONDBUTTON_DOWN,
                in_contact=True,
            )
        else:
            self._emit(
                x, y,
                POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT | POINTER_FLAG_DOWN | POINTER_FLAG_FIRSTBUTTON,
                button_change=POINTER_CHANGE_FIRSTBUTTON_DOWN,
                in_contact=True,
            )

    def up(self, x: int, y: int, button: str = "left"):
        """抬起。"""
        if button == "right":
            self._emit(
                x, y,
                POINTER_FLAG_INRANGE | POINTER_FLAG_UP | POINTER_FLAG_SECONDBUTTON,
                button_change=POINTER_CHANGE_SECONDBUTTON_UP,
            )
        else:
            self._emit(
                x, y,
                POINTER_FLAG_INRANGE | POINTER_FLAG_UP | POINTER_FLAG_FIRSTBUTTON,
                button_change=POINTER_CHANGE_FIRSTBUTTON_UP,
            )

    def click(self, x: int, y: int, button: str = "left", hold: float = 0.05,
              activate: bool = True, hwnd=None, verify_target: bool = True):
        """移动到 (x,y) 并单击。

        activate=True（默认）时先激活目标窗口并**复核前台**，复核不过就报错
        中止——绝不"激活失败还照注入"（实测教训）。
        hwnd 显式指定目标窗口（推荐，最可靠）；verify_target=True 时校验
        注入点确实落在该窗口内（防遮挡 / 防坐标写错）。
        """
        self._prepare_target(x, y, activate=activate, hwnd=hwnd,
                             verify_target=verify_target)
        self.move(x, y)
        time.sleep(0.02)
        self.down(x, y, button)
        time.sleep(hold)
        self.up(x, y, button)
        time.sleep(0.02)
        self.move(x, y)  # 恢复悬停态

    def double_click(self, x: int, y: int, interval: float = 0.12,
                     activate: bool = True, hwnd=None, verify_target: bool = True):
        """双击（激活只在第一次做，但两次都要校验落点）。

        坑（2026-10 实测）：第二次 click 若写 activate=False 且不传 hwnd，
        _prepare_target 里 `if (hwnd or activate)` 两个条件都不成立 → 直接
        return 0，等于**盲注入**：第一次点中目标、第二次飘到别的窗口时不会报错，
        静默点错地方。所以第二次必须把 hwnd/verify_target 原样带下去
        （activate 仍为 False，避免重复抢前台）。
        """
        self.click(x, y, activate=activate, hwnd=hwnd, verify_target=verify_target)
        time.sleep(interval)
        self.click(x, y, activate=False, hwnd=hwnd, verify_target=verify_target)

    def drag(self, x1: int, y1: int, x2: int, y2: int, steps: int = 12,
             button: str = "left", activate: bool = False, hwnd=None,
             verify_target: bool = True):
        """从 (x1,y1) 按住拖到 (x2,y2)。

        默认不激活（保持 v1 行为）；activate=True 或给了 hwnd 时，
        起点会做同样的前台复核 / 目标校验。
        """
        self._prepare_target(x1, y1, activate=activate, hwnd=hwnd,
                             verify_target=verify_target)
        self.move(x1, y1)
        time.sleep(0.02)
        self.down(x1, y1, button)
        time.sleep(0.03)
        for i in range(1, steps + 1):
            t = i / steps
            x = round(x1 + (x2 - x1) * t)
            y = round(y1 + (y2 - y1) * t)
            self._emit(x, y, POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT | POINTER_FLAG_UPDATE,
                       in_contact=True)
            time.sleep(0.012)
        self.up(x2, y2, button)
        time.sleep(0.02)
        self.move(x2, y2)


# ============================ 命令行 ============================

def _exec_script(mouse: SyntheticMouse, text: str):
    """执行分号分隔的动作串（同一进程内连续执行，指针状态连续）。

    支持：move x y | click [x y] | dblclick [x y] | down x y | up x y |
          drag x1 y1 x2 y2 | sleep 秒
    （click/dblclick 不带坐标时，使用最近一次的坐标）
    """
    last = [None]  # 最近坐标

    def _coords(toks):
        if len(toks) >= 2:
            x, y = int(toks[0]), int(toks[1])
            last[0] = (x, y)
            return x, y
        if last[0]:
            return last[0]
        raise SystemExit("click/dblclick 需要坐标（或先执行一次带坐标的动作）")

    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        toks = part.split()
        cmd = toks[0].lower()
        args = toks[1:]
        if cmd == "move":
            x, y = int(args[0]), int(args[1])
            mouse.move(x, y)
            last[0] = (x, y)
        elif cmd == "click":
            x, y = _coords(args)
            mouse.click(x, y)
        elif cmd == "dblclick":
            x, y = _coords(args)
            mouse.double_click(x, y)
        elif cmd == "down":
            x, y = int(args[0]), int(args[1])
            mouse.down(x, y)
            last[0] = (x, y)
        elif cmd == "up":
            x, y = int(args[0]), int(args[1])
            mouse.up(x, y)
            last[0] = (x, y)
        elif cmd == "drag":
            mouse.drag(int(args[0]), int(args[1]), int(args[2]), int(args[3]))
            last[0] = (int(args[2]), int(args[3]))
        elif cmd == "sleep":
            time.sleep(float(args[0]))
        else:
            raise SystemExit(f"未知动作：{cmd}")
        print(f"[ai_mouse] {part} ✓")


def _print_doctor():
    """环境体检（只读，不需要授权）：给"以后直接用"提供一键底账。"""
    import os
    import platform
    print("=" * 56)
    print("AI 鼠标 · 环境体检")
    print("=" * 56)
    print(f"工具位置   : {os.path.abspath(__file__)}")
    print(f"Python     : {sys.version.split()[0]} ({platform.architecture()[0]})")
    print(f"DPI 感知   : {set_dpi_aware()}")
    info = list_monitors()
    mons = "；".join(
        f"{m['device']} {m['size'][0]}x{m['size'][1]}"
        + (f"@{m['scale_pct']}%" if m["scale_pct"] else "")
        + ("(主)" if m["primary"] else "(副)")
        for m in info["monitors"])
    print(f"显示器     : {info['count']} 台  {mons}")
    print(f"虚拟桌面   : x={info['virtual'][0]} y={info['virtual'][1]} "
          f"{info['virtual'][2]}x{info['virtual'][3]}")
    print(f"授权状态   : {gate_status()}")
    print(f"光标守护   : 默认开启（>{CursorGuard.JUMP_PX}px / ≤{CursorGuard.JUMP_DT * 1000:.0f}ms "
          f"判瞬移，~2ms 还原；cursor_guard=False 可关）")
    here = os.path.dirname(os.path.abspath(__file__))
    for label, fn in (("守护验证", "_guard_test2_result.json"),
                      ("副屏实测", "_dualscreen_result.json")):
        p = os.path.join(here, fn)
        if os.path.exists(p):
            print(f"最近{label} : {time.strftime('%m-%d %H:%M', time.localtime(os.path.getmtime(p)))} → {fn}")
    if info["count"] >= 2:
        print(f"副屏       : 已识别 {info['count']} 台 → 可跑 _dualscreen_test.py 实测")
    else:
        print("副屏       : 未接（接上后跑 _dualscreen_test.py）")
    print("-" * 56)
    print('常用: aim.cmd find "窗口标题" ｜ aim.cmd click X Y --hwnd N ｜ aim.cmd demo')
    print("      （一切注入动作需先在桌宠点「✅ 允许」；详见 README.md）")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ai_mouse",
        description="AI 鼠标：Windows 合成笔指针注入工具（与物理鼠标互不干扰）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("move", help="悬停移动到坐标")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)

    p = sub.add_parser("click", help="移动到坐标并单击")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--button", default="left", choices=["left", "right"])
    p.add_argument("--no-activate", action="store_true",
                   help="不把目标窗口带到前台（默认会先激活并复核）")
    p.add_argument("--hwnd", type=int, default=None,
                   help="目标窗口句柄（推荐：比坐标反查可靠）")
    p.add_argument("--no-verify", action="store_true",
                   help="跳过'注入点必须在目标窗口内'校验（坐标被遮挡时慎用）")

    p = sub.add_parser("dblclick", help="双击")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--no-activate", action="store_true",
                   help="不把目标窗口带到前台（默认会先激活并复核）")
    p.add_argument("--hwnd", type=int, default=None,
                   help="目标窗口句柄（推荐：比坐标反查可靠）")
    p.add_argument("--no-verify", action="store_true",
                   help="跳过'注入点必须在目标窗口内'校验（坐标被遮挡时慎用）")

    p = sub.add_parser("find", help="查找窗口（按标题子串），输出 hwnd")
    p.add_argument("title", nargs="?", default="", help="标题子串（省略=列出全部可见窗口）")
    p.add_argument("--class-name", default="", help="窗口类名过滤")

    p = sub.add_parser("drag", help="拖拽")
    p.add_argument("x1", type=int)
    p.add_argument("y1", type=int)
    p.add_argument("x2", type=int)
    p.add_argument("y2", type=int)

    p = sub.add_parser("script", help="一次进程内执行多个动作（分号分隔）")
    p.add_argument("text")

    sub.add_parser("consent", help="查看当前控制授权状态（只读）")
    sub.add_parser("pos", help="打印当前物理光标位置（只读）")
    p = sub.add_parser("monitors", help="枚举所有显示器（只读，副屏适配用）")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    sub.add_parser("doctor", help="环境体检：DPI/显示器/授权/守护/副屏 一览（只读）")
    p = sub.add_parser("demo", help="开箱演示：AI 鼠标在演示窗画轨迹+点击（约 20 秒，需授权）")
    p.add_argument("--dry", action="store_true", help="只打印将要做的事，不请求授权、不动手")
    sub.add_parser("selftest", help="全自动自检（窗口 + 注入 + 隔离验证）")

    args = parser.parse_args(argv)

    if args.cmd == "consent":
        print(gate_status())
        return 0

    if args.cmd == "pos":
        print(get_cursor_pos())
        return 0

    if args.cmd == "monitors":
        info = list_monitors()
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=1))
        else:
            v = info["virtual"]
            print(f"虚拟桌面: x={v[0]} y={v[1]} {v[2]}x{v[3]} | 显示器数: {info['count']}")
            for i, m in enumerate(info["monitors"], 1):
                scale = f" @{m['scale_pct']}% ({m['dpi']}dpi)" if m["scale_pct"] else ""
                print(f"  显示器{i}: {m['device']}  rect={tuple(m['rect'])}  "
                      f"work={tuple(m['work'])}  primary={m['primary']}  "
                      f"{m['size'][0]}x{m['size'][1]}{scale}")
        return 0

    if args.cmd == "doctor":
        return _print_doctor()

    if args.cmd == "demo":
        import aim_demo
        return aim_demo.main(dry=args.dry)

    if args.cmd == "find":
        for h in find_windows(args.title, args.class_name):
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(h, buf, 512)
            print(f"{h}\t{buf.value}")
        return 0

    if args.cmd == "selftest":
        import _selftest
        return _selftest.main()

    # 其他动作统一在同一个合成鼠标会话里执行
    try:
        with SyntheticMouse() as mouse:
            if args.cmd == "move":
                mouse.move(args.x, args.y)
            elif args.cmd == "click":
                mouse.click(args.x, args.y, button=args.button,
                            activate=not args.no_activate, hwnd=args.hwnd,
                            verify_target=not args.no_verify)
            elif args.cmd == "dblclick":
                mouse.double_click(args.x, args.y,
                                   activate=not args.no_activate, hwnd=args.hwnd,
                                   verify_target=not args.no_verify)
            elif args.cmd == "drag":
                mouse.drag(args.x1, args.y1, args.x2, args.y2)
            elif args.cmd == "script":
                _exec_script(mouse, args.text)
    except AiMouseError as e:
        print(f"[ai_mouse] {e}", file=sys.stderr)
        return 3
    except Exception as e:
        if type(e).__name__ == "NoConsent":      # gate.NoConsent：未授权
            print(f"[ai_mouse] {e}", file=sys.stderr)
            return 4
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
