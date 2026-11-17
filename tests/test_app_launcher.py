#!/usr/bin/env python3
"""iBike.app（菜单栏启动器）的测试。

不真开菜单栏（那需要 GUI 会话），但把能测的都测了：

  [1] 纯逻辑：数据目录优先级、命令行、菜单栏标题文案
  [2] 探活：起一个假服务 → probe/port_in_use 判断对不对
  [3] 子进程生命周期：start → wait_ready → stop（SIGTERM，优雅停）
  [4] 打包产物：Info.plist 必须有蓝牙权限说明（少了系统会直接杀掉进程）
  [5] 端到端：launcher.py --check（起真服务 → 探活 → 优雅停止）

    python3 tests/test_app_launcher.py
"""

from __future__ import annotations

import json
import os
import plistlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging"))

_failures: list = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print("  {} {}{}".format("\033[32m✔\033[0m" if cond else "\033[31m✘\033[0m",
                             label, "  → " + detail if detail else ""))
    if not cond:
        _failures.append(label)
    return cond


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_pure_logic() -> None:
    print("\n[1] 纯逻辑：数据目录 / 命令行 / 菜单栏标题")
    import launcher

    # 数据目录：环境变量 > 项目 data/ > Application Support
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "proj"
        (project / "data").mkdir(parents=True)
        old = os.environ.pop("IBIKE_DATA_DIR", None)
        try:
            check(launcher.resolve_data_dir(project) == project / "data",
                  "项目里有 data/ 就用它（和 ./run.sh 同一份历史，不会出现"
                  "「App 里没有报告」）", str(launcher.resolve_data_dir(project)))
            os.environ["IBIKE_DATA_DIR"] = str(Path(tmp) / "custom")
            check(launcher.resolve_data_dir(project) == Path(tmp) / "custom",
                  "IBIKE_DATA_DIR 优先")
        finally:
            os.environ.pop("IBIKE_DATA_DIR", None)
            if old is not None:
                os.environ["IBIKE_DATA_DIR"] = old
        empty = Path(tmp) / "empty"
        empty.mkdir()
        got = launcher.resolve_data_dir(empty)
        check(got == launcher.APP_SUPPORT_DIR,
              "项目目录里没有 data/ 时退回 Application Support（绝不写进 .app 里）",
              str(got))

    cmd = launcher.build_command("/x/python", Path("/x/main.py"), 8766)
    check(cmd == ["/x/python", "/x/main.py", "--port", "8766"], "命令行拼装",
          str(cmd))

    # 菜单栏标题：一眼能看出在干什么
    check(launcher.format_title(None, False) == "iBike · 已停止", "停了显示已停止")
    check(launcher.format_title(None, True) == "iBike · 启动中…", "启动中")
    title = launcher.format_title({"state": "running", "power": 132.4,
                                   "remaining_s": 95}, True)
    check("骑行中" in title and "132W" in title and "1:35" in title,
          "骑行中显示功率和剩余时间", title)
    check("骑行台掉线" in launcher.format_title(
        {"state": "paused", "trainer_lost": True}, True), "掉线要写出来")
    check("自动暂停" in launcher.format_title(
        {"state": "paused", "auto_paused": True}, True), "自动暂停要写出来")


def test_probe() -> None:
    print("\n[2] 探活：假服务 / 端口占用")
    import launcher

    port = free_port()
    check(launcher.probe(port, timeout=0.4) is None, "没有服务时探不到")
    check(launcher.port_in_use(port) is False, "端口空闲")

    script = Path(tempfile.mkdtemp(prefix="ibike-fake-srv-")) / "srv.py"
    script.write_text(
        "import json, sys, http.server\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = json.dumps({'state': 'idle', 'power': 0}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def log_message(self, *a):\n"
        "        pass\n"
        "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
        "http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()\n",
        encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script), "--port", str(port)],
                            start_new_session=True)
    try:
        deadline = time.time() + 8
        data = None
        while time.time() < deadline:
            data = launcher.probe(port, timeout=0.5)
            if data is not None:
                break
            time.sleep(0.15)
        check(data is not None and data.get("state") == "idle",
              "服务起来之后能读到状态", str(data))
        check(launcher.port_in_use(port) is True, "端口占用能识别出来")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_server_process() -> None:
    print("\n[3] 子进程生命周期：起 → 就绪 → 优雅停")
    import launcher

    port = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="ibike-launchproc-"))
    script = tmp / "srv.py"
    script.write_text(
        "import json, sys, signal, http.server\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = json.dumps({'state': 'idle'}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def log_message(self, *a):\n"
        "        pass\n"
        "def bye(*a):\n"
        "    print('graceful', flush=True)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, bye)\n"
        "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
        "http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()\n",
        encoding="utf-8")
    server = launcher.ServerProcess(
        sys.executable, script, port, tmp / "data", log_path=tmp / "srv.log")
    try:
        server.start()
        check(server.running(), "进程起来了")
        check(server.wait_ready(timeout=10), "能探到就绪")
        check(server.state() is not None, "能读到状态")
        proc = server.proc
        server.stop()
        check(not server.running(), "stop 之后进程没了")
        check(proc is not None and proc.poll() is not None,
              "确认真的退出了", "returncode={}".format(proc.poll() if proc else None))
        log = (tmp / "srv.log").read_text(encoding="utf-8", errors="replace")
        # 用 SIGTERM 而不是 kill -9，服务才有机会存报告/关悬浮窗
        check("graceful" in log, "走的是 SIGTERM 优雅停止（不是硬杀）",
              log.strip().splitlines()[-1] if log.strip() else "")
    finally:
        server.stop()


def test_bundle() -> None:
    print("\n[4] 打包产物：Info.plist / 可执行位 / 配置")
    out = Path(tempfile.mkdtemp(prefix="ibike-app-")) / "iBike.app"
    env = dict(os.environ)
    env["OUT"] = str(out)
    proc = subprocess.run(["bash", str(ROOT / "packaging" / "make_app.sh")],
                          cwd=str(ROOT), env=env, capture_output=True, text=True)
    if not check(proc.returncode == 0, "make_app.sh 成功",
                 (proc.stderr or "").strip()[-200:]):
        return
    plist_path = out / "Contents" / "Info.plist"
    exe = out / "Contents" / "MacOS" / "iBike"
    config = out / "Contents" / "Resources" / "app-config.json"
    check(plist_path.is_file() and exe.is_file() and config.is_file(),
          "包结构齐全（Info.plist / 可执行 / 配置）")
    with open(plist_path, "rb") as fh:
        info = plistlib.load(fh)
    desc = str(info.get("NSBluetoothAlwaysUsageDescription") or "")
    check(len(desc) > 5,
          "Info.plist 有蓝牙权限说明（少了它，一碰蓝牙系统就直接杀进程）", desc)
    check(info.get("LSUIElement") is True,
          "LSUIElement=true（只驻留菜单栏，不占 Dock）")
    check(len(str(info.get("NSDesktopFolderUsageDescription") or "")) > 5,
          "Info.plist 有「桌面文件夹」用途说明（项目在桌面上，少了会被静默拒绝）",
          str(info.get("NSDesktopFolderUsageDescription") or ""))
    # 关键：主可执行文件必须是编译出来的 Mach-O。
    # 用 shell 脚本时 LaunchServices 把主进程算成 /bin/bash，TCC 权限也归给 bash
    # （bash 没有用途说明）→ 访问桌面被**静默拒绝**，表现是"双击完全没反应"。
    with open(exe, "rb") as fh:
        magic = fh.read(4)
    check(magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"),
          "主可执行文件是 Mach-O 二进制（不能是 shell 脚本，否则权限归属会错）",
          magic.hex())
    check(os.access(exe, os.X_OK), "可执行文件有执行位")
    data = json.loads(config.read_text(encoding="utf-8"))
    check(Path(data.get("project_root", "")) == ROOT,
          "配置里记的是当前项目路径", data.get("project_root"))


def test_check_mode() -> None:
    print("\n[5] 端到端：launcher.py --check（起真服务 → 探活 → 优雅停）")
    port = free_port()
    data_dir = tempfile.mkdtemp(prefix="ibike-launch-check-")
    env = dict(os.environ)
    env["IBIKE_DATA_DIR"] = data_dir
    proc = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"),
         str(ROOT / "packaging" / "launcher.py"),
         "--check", "--port", str(port)],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
    out = proc.stdout + proc.stderr
    check(proc.returncode == 0, "退出码 0", str(proc.returncode))
    check("服务已就绪" in out, "服务真的起来了",
          (out.strip().splitlines() or [""])[0])
    check("已优雅停止" in out, "停的时候走的是优雅收尾")
    check(data_dir in out, "用的是指定的数据目录")


def main() -> int:
    print("=" * 70)
    print("iBike.app（菜单栏启动器）测试")
    print("=" * 70)
    test_pure_logic()
    test_probe()
    test_server_process()
    test_bundle()
    test_check_mode()

    print("\n" + "=" * 70)
    if _failures:
        print("失败 {} 项：".format(len(_failures)))
        for f in _failures:
            print("   - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
