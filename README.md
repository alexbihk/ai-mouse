# ai-mouse — a second pointer for AI, without hijacking yours

[中文说明见下方](#中文) · Windows 10 1809+ · Python 3.9+ · **zero dependencies**

Give an AI agent its own pointer on your machine. It can move, click, double-click and drag real
windows — while **your** physical cursor stays under **your** hand.

```
┌──────────────────────────────────────────────────────┐
│  your mouse ──────────►  normal input stream         │
│  AI's synthetic pen ──►  WM_POINTER stream           │  ← two writers, no fight
└──────────────────────────────────────────────────────┘
```

---

## ⚠️ Read this first: what it is NOT good at

Published because the limitations are the interesting part. Verified on Windows 11 build 26300,
single 2560×1600 display @150% scaling.

| Limitation | Reality |
|---|---|
| Cursor still flickers | ~60–100 ms of hidden cursor per contact (12–15% duty cycle in our runs). OS anti-mistouch behaviour, **not** a bug we can fix. |
| Clicks steal focus | Every pen contact activates the target window (~0.9×/s). Fine for "AI works on the second monitor", bad for "AI and I share one monitor". |
| Your clicks on the AI's window get swallowed | ~0.8 s cool-down after each contact, **only on the window the AI is operating**. Cross-window delivery stays at 92.7–100%. |
| No wheel | The synthetic pointer API has pen semantics; pens have no wheel. |
| Right click | Experimental. |

**Bottom line: this is for unattended automation on a second monitor, not for co-working on one.**
If your task is a web page, use CDP instead — zero injection, zero side effects.

---

## Why not just use `SendInput`?

Because `SendInput` is global. It *moves your cursor*. If you are dragging a window or aiming in a
game, an agent calling `SendInput` physically yanks your pointer away. Posting `WM_LBUTTONDOWN`
does not work either: modern apps (Chrome et al.) ignore synthetic messages because they cannot
distinguish them from forged ones.

This project uses the official **synthetic pointer device** API instead:

```python
CreateSyntheticPointerDevice(PT_PEN, ...)   # Windows 10 1809+
InjectSyntheticPointerInput(...)            # hover / contact / down / up
DestroySyntheticPointerDevice(...)
```

The OS treats it as *a pen*, not *the mouse* — a separate input stream. The core is ~60 lines of
ctypes; the rest of this file is guards, verification and research notes.

---

## The bug that ate two days: pointer promotion

Synthetic pen events are fine on windows that handle `WM_POINTER`. On windows that don't
(Chrome is one), Windows **promotes** the pen events to mouse events — and moves the physical
cursor to the injection point while doing it.

Measured: **600–1600 px of cursor teleport, in 7–44 ms, on every contact.**
You never see it move. You only feel "my mouse stopped responding".

It cannot be turned off. We tried, and rebooted to confirm:

| Attempt | Result |
|---|---|
| `EnableCursorSuppression = 0` + reboot | no change |
| Disable MouseVanish | no change |
| `POINTER_FEEDBACK_NONE` | no change |
| `PT_TOUCH` instead of `PT_PEN` | worse |
| Create/destroy device around each click | no change |

### CursorGuard

So we detect-and-restore instead. A background thread samples `GetCursorPos` every **1.2 ms** and
treats *physically impossible* motion as a hijack:

- rule A: Δt ≤ 15 ms and Δd > 200 px → **13,300 px/s**, while a human hand tops out near 5,000 px/s
- rule B: within ±0.2 s of an injection, cursor lands within ±30 px of the pen point from >200 px away
- action: `SetCursorPos(previous)` — detect + restore in **~2 ms**, faster than one frame at 60 Hz

Measured with an **A/B/A/B** design (alternating, to cancel ordering effects), scoring the share of
time the cursor is pinned within 120 px of the pen point:

| Phase | Cursor hijacked | Guard |
|---|---|---|
| A1 no guard | **13.0%** | — |
| B1 with guard | **2.7%** | 24/24 intercepted |
| A2 no guard (replicated) | **11.7%** | — |
| B2 with guard (replicated) | **1.8%** | 28/28 intercepted |

**5–7× improvement, replicated twice. 52 hijacks detected, 52 restored, 0 leaked.**
Delivery rate unaffected (36/38 clicks landed during the same run).

Residual: a ~2 ms displacement during each interception (usually inside one frame), and the
flicker above.

---

## What we got wrong (kept on purpose)

Three conclusions we published to ourselves and later had to retract. They are in this repo
because a project that hides its wrong turns makes every reader repeat them.

| Wrong conclusion | Correction |
|---|---|
| "The target window must be foreground, otherwise events are dropped" | Events do reach background windows (3/3). The real constraint is that *clicking activates the window*. |
| "Real mouse clicks are globally frozen while the agent works" | They are not. The cursor was teleporting, and the test clicks happened to land on the window the agent was operating, which has the 0.8 s swallow window. Same-window baseline: 9/10 delivered. |
| "A registry switch disables the anti-mistouch state machine" | It does not. Rebooted and re-measured: zero change. |

---

## Install

Nothing to install. Single file, standard library only.

```bash
git clone https://github.com/alexbihk/ai-mouse
cd ai-mouse
python ai_mouse.py doctor        # read-only: DPI, monitors, gate status
python ai_mouse.py demo          # ~20 s, asks for authorization first
```

## CLI

```bash
python ai_mouse.py consent                       # gate status (read-only)
python ai_mouse.py monitors                      # display list (read-only)
python ai_mouse.py find "Notepad"                # window handles (read-only)
python ai_mouse.py move 800 500                  # hover
python ai_mouse.py click 800 500 --hwnd 1234     # click, verified against hwnd (recommended)
python ai_mouse.py dblclick 800 500 --hwnd 1234
python ai_mouse.py drag 300 300 900 600
python ai_mouse.py script "move 100 100; click; move 400 400; dblclick"
```

Exit codes: `3` verification failed, `4` not authorized.

## Python API

```python
import sys; sys.path.insert(0, "path/to/ai-mouse")
from ai_mouse import SyntheticMouse, find_windows

hwnd = find_windows("Notepad")[0]
with SyntheticMouse(purpose="fill the form") as m:
    m.click(800, 500, hwnd=hwnd)
    m.drag(300, 300, 900, 600)
    print(m._guard.yanks, m._guard.restores)   # audit: how many hijacks were repaired
```

Key kwargs of `click()/double_click()`: `activate=True` (bring window to front, then re-verify
foreground or abort), `hwnd=` (verify the point really falls inside that window),
`verify_target=True` (default).

---

## The authorization gate (a design decision, not a limitation)

Injecting input into someone's machine is not something an agent should be able to do silently.
So every injection goes through a gate that **only a human can open**:

1. **HUD gate** — if `hud/gate.py` or `$AIM_HUD_DIR/gate.py` exists, the gate pops a card on a
   desktop pet and waits for a human click. Consent is written to `consent.json`.
2. **SimpleConsent** — otherwise, ask once in the terminal (`y/N`). Process-scoped, writes nothing.
3. Strict mode: `AIM_GATE=hud` aborts if no HUD gate is found. `AIM_GATE=simple` forces the prompt.

Fail-closed everywhere: no TTY → refuse; declined → refuse; exception while loading a gate →
refuse. An agent must never be able to write `consent.json` itself — that would be issuing its own
license.

---

## Acceptable use

Use it for UI testing, accessibility tooling, and personal automation.

**Do not** use it for ad fraud, click farming, scalping/bot-buying, game cheats, evading platform
anti-bot systems, or anything that violates the terms of a service you do not own. You are
responsible for what you automate.

## Environment

- Windows 10 1809+ (tested on Windows 11 build 26300)
- Python 3.9+, standard library only
- Per-monitor-v2 DPI aware; coordinates are physical pixels
- Multi-monitor: supported by design (PMv2 + virtual-desktop coordinates); contributed results welcome

> Measurement methodology is in the section above. Raw logs (`*_result.json`, `*.jsonl`) are kept
> out of this repository because they contain machine-specific paths; ask in an issue and we will
> publish a sanitized excerpt.

## License

MIT — see `LICENSE`. No warranty.

---

<a id="中文"></a>
# 中文说明

**给 AI 造一只自己的鼠标**：它能移动、点击、双击、拖拽真实窗口，而**你的物理光标一直握在你手里**。

## 它不擅长什么（先看这个）

| 局限 | 实际情况 |
|---|---|
| 光标仍会闪 | 每次接触短隐 60–100ms（实测占空 12–15%），系统防误触行为，**关不掉** |
| 点击会抢焦点 | 每次接触都会激活目标窗（约 0.9 次/秒）。适合"AI 在副屏干活"，不适合"跟你抢同一块屏" |
| 你在 AI 那扇窗上的点击会被吞 | 每次接触后 0.8 秒冷却，**仅限 AI 正在操作的那一扇窗**；跨窗送达 92.7%–100% |
| 没有滚轮 | 合成指针是笔语义，笔没有滚轮 |
| 右键 | 实验性 |

**一句话**：它是给"副屏无人值守自动化"用的，不是给"两个人挤一块屏"用的。如果任务是网页，请直接用 CDP——零注入、零副作用。

## 为什么不用 `SendInput`

因为它是**全局**的：它一动，你的光标跟着动。你在拖窗口或打游戏，它把光标拽走。
发 `WM_LBUTTONDOWN` 也没用——Chrome 这类程序会直接忽略伪造消息。

本项目用 Windows 官方的**合成指针设备**（`CreateSyntheticPointerDevice(PT_PEN)`），
系统认为这是"一支笔"，是另一条输入流。核心约 60 行 ctypes，其余都是守护、校验和研究记录。

## 那个花了两天的坑：指针提升

在不处理 `WM_POINTER` 的窗口上（Chrome 就是），系统会把笔事件**提升**成鼠标事件，
顺便把物理光标搬到注入点：**实测 600–1600 像素、7–44 毫秒，每次接触都可能发生。**

而且关不掉。我们试过并重启验证：注册表 `EnableCursorSuppression=0`、关 MouseVanish、
`POINTER_FEEDBACK_NONE`、改用 `PT_TOUCH`、点完即销毁设备——**全部无效**。

### CursorGuard 光标守护

每 1.2ms 采样一次 `GetCursorPos`，把"人手做不到的位移"判为被抢：
相邻采样 ≤15ms 且位移 >200px（= 每秒 13,300 像素，人手上限约 5,000）；注入前后 0.2 秒内
光标从 200px 外"恰好"落在笔点 ±30px 也算。命中就 `SetCursorPos` 还原，**检测+还原约 2ms**。

A/B/A/B 交替实测（光标被吸在笔点 120px 内的时间占比）：

| 阶段 | 被吸占比 | 守护拦截 |
|---|---|---|
| A1 无守护 | **13.0%** | — |
| B1 有守护 | **2.7%** | 24/24 |
| A2 无守护（复现） | **11.7%** | — |
| B2 有守护（复现） | **1.8%** | 28/28 |

**5–7 倍改善，两次复现；52 次被吸，52 次全部还原，一次没漏。** 同期送达率未下降（36/38）。

## 授权闸门（设计决定，不是限制）

往别人的电脑里注入输入，不该是智能体能悄悄做的事。所以每次注入都过一道**只有人能开**的闸门：

- 有 `hud/gate.py`（或 `$AIM_HUD_DIR/gate.py`）→ 弹卡片等人点「允许」，写 `consent.json`
- 没有 → 内置 `SimpleConsent`，在终端问一次 `y/N`，仅本进程有效、不落盘
- 严格模式：`AIM_GATE=hud` 找不到就中止；`AIM_GATE=simple` 强制命令行确认

处处 fail-closed：无终端 → 拒绝；点拒绝 → 拒绝；闸门加载异常 → 拒绝。
**智能体永远不能自己写 `consent.json`**——那等于自己给自己发许可证。

## 合理使用

用于界面测试、无障碍辅助、个人自动化。
**不要**用于刷量、抢购、游戏外挂、绕过平台风控或任何违反你所使用服务条款的用途。
你自动化的东西，后果由你承担。

## 许可

MIT。
