#!/usr/bin/env python3
"""iBike 菜单栏启动器：双击 .app 就能用，不用开终端。

它做的事很单纯——**当作一个壳**：

1. 把服务（`main.py`）作为子进程跑起来，日志写到 `~/Library/Logs/iBike.log`
2. 等服务真的能应答，再打开浏览器
3. 常驻菜单栏：看状态 / 打开控制台 / 显示日志 / 重启 / 退出
4. 退出时给服务发 SIGTERM，走它自己的优雅收尾（存报告、关悬浮窗，约 0.1 秒）

**故意不把 Python 和依赖打进包里**：这样改代码不用重新打包，重启 App 就生效；
体积也就几十 KB 而不是上百 MB。代价是它依赖项目目录和 `.venv` 存在——对自用这台
机器完全够用（真要分发给别人，那得做 PyInstaller/py2app 那一档，见 docs/packaging-options.md）。

用法：
    ./iBike.app                    # 双击就行
    python3 packaging/launcher.py  # 或者命令行直接跑（等价）
    python3 packaging/launcher.py --check   # 不开菜单栏：起服务→探活→停掉，用来测试
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

APP_NAME = "iBike"
DEFAULT_PORT = 8765
READY_TIMEOUT_S = 25.0           # 等服务起来的耐心（首次启动要建对象、扫 BLE，慢一点）
STOP_TIMEOUT_S = 8.0             # 等服务优雅退出的耐心（它自己做收尾约 0.1s）
LOG_PATH = Path.home() / "Library" / "Logs" / "iBike.log"
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "iBike"

STATE_TEXT = {
    "running": "骑行中",
    "paused": "已暂停",
    "finished": "已完成",
    "idle": "待机",
    "error": "出错",
}


# ----------------------------------------------------------------------
# 纯逻辑（好测）
# ----------------------------------------------------------------------

def project_root() -> Path:
    """项目根目录：装了 .app 之后取打包时写进去的路径，否则取本文件上一级。"""
    config = Path(__file__).resolve().parent / "app-config.json"
    if config.is_file():
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
            root = str(data.get("project_root") or "").strip()
            if root:
                return Path(root).expanduser().resolve()
        except (OSError, ValueError):
            pass
    return Path(__file__).resolve().parent.parent


def resolve_data_dir(project: Path) -> Path:
    """数据目录：**绝不放在 .app 里面**（.app 升级时整体被替换，报告会全没）。

    优先级：
      1. 环境变量 `IBIKE_DATA_DIR`（测试和高级用法）
      2. 项目目录下的 `data/`（存在就用它——这样 App 和 `./run.sh` 看到的是同一份
         历史，不会出现"App 里没有报告"的困惑）
      3. `~/Library/Application Support/iBike`（项目目录搬走/删了时的退路）
    """
    override = os.environ.get("IBIKE_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    project_data = project / "data"
    if project_data.is_dir():
        return project_data
    return APP_SUPPORT_DIR


def state_url(port: int) -> str:
    return "http://127.0.0.1:{}/api/state".format(port)


def probe(port: int, timeout: float = 1.0) -> Optional[dict]:
    """读一次 /api/state。服务没起来 / 不是本程序 → None。"""
    try:
        with urllib.request.urlopen(state_url(port), timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def port_in_use(port: int) -> bool:
    """端口有没有人占着（用来区分"我们的服务在跑"和"别的程序占了端口"）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.3)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def build_command(python: str, main_py: Path, port: int) -> List[str]:
    return [python, str(main_py), "--port", str(port)]


def format_title(state: Optional[dict], running: bool) -> str:
    """菜单栏上那行字：一眼能看出现在在干什么。"""
    if not running:
        return "iBike · 已停止"
    if not state:
        return "iBike · 启动中…"
    mode = STATE_TEXT.get(str(state.get("state")), str(state.get("state") or "?"))
    bits = [mode]
    if state.get("state") in ("running", "paused"):
        power = state.get("power")
        if power is not None:
            bits.append("{:.0f}W".format(power))
        left = state.get("remaining_s")
        if left is not None:
            try:
                total = int(max(0, float(left)))
                bits.append("{}:{:02d}".format(total // 60, total % 60))
            except (TypeError, ValueError):
                pass
    if state.get("trainer_lost"):
        bits.append("骑行台掉线")
    elif state.get("auto_paused"):
        bits.append("自动暂停")
    return "iBike · " + " ".join(bits)


def tail(path: Path, lines: int = 12) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "（还没有日志）"
    rows = [ln for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(rows[-lines:]) or "（日志是空的）"


# ----------------------------------------------------------------------
# 子进程管理
# ----------------------------------------------------------------------

class ServerProcess:
    """跑 `main.py`，负责日志、探活、优雅停止。"""

    def __init__(self, python: str, main_py: Path, port: int, data_dir: Path,
                 log_path: Path = LOG_PATH) -> None:
        self.python = python
        self.main_py = main_py
        self.port = port
        self.data_dir = data_dir
        self.log_path = log_path
        self.proc: Optional[subprocess.Popen] = None

    # -- 生命周期 --
    def start(self) -> None:
        if self.running():
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["IBIKE_DATA_DIR"] = str(self.data_dir)
        # 别让服务端的 print 因为没有终端而堆在缓冲区里（日志要能当场看到）
        env["PYTHONUNBUFFERED"] = "1"
        log = open(self.log_path, "wb")
        log.write(("\n===== {} 启动 =====\n".format(
            time.strftime("%Y-%m-%d %H:%M:%S"))).encode("utf-8"))
        log.flush()
        cmd = build_command(self.python, self.main_py, self.port)
        self.proc = subprocess.Popen(
            cmd, cwd=str(self.main_py.parent), env=env,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()          # 子进程已经继承了 fd

    def wait_ready(self, timeout: float = READY_TIMEOUT_S) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False         # 已经挂了，别干等
            if probe(self.port, timeout=0.6) is not None:
                return True
            time.sleep(0.25)
        return False

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def state(self) -> Optional[dict]:
        return probe(self.port)

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        """SIGTERM → 服务自己收尾（存报告、关悬浮窗）→ 不等就 kill。"""
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except OSError:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            proc.kill()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


# ----------------------------------------------------------------------
# 菜单栏（pyobjc，延迟导入：--check 不需要 GUI）
# ----------------------------------------------------------------------

def run_menu_bar(server: ServerProcess, port: int, open_browser: bool) -> int:
    import objc
    from AppKit import (NSAlert, NSApplication, NSApplicationActivationPolicyAccessory,
                        NSMenu, NSMenuItem, NSObject, NSTimer, NSSound,
                        NSStatusBar, NSVariableStatusItemLength)
    from Foundation import NSURL

    def make_item(target, title, action, key):
        """菜单项。放模块级：放在 Delegate 里会被 pyobjc 当成 ObjC 选择器。"""
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, action, key)
        item.setTarget_(target)
        return item

    class Delegate(NSObject):
        def init(self):
            self = objc.super(Delegate, self).init()
            if self is None:
                return None
            self.status_item = None
            self.title_item = None
            self.console_item = None
            self.started_at = time.time()
            return self

        # -- 菜单构建 --
        def applicationDidFinishLaunching_(self, _notification):
            bar = NSStatusBar.systemStatusBar()
            self.status_item = bar.statusItemWithLength_(NSVariableStatusItemLength)
            button = self.status_item.button()
            button.setTitle_("iBike · 启动中…")
            menu = NSMenu.alloc().init()
            self.title_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "iBike", None, "")
            self.title_item.setEnabled_(False)
            menu.addItem_(self.title_item)
            menu.addItem_(NSMenuItem.separatorItem())
            menu.addItem_(make_item(self, "打开控制台", "openConsole:", "o"))
            menu.addItem_(make_item(self, "显示日志", "showLog:", "l"))
            self.console_item = make_item(self, "重启服务", "restartServer:", "r")
            menu.addItem_(self.console_item)
            menu.addItem_(NSMenuItem.separatorItem())
            menu.addItem_(make_item(self, "退出（会保存训练记录）", "quitApp:", "q"))
            self.status_item.setMenu_(menu)
            self.tick_(None)
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                2.0, self, "tick:", None, True)

        # -- 定时刷新标题 --
        def tick_(self, _timer):
            if self.status_item is None:
                return
            state = server.state()
            title = format_title(state, server.running())
            self.status_item.button().setTitle_(title)
            if self.title_item is not None:
                self.title_item.setTitle_(title)
            if self.console_item is not None:
                self.console_item.setEnabled_(server.running())
            # 服务自己退了（崩了/被停了）：告诉用户，别让菜单栏一直显示骑行中
            if not server.running() and time.time() - self.started_at > 3.0:
                self.status_item.button().setTitle_("iBike · 已停止（点开菜单看日志）")

        # -- 菜单动作 --
        def openConsole_(self, _sender):
            subprocess.Popen(["open", "http://127.0.0.1:{}".format(port)])

        def showLog_(self, _sender):
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            LOG_PATH.touch(exist_ok=True)
            subprocess.Popen(["open", "-t", str(LOG_PATH)])

        def restartServer_(self, _sender):
            server.stop()
            time.sleep(0.3)
            server.start()
            server.wait_ready()

        def quitApp_(self, _sender):
            server.stop()
            NSApplication.sharedApplication().terminate_(None)

        def applicationWillTerminate_(self, _notification):
            server.stop()

    app = NSApplication.sharedApplication()
    # 只驻留菜单栏：不进 Dock、不抢焦点（它是个后台服务外壳）
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = Delegate.alloc().init()
    app.setDelegate_(delegate)

    def _bye(_signum, _frame):
        server.stop()
        app.terminate_(None)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _bye)
        except (ValueError, OSError):
            pass

    if open_browser:
        subprocess.Popen(["open", "http://127.0.0.1:{}".format(port)])
    app.run()
    return 0


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="iBike 菜单栏启动器")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--check", action="store_true",
                        help="不开菜单栏：起服务→探活→停掉，用来测试")
    parser.add_argument("--no-browser", action="store_true",
                        help="别自动打开浏览器")
    args = parser.parse_args(argv)

    project = project_root()
    main_py = project / "main.py"
    if not main_py.exists():
        print("找不到 {} —— .app 里记的项目路径失效了。\n"
              "重新跑一次 packaging/make_app.sh 就好。".format(main_py),
              file=sys.stderr)
        return 2
    python = str(project / ".venv" / "bin" / "python")
    if not Path(python).exists():
        python = sys.executable

    data_dir = resolve_data_dir(project)
    server = ServerProcess(python, main_py, args.port, data_dir)

    # 已经在跑（比如又双击了一次）：不重复启动，直接把控制台打开
    existing = probe(args.port)
    if existing is not None and not args.check:
        subprocess.Popen(["open", "http://127.0.0.1:{}".format(args.port)])
        print("iBike 已经在运行了（端口 {}），已打开控制台。".format(args.port))
        return 0
    if existing is None and port_in_use(args.port):
        print("端口 {} 被别的程序占着，换个端口：--port 8766".format(args.port),
              file=sys.stderr)
        if not args.check:
            _alert("端口 {} 被别的程序占用了，试试 --port 8766".format(args.port))
        return 2

    server.start()
    if not server.wait_ready():
        detail = tail(LOG_PATH)
        print("服务没起来。日志（{}）:\n{}".format(LOG_PATH, detail), file=sys.stderr)
        if not args.check:
            _alert("iBike 启动失败，日志在 {}\n\n{}".format(LOG_PATH, detail))
        server.stop()
        return 1

    if args.check:
        state = server.state() or {}
        print("服务已就绪：{}".format(state_url(args.port)))
        print("  数据目录:", data_dir)
        print("  当前状态:", state.get("state"), "| 骑行台:",
              (state.get("trainer") or {}).get("name") or "未连接",
              "| 心率带:", "已连接" if (state.get("hr_client") or {}).get("connected")
              else "未连接")
        print("  菜单栏标题会是:", format_title(state, True))
        server.stop()
        print("  已优雅停止 ✓")
        return 0

    print("iBike 服务已就绪（端口 {}），日志：{}".format(args.port, LOG_PATH))
    try:
        return run_menu_bar(server, args.port, open_browser=not args.no_browser)
    finally:
        # 菜单栏那层要是崩了，也不能把服务留成孤儿进程（它接管着蓝牙和报告）
        server.stop()


def _alert(text: str) -> None:
    """出问题时弹个系统对话框——双击启动的用户看不到终端。"""
    try:
        import objc  # noqa: F401
        from AppKit import NSAlert, NSApplication
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(1)          # Accessory
        alert = NSAlert.alloc().init()
        alert.setMessageText_("iBike 启动失败")
        alert.setInformativeText_(text)
        alert.runModal()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
