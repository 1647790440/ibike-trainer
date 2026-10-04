#!/usr/bin/env python3
"""悬浮窗开关的测试。

设备页那个「悬浮显示」按钮打的是 `POST /api/overlay`，服务端据此拉起/收掉一个
独立的 overlay.py 进程。这里不真的开窗口——两种办法都不动 GUI：

  · 覆盖 `server.overlay_command`，换成一个只会 sleep 的假进程（测开关逻辑）
  · 覆盖 `server_mod.OVERLAY_SCRIPT`，换成一个小脚本，让它把 argv 写进文件
    （测"端口有没有正确传过去"和"脚本不存在时给不给得出人话")

    python3 tests/test_overlay_toggle.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os as _os

_os.environ.setdefault("IBIKE_DATA_DIR",
                       tempfile.mkdtemp(prefix="ibike-test-data-"))

import ibike.server as server_mod  # noqa: E402
from aiohttp import ClientSession, web  # noqa: E402

_failures: List[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print("  {} {}{}".format("\033[32m✔\033[0m" if cond else "\033[31m✘\033[0m",
                             label, "  → " + detail if detail else ""))
    if not cond:
        _failures.append(label)
    return cond


SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]
DIES = [sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"]


def install_fakes() -> None:
    class FakeTrainerClient:
        def __new__(cls, on_event=None, on_data=None):
            raise RuntimeError("这个测试不该连骑行台")

        @staticmethod
        def classify_trainers(raw):
            return []

    async def fake_scan_raw(timeout: float = 8.0):
        return []

    server_mod.TrainerClient = FakeTrainerClient
    server_mod.scan_raw = fake_scan_raw


async def with_server(fn) -> None:
    server = server_mod.Server(use_simulator=False)
    app = server.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        async with ClientSession() as http:
            await fn(http, "http://127.0.0.1:{}".format(port), server)
    finally:
        await runner.cleanup()


async def _post(http, base, action):
    async with http.post(base + "/api/overlay", json={"action": action}) as resp:
        return resp.status, await resp.json()


async def _state(http, base):
    async with http.get(base + "/api/state") as resp:
        return await resp.json()


async def test_toggle_on_and_off() -> None:
    print("\n[1] 开 → 关：状态跟着走，子进程真的起停")
    install_fakes()

    async def scenario(http, base, server):
        server.overlay_command = list(SLEEPER)
        s = await _state(http, base)
        check(s.get("overlay_running") is False, "一开始是关着的（按钮显示「悬浮显示」）",
              str(s.get("overlay_running")))

        status, data = await _post(http, base, "toggle")
        check(status == 200 and data.get("ok"), "开启成功", str(data))
        check(data.get("overlay_running") is True, "开启后状态是开着")
        proc = server._overlay_proc
        check(proc is not None and proc.poll() is None, "子进程活着", str(proc))
        check((await _state(http, base)).get("overlay_running") is True,
              "快照里带上 overlay_running（刷新页面后按钮还能对上）")

        status, data = await _post(http, base, "toggle")
        check(status == 200 and data.get("overlay_running") is False, "再点一次关掉",
              str(data))
        if proc is not None:
            for _ in range(20):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            check(proc.poll() is not None, "关掉之后子进程真的没了",
                  "returncode={}".format(proc.poll()))
        check(server._overlay_proc is None, "服务端不再持有它")

        # 显式动作也要能用（前端虽然只发 toggle）
        await _post(http, base, "start")
        check((await _state(http, base)).get("overlay_running") is True,
              "action=start 也能开")
        await _post(http, base, "stop")
        check((await _state(http, base)).get("overlay_running") is False,
              "action=stop 也能关")

    await with_server(scenario)


async def test_dead_child_is_reported() -> None:
    print("\n[2] 子进程自己挂了：状态要回到「关着」，而不是骗人")
    install_fakes()

    async def scenario(http, base, server):
        # 先开一个正常的
        server.overlay_command = list(SLEEPER)
        await _post(http, base, "toggle")
        check((await _state(http, base)).get("overlay_running") is True, "先正常开着")

        # 再模拟它自己退出（比如用户手动关掉窗口 / 崩了）
        proc = server._overlay_proc
        proc.terminate()
        await asyncio.sleep(0.6)
        s = await _state(http, base)
        check(s.get("overlay_running") is False,
              "子进程死了就报「没开」（按钮能重新点）", str(s.get("overlay_running")))
        check(server._overlay_proc is None, "并且把它从服务端摘掉")

        # 起一个立刻失败的：要给出可读的错误，而不是静默成功
        server.overlay_command = list(DIES)
        status, data = await _post(http, base, "toggle")
        check(status == 400 and not data.get("ok"), "起不来时返回错误",
              "HTTP {} {}".format(status, data.get("error")))
        check("退出码" in str(data.get("error")) or "没能启动" in str(data.get("error")),
              "错误里说清了是启动失败", str(data.get("error")))
        check(data.get("overlay_running") is False, "状态仍然是关着")

    await with_server(scenario)


async def test_command_gets_the_right_port() -> None:
    print("\n[3] 传给 overlay.py 的端口 = 服务实际监听的端口")
    install_fakes()
    out = Path(tempfile.mkdtemp(prefix="ibike-overlay-argv-")) / "argv.txt"
    fake = Path(tempfile.mkdtemp(prefix="ibike-overlay-fake-")) / "overlay.py"
    fake.write_text(
        "import sys, time\n"
        "open({!r}, 'w').write(' '.join(sys.argv[1:]))\n"
        "time.sleep(120)\n".format(str(out)), encoding="utf-8")

    original = server_mod.OVERLAY_SCRIPT
    server_mod.OVERLAY_SCRIPT = fake
    try:
        async def scenario(http, base, server):
            await _post(http, base, "toggle")
            for _ in range(20):
                if out.exists():
                    break
                await asyncio.sleep(0.1)
            argv = out.read_text(encoding="utf-8") if out.exists() else ""
            port = base.rsplit(":", 1)[1]
            check("--port" in argv, "命令里带了 --port", argv)
            check(port in argv, "端口和服务实际监听的一致（{}）".format(port), argv)
            await _post(http, base, "stop")

        await with_server(scenario)
    finally:
        server_mod.OVERLAY_SCRIPT = original


async def test_missing_script_gives_a_useful_error() -> None:
    print("\n[4] 找不到 overlay.py 时：要给出能照着做的提示")
    install_fakes()
    original = server_mod.OVERLAY_SCRIPT
    server_mod.OVERLAY_SCRIPT = Path("/nonexistent/overlay.py")
    try:
        async def scenario(http, base, server):
            status, data = await _post(http, base, "toggle")
            check(status == 400 and not data.get("ok"), "返回 400",
                  "HTTP {}".format(status))
            check("找不到" in str(data.get("error")),
                  "提示了原因", str(data.get("error")))

        await with_server(scenario)
    finally:
        server_mod.OVERLAY_SCRIPT = original


async def test_cleanup_kills_the_child() -> None:
    print("\n[5] 服务退出时要把悬浮窗一起收掉（不留孤儿窗口）")
    install_fakes()
    server = server_mod.Server(use_simulator=False)
    server.overlay_command = list(SLEEPER)
    app = server.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    proc = None
    try:
        async with ClientSession() as http:
            await http.post("http://127.0.0.1:{}/api/overlay".format(
                runner.addresses[0][1]), json={"action": "start"})
            proc = server._overlay_proc
        check(proc is not None and proc.poll() is None, "起来了")
        await server.cleanup(app)
        for _ in range(20):
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.1)
        check(proc.poll() is not None, "cleanup 之后子进程被收掉了",
              "returncode={}".format(proc.poll()))
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        await runner.cleanup()


async def test_overlay_self_exits_when_service_gone() -> None:
    """服务消失太久，悬浮窗要自己退出（不只靠服务退出时来杀它）。

    真机踩到过：关终端窗口发的是 SIGHUP、`kill` 发的是 SIGTERM，进程直接消失，
    没人去杀子进程 → 屏幕上挂着一个永远显示"iBike 未运行"的孤儿窗口。
    """
    print("\n[6] 服务消失后悬浮窗要自己退出")
    import overlay

    poller = overlay.StatePoller(1, exit_after_gone=0.2)   # 端口 1 上什么都没有
    try:
        await asyncio.sleep(0.3)
        check(not poller.should_exit(),
              "从没连上过时不退出（单独跑 ./run-overlay.sh 时应该等人）")
        # 模拟"连上过、然后服务没了"
        poller._ever_ok = True
        poller._last_ok = time.time() - 1.0
        check(poller.should_exit(), "连上过又断了超过阈值 → 该退出",
              "断了 {:.1f}s".format(poller.gone_for()))
        check("服务已停止" in poller.note(), "窗口上给出提示", poller.note())
        poller._last_ok = time.time()
        check(not poller.should_exit(), "刚拿到数据时不退出")
        check(poller.note() == "", "正常时不显示多余提示", poller.note())
        poller.exit_after_gone = 0.0
        poller._last_ok = time.time() - 100.0
        check(not poller.should_exit(), "--exit-after-gone 0 表示永不自动关闭")
    finally:
        poller.stop()


async def test_signal_leads_to_graceful_shutdown() -> None:
    """收到 SIGTERM/SIGHUP 时要收尾：杀悬浮窗、存报告，然后退出。"""
    print("\n[7] 收到 SIGTERM/SIGHUP 要走优雅收尾")
    install_fakes()
    import signal as _signal

    server = server_mod.Server(use_simulator=False)
    app = server.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        server.overlay_command = list(SLEEPER)
        # 注意：直接构造的 Server 不会接管信号、也不会 os._exit
        async with ClientSession() as http:
            await http.post("http://127.0.0.1:{}/api/overlay".format(
                runner.addresses[0][1]), json={"action": "start"})
        proc = server._overlay_proc
        if check(proc is not None and proc.poll() is None, "先有一个悬浮窗在跑"):
            server._on_terminate_signal(_signal.SIGTERM)
            check(server._terminating, "标记为正在收尾")
            server._on_terminate_signal(_signal.SIGTERM)   # 第二次应该是空操作
            for _ in range(40):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            check(proc.poll() is not None, "收尾把悬浮窗一起收掉了",
                  "returncode={}".format(proc.poll()))
            check(server._overlay_proc is None, "服务端也放手了")
        if proc is not None and proc.poll() is None:
            proc.kill()
    finally:
        await runner.cleanup()


async def main() -> int:
    print("=" * 70)
    print("悬浮窗开关测试（实验功能）")
    print("=" * 70)
    await test_toggle_on_and_off()
    await test_dead_child_is_reported()
    await test_command_gets_the_right_port()
    await test_missing_script_gives_a_useful_error()
    await test_cleanup_kills_the_child()
    await test_overlay_self_exits_when_service_gone()
    await test_signal_leads_to_graceful_shutdown()

    print("\n" + "=" * 70)
    if _failures:
        print("失败 {} 项：".format(len(_failures)))
        for f in _failures:
            print("   - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
