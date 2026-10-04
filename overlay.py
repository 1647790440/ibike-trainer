#!/usr/bin/env python3
"""iBike 数据悬浮窗：像微星小飞机那样，把实时训练数据浮在屏幕角落。

一般不用手敲命令——**网页「1 连接设备」页上有个「悬浮显示」按钮**，
点一下就开、再点一下就关。命令行留给想单独控制它的场景：

    ./run-overlay.sh                      # 默认贴屏幕右上角
    ./run-overlay.sh --anchor top-left    # 换角：top-right/left、bottom-right/left
    ./run-overlay.sh --pos 24,60          # 距所选角的边距
    ./run-overlay.sh --alpha 0.8          # 再淡一点
    ./run-overlay.sh --size 1.3           # 字号放大
    ./run-overlay.sh --check              # 不开窗口，只打印它将要显示的内容
    ./run-overlay.sh --selftest           # 造几个假状态，验证取数/配色/排版逻辑

它是**独立进程**、只读 `/api/state`，所以崩了也影响不到训练；服务退出时会把它收掉。
详细说明见 docs/overlay.md。

实现要点（macOS 原生窗口，靠 bleak 顺带装好的 pyobjc，零新增依赖）：

  · 无边框 + 背景全透明（opaque=0）           → 只有文字浮着，不是一块半透明方块
  · level=floating                            → 永远在最上层
  · collectionBehavior 带 FullScreenAuxiliary → 能盖在**全屏视频**上面
  · ignoresMouseEvents                        → **点击穿透**，不挡你点下面的视频
  · activationPolicy=Accessory                → 不进 Dock、不抢焦点、不打断视频
  · 数据从本机 /api/state 拉，0.5 秒一次       → 后端一行都不用改，也绝不干扰训练

位置固定用 --anchor/--pos 指定（默认右上角）：点击穿透之后就没法用鼠标拖了，
想挪位置改参数重启即可。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request

DEFAULT_PORT = 8765
POLL_INTERVAL_S = 0.5

# 颜色（RGB）
GREEN = (0.35, 0.90, 0.45)
AMBER = (1.00, 0.72, 0.28)
RED = (1.00, 0.42, 0.38)
WHITE = (1.00, 1.00, 1.00)
DIM = (0.78, 0.80, 0.84)
CYAN = (0.45, 0.85, 1.00)

STATE_TEXT = {
    "running": "进行中",
    "paused": "已暂停",
    "finished": "已完成",
    "idle": "未开始",
    "error": "出错",
}


def state_url(port: int) -> str:
    return "http://127.0.0.1:{}/api/state".format(port)


def fetch_state(port: int, timeout: float = 1.5):
    """读一次 /api/state。失败返回 None（悬浮窗只显示"未运行"，不报错刷屏）。"""
    try:
        with urllib.request.urlopen(state_url(port), timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


ANCHORS = ("top-right", "top-left", "bottom-right", "bottom-left")


def resolve_origin(visible, win_w, win_h, anchor, offset_x, offset_y):
    """算出窗口**左下角**在屏幕坐标系里的位置（Cocoa 是左下角为原点）。

    ``visible`` 是 NSScreen.visibleFrame()：已经排除了菜单栏和 Dock，
    所以贴着它的上沿放就是从菜单栏下面开始，不会被菜单栏压住。
    ``anchor`` 是贴哪个角，``offset`` 是从那个角往里让多少。
    """
    vx, vy = visible.origin.x, visible.origin.y
    vw, vh = visible.size.width, visible.size.height
    if anchor == "top-left":
        return vx + offset_x, vy + vh - offset_y - win_h
    if anchor == "top-right":
        return vx + vw - offset_x - win_w, vy + vh - offset_y - win_h
    if anchor == "bottom-left":
        return vx + offset_x, vy + offset_y
    # bottom-right
    return vx + vw - offset_x - win_w, vy + offset_y


def _clock(seconds) -> str:
    try:
        total = int(max(0.0, float(seconds)))
    except (TypeError, ValueError):
        return "--:--"
    return "{}:{:02d}".format(total // 60, total % 60)


def format_lines(state) -> list:
    """把状态整理成要画的行：(文字, 颜色, 字号档位)。

    抽成纯函数是为了能单测——--selftest 就是拿几个假状态跑它。
    """
    if state is None:
        return [("iBike 未运行", DIM, "big"),
                ("等 127.0.0.1 上的服务起来", DIM, "small"),
                ("", DIM, "small")]

    power = state.get("power")
    target = state.get("target_power")
    lines = []

    # 第一行：功率。骑的时候最需要一眼看到的就是它，字号最大。
    if power is None:
        lines.append(("-- W", DIM, "big"))
    else:
        color = GREEN
        if target:
            dev = abs(power - target) / float(target)
            color = GREEN if dev <= 0.05 else (AMBER if dev <= 0.12 else RED)
        lines.append(("{:.0f}W".format(power), color, "big"))

    # 第二行：剩余时间
    if state.get("is_interval") and state.get("step_remaining_s") is not None:
        lines.append(("本段 " + _clock(state.get("step_remaining_s")), WHITE, "mid"))
    else:
        lines.append(("剩余 " + _clock(state.get("remaining_s")), WHITE, "mid"))

    # 第三行：心率 / 踏频 / 目标
    third = []
    hr = state.get("heart_rate")
    zone = (state.get("hr_zone") or {}).get("code")
    if hr is not None:
        third.append("♥{:.0f}".format(hr) + (" " + zone if zone else ""))
    cadence = state.get("cadence")
    if cadence is not None:
        third.append("{:.0f}rpm".format(cadence))
    if target:
        third.append("/ {:.0f}W".format(target))
    lines.append((" · ".join(third) or "—", CYAN, "mid"))

    # 第四行：状态（掉线/重连/暂停都要一眼看出来）
    if state.get("error"):
        lines.append(("⚠ " + str(state["error"])[:26], RED, "small"))
    elif state.get("trainer_lost") and state.get("auto_reconnecting"):
        lines.append(("● 骑行台掉线 · 正在自动重连", AMBER, "small"))
    elif state.get("trainer_lost"):
        lines.append(("● 骑行台掉线", RED, "small"))
    elif state.get("auto_paused"):
        lines.append(("● 自动暂停（踩起来就继续）", AMBER, "small"))
    else:
        name = STATE_TEXT.get(str(state.get("state")), str(state.get("state")))
        mode = state.get("erg_mode_label") or ""
        color = GREEN if state.get("state") == "running" else DIM
        lines.append(("● " + name + (" · " + mode if mode else ""), color, "small"))
    return lines


# ----------------------------------------------------------------------
# 取数：后台线程轮询，主线程只管画
# ----------------------------------------------------------------------

class StatePoller:
    """后台线程每 0.5 秒读一次 /api/state。只读接口，绝不干扰训练。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.state = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_ok = 0.0
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self):
        with self._lock:
            return self.state

    def _loop(self) -> None:
        while not self._stop.is_set():
            fresh = fetch_state(self.port)
            with self._lock:
                if fresh is not None:
                    self.state = fresh
                    self._last_ok = time.time()
                elif self._last_ok and time.time() - self._last_ok > 5.0:
                    # 服务真的没了才显示"未运行"，短暂抖动保留上一份，
                    # 免得窗口一闪一闪
                    self.state = None
            self._stop.wait(POLL_INTERVAL_S)


# ----------------------------------------------------------------------
# 窗口（只在 GUI 模式下用到）
# ----------------------------------------------------------------------

FONT_SIZES = {"big": 26.0, "mid": 13.0, "small": 11.0}
ROW_HEIGHTS = {"big": 30.0, "mid": 17.0, "small": 15.0}
PADDING = 10.0


def _build_appkit():
    """延迟导入 AppKit：--check / --selftest 完全不需要 GUI。"""
    import AppKit
    import objc
    from Foundation import NSObject, NSTimer

    class OverlayView(AppKit.NSView):
        def initWithPoller_scale_(self, poller, scale):
            self = objc.super(OverlayView, self).init()
            if self is None:
                return None
            self._poller = poller
            self._scale = scale
            shadow = AppKit.NSShadow.alloc().init()
            shadow.setShadowOffset_((0.0, -1.0))
            shadow.setShadowBlurRadius_(3.0)
            shadow.setShadowColor_(
                AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.9))
            self._shadow = shadow
            return self

        def isFlipped(self):        # y 轴朝下，排版好算
            return True

        def drawRect_(self, _rect):
            y = PADDING
            for text, color, kind in format_lines(self._poller.snapshot()):
                if text:
                    size = FONT_SIZES[kind] * self._scale
                    attrs = {
                        AppKit.NSFontAttributeName:
                            AppKit.NSFont.systemFontOfSize_weight_(size, 0.4),
                        AppKit.NSForegroundColorAttributeName:
                            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
                                color[0], color[1], color[2], 1.0),
                        AppKit.NSShadowAttributeName: self._shadow,
                    }
                    s = AppKit.NSAttributedString.alloc().initWithString_attributes_(
                        text, attrs)
                    s.drawAtPoint_((PADDING, y))
                y += ROW_HEIGHTS[kind] * self._scale

    class TickTarget(NSObject):
        def initWithView_(self, view):
            self = objc.super(TickTarget, self).init()
            self._view = view
            return self

        def tick_(self, _timer):
            self._view.setNeedsDisplay_(True)

    return AppKit, NSTimer, OverlayView, TickTarget


def run_overlay(args) -> int:
    try:
        AppKit, NSTimer, OverlayView, TickTarget = _build_appkit()
    except ImportError as exc:
        print("这个 Python 里没有 AppKit（pyobjc）：{}".format(exc), file=sys.stderr)
        print("注：跑 ./run.sh 用的那个 venv 里是有的（bleak 会装 pyobjc）。",
              file=sys.stderr)
        return 2

    poller = StatePoller(args.port)

    app = AppKit.NSApplication.sharedApplication()
    # 不进 Dock、不抢焦点：它只是个显示层
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    offset_x, offset_y = args.pos
    visible = AppKit.NSScreen.mainScreen().visibleFrame()
    origin_x, origin_y = resolve_origin(visible, args.width, args.height,
                                        args.anchor, offset_x, offset_y)
    win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(origin_x, origin_y, args.width, args.height),
        AppKit.NSWindowStyleMaskBorderless,
        AppKit.NSBackingStoreBuffered,
        False,
    )
    win.setOpaque_(False)
    win.setBackgroundColor_(AppKit.NSColor.clearColor())
    win.setHasShadow_(False)
    win.setLevel_(AppKit.NSFloatingWindowLevel)          # 置顶
    win.setIgnoresMouseEvents_(True)                      # 点击穿透
    win.setAlphaValue_(args.alpha)
    win.setReleasedWhenClosed_(False)
    win.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary   # 能浮在全屏视频上
        | AppKit.NSWindowCollectionBehaviorStationary)
    view = OverlayView.alloc().initWithPoller_scale_(poller, args.size)
    win.setContentView_(view)
    win.orderFrontRegardless()                            # 不激活 app 也显示

    target = TickTarget.alloc().initWithView_(view)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        POLL_INTERVAL_S, target, "tick:", None, True)

    print("悬浮窗已显示：贴 {}，距角 ({:.0f},{:.0f})，{}×{}，透明度 {}。Ctrl+C 退出。"
          .format(args.anchor, offset_x, offset_y, args.width, args.height,
                  args.alpha), flush=True)
    print("点击穿透已开——鼠标能直接点到底下的视频。想挪位置改 --pos 重启即可。",
          flush=True)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
    return 0


def run_check(args) -> int:
    state = fetch_state(args.port)
    if state is None:
        print("拿不到 {}（服务没起来？）".format(state_url(args.port)))
        return 1
    print("数据来自 {}".format(state_url(args.port)))
    for text, color, kind in format_lines(state):
        print("  [{:>5}] {}".format(kind, text))
    return 0


SAMPLE_STATES = [
    ("正常骑行（贴目标）", {
        "state": "running", "power": 138.0, "target_power": 140.0,
        "remaining_s": 1938, "heart_rate": 148, "hr_zone": {"code": "Z4"},
        "cadence": 78, "erg_mode_label": "原生 ERG"}),
    ("偏高了", {"state": "running", "power": 168.0, "target_power": 140.0,
                "remaining_s": 900, "heart_rate": 156, "hr_zone": {"code": "Z4"},
                "cadence": 82, "erg_mode_label": "原生 ERG"}),
    ("偏低了", {"state": "running", "power": 108.0, "target_power": 140.0,
                "remaining_s": 120, "heart_rate": 132, "hr_zone": {"code": "Z3"},
                "cadence": 70, "erg_mode_label": "原生 ERG"}),
    ("间歇课（显示本段剩余）", {
        "state": "running", "power": 238.0, "target_power": 238.0,
        "is_interval": True, "step_remaining_s": 96, "remaining_s": 1500,
        "heart_rate": 162, "hr_zone": {"code": "Z5"}, "cadence": 88,
        "erg_mode_label": "原生 ERG"}),
    ("自动暂停", {"state": "paused", "auto_paused": True, "power": None,
                  "target_power": 140.0, "remaining_s": 600, "heart_rate": 112,
                  "cadence": None, "erg_mode_label": "原生 ERG"}),
    ("掉线 + 自动重连", {"state": "paused", "trainer_lost": True,
                         "auto_reconnecting": True, "power": None,
                         "target_power": 140.0, "remaining_s": 600,
                         "heart_rate": 124, "cadence": None}),
    ("服务没起来", None),
]


def run_selftest(args) -> int:
    # 位置计算是纯函数，可以先在这里验：假设屏幕可视区 1512×982（菜单栏已排除）
    from types import SimpleNamespace
    fake = SimpleNamespace(
        origin=SimpleNamespace(x=0.0, y=0.0),
        size=SimpleNamespace(width=1512.0, height=982.0))
    cases = {
        # 2115×1023 的窗口、宽高 210×105、边距 24
        "top-right": (1278.0, 853.0),
        "top-left": (24.0, 853.0),
        "bottom-right": (1278.0, 24.0),
        "bottom-left": (24.0, 24.0),
    }
    print("位置计算自检（可视区 1512×982，窗口 210×105，边距 24）：")
    bad = 0
    for anchor, want in cases.items():
        got = resolve_origin(fake, 210.0, 105.0, anchor, 24.0, 24.0)
        ok = (abs(got[0] - want[0]) < 0.5 and abs(got[1] - want[1]) < 0.5)
        bad += 0 if ok else 1
        print("  {} {:<12} → x={:7.1f} y={:7.1f}{}".format(
            "✔" if ok else "✘", anchor, got[0], got[1],
            "" if ok else "   期望 x={:.1f} y={:.1f}".format(*want)))
    print()

    for label, state in SAMPLE_STATES:
        print("\n[{}]".format(label))
        for text, color, kind in format_lines(state):
            print("  [{:>5}] {:32s} rgb{}".format(kind, text, color))
    print("\n排版 / 配色 / 状态分支都跑通了。" if not bad
          else "\n有 {} 项位置计算不对。".format(bad))
    print("想看真实数据：先 .venv/bin/python main.py --sim，再 ./run-overlay.sh --check")
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="iBike 数据悬浮窗（实验副本，不影响原项目）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="iBike 服务端口（默认 {}）".format(DEFAULT_PORT))
    parser.add_argument("--anchor", default="top-right", choices=list(ANCHORS),
                        help="贴屏幕的哪个角（默认 top-right：右上角，"
                             "会自动避开菜单栏）")
    parser.add_argument("--pos", default="24,24",
                        help="距所选角的位置，如 24,24（默认 24,24）")
    parser.add_argument("--alpha", type=float, default=0.92,
                        help="整体透明度 0-1（默认 0.92）")
    parser.add_argument("--size", type=float, default=1.0,
                        help="字号缩放（默认 1.0）")
    parser.add_argument("--width", type=float, default=210.0)
    parser.add_argument("--height", type=float, default=105.0)
    parser.add_argument("--check", action="store_true",
                        help="不开窗口，只打印将要显示的内容")
    parser.add_argument("--selftest", action="store_true",
                        help="用假数据验证取数/配色/排版逻辑")
    args = parser.parse_args()

    try:
        x, y = [float(v) for v in str(args.pos).split(",")]
    except ValueError:
        print("--pos 要写成 24,24 这样", file=sys.stderr)
        return 2
    args.pos = (x, y)
    args.alpha = max(0.15, min(1.0, args.alpha))

    if args.selftest:
        return run_selftest(args)
    if args.check:
        return run_check(args)
    return run_overlay(args)


if __name__ == "__main__":
    sys.exit(main())
