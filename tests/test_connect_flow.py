#!/usr/bin/env python3
"""连接流程的健壮性测试。

专门盯住一个很容易复现、又特别难查的故障：连接失败后服务端留下半成品状态，
前端因此认为"未连接"，开始按钮永远灰着，而用户看不到任何错误。

    python3 tests/test_connect_flow.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# 测试隔离：绝不能碰用户的真实数据目录。
# 训练报告是"训练一结束就自动保存"的，不隔离的话跑一次测试就会往
# data/reports/ 里留下一条真实报告。数据根目录在构造存储对象时读这个环境变量。
# ---------------------------------------------------------------------------
import os as _os
import tempfile as _tempfile

_os.environ.setdefault("IBIKE_DATA_DIR",
                       _tempfile.mkdtemp(prefix="ibike-test-data-"))

import ibike.server as server_mod  # noqa: E402
from aiohttp import ClientSession, web  # noqa: E402

_failures: List[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print("  {} {}{}".format("\033[32m✔\033[0m" if cond else "\033[31m✘\033[0m",
                             label, "  → " + detail if detail else ""))
    if not cond:
        _failures.append(label)
    return cond


class FakeTrainer:
    """可控的真机替身：可以指定 connect 是成功还是抛异常。"""

    should_fail = False
    fail_message = "连接失败：设备无响应"

    def __init__(self, on_event=None, on_data=None) -> None:
        self.on_event = on_event
        self.connected = False
        self.is_simulator = False
        self.name = "未连接"
        self.latest: Dict[str, Any] = {}
        self.has_control = False
        self.capabilities = {
            "has_control_point": True,
            "supports_power_target": True,
            "supports_resistance_target": True,
        }

    async def connect(self, address: str = "", timeout: float = 0.0) -> None:
        if self.should_fail:
            raise RuntimeError(self.fail_message)
        self.connected = True
        self.name = "假骑行台"

    async def disconnect(self) -> None:
        self.connected = False

    async def start(self) -> None:
        self.has_control = True

    async def pause(self) -> None:
        pass

    async def stop(self) -> None:
        self.has_control = False

    async def set_target_power(self, watts: int) -> None:
        pass

    async def set_resistance_raw(self, raw: int) -> None:
        pass

    def state(self) -> Dict[str, Any]:
        return {"kind": "ble", "connected": self.connected, "name": self.name,
                "capabilities": self.capabilities, "machine_info": {}, "has_control": self.has_control}


def install_fake_trainer() -> None:
    """把服务端用的 TrainerClient 和广播扫描都换成替身。

    `/api/scan` 现在走的是 `ibike.ble.scan_raw`（一次广播扫描，骑行台和心率带
    都从这一份结果里筛），所以两个入口都要替掉，否则测试会真的去开蓝牙。
    """

    class FakeTrainerClient:
        def __new__(cls, on_event=None, on_data=None):
            return FakeTrainer(on_event=on_event)

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


async def test_successful_connect() -> None:
    print("\n[1] 连接成功：快照里必须带上 trainer.connected")
    install_fake_trainer()
    FakeTrainer.should_fail = False

    async def scenario(http, base, server):
        async with http.post(base + "/api/connect", json={"address": "AA:BB"}) as resp:
            data = await resp.json()
        check(resp.status == 200 and data.get("ok"), "连接返回 ok")
        check((data.get("trainer") or {}).get("connected") is True,
              "响应里的 trainer.connected 为 True")
        # 前端靠这个字段点亮"开始训练"，所以它必须在同一个响应里返回
        check((data.get("state") or {}).get("trainer", {}).get("connected") is True,
              "响应同时带回完整快照（前端不必等 WebSocket）")

        async with http.get(base + "/api/state") as r:
            s = await r.json()
        check(s.get("trainer", {}).get("connected") is True,
              "/api/state 里的 trainer.connected 为 True")

    await with_server(scenario)


async def test_failed_connect_leaves_clean_state() -> None:
    print("\n[2] 连接失败：不能留下半成品状态")
    install_fake_trainer()
    FakeTrainer.should_fail = True

    async def scenario(http, base, server):
        async with http.post(base + "/api/connect", json={"address": "AA:BB"}) as resp:
            data = await resp.json()
        check(resp.status == 400 and data.get("ok") is False, "连接失败返回 400",
              str(data.get("error")))
        check(bool(data.get("error")), "错误信息不为空（前端会显示在消息条上）",
              str(data.get("error")))

        # 关键：服务端不能留下半死不活的 trainer/session
        check(server.trainer is None, "失败后 server.trainer 已清空")
        check(server.session is None, "失败后 server.session 已清空")

        async with http.get(base + "/api/state") as r:
            s = await r.json()
        check(s.get("state") == "idle", "状态回到 idle", str(s.get("state")))
        check(s.get("trainer", {}).get("connected") in (None, False),
              "快照里没有残留的已连接状态")

        # 失败之后重试应当能成功
        FakeTrainer.should_fail = False
        async with http.post(base + "/api/connect", json={"address": "AA:BB"}) as r:
            d = await r.json()
        check(d.get("ok") and (d.get("state") or {}).get("trainer", {}).get("connected") is True,
              "失败后重试可以正常连上")

    await with_server(scenario)


async def test_start_blocked_before_connect() -> None:
    print("\n[3] 未连接时开始训练应被明确拒绝")
    install_fake_trainer()
    FakeTrainer.should_fail = False

    async def scenario(http, base, server):
        async with http.post(base + "/api/start",
                             json={"target_power": 100, "duration_min": 30}) as resp:
            data = await resp.json()
        check(resp.status == 400 and data.get("ok") is False,
              "返回 400 而不是静默失败", str(data.get("error")))
        check("骑行台" in str(data.get("error")), "错误信息说明了原因",
              str(data.get("error")))

    await with_server(scenario)


async def test_input_validation() -> None:
    print("\n[4] 异常入参应当返回 400，而不是 500")
    install_fake_trainer()
    FakeTrainer.should_fail = False

    async def scenario(http, base, server):
        # 扫描超时参数不是数字：以前在 try 之外 float() 会抛 ValueError → 500。
        # 修好后应当退回默认值照常工作，关键是不能再出现 500。
        async with http.post(base + "/api/scan", json={"timeout": "abc"}) as resp:
            check(resp.status < 500, "非法的扫描超时参数不会造成 500",
                  "HTTP {}".format(resp.status))

        # course 传了个列表：normalize_course 会 AttributeError → 500
        async with http.post(base + "/api/plan", json={"course": [], "ftp": 200}) as resp:
            check(resp.status < 500, "非法的 course 参数不会造成 500",
                  "HTTP {}".format(resp.status))

        # 未连接时调 plan 不该炸
        async with http.post(base + "/api/plan",
                             json={"course": {"name": "x", "steps": [
                                 {"kind": "work", "duration_s": 60, "pct": 80}]},
                                 "ftp": 200}) as resp:
            check(resp.status == 200, "合法的草稿能正常预览",
                  "HTTP {}".format(resp.status))

    await with_server(scenario)


async def test_nan_target_is_rejected() -> None:
    print("\n[5] NaN 目标功率不能被\"夹\"成一个合法值")
    install_fake_trainer()
    FakeTrainer.should_fail = False

    async def scenario(http, base, server):
        async with http.post(base + "/api/connect", json={"address": "AA:BB"}) as r:
            await r.json()
        async with http.post(base + "/api/start",
                             json={"target_power": 100, "duration_min": 30}) as r:
            await r.json()

        # Python 的 min(2000.0, nan) 会得到 2000.0，所以 NaN 以前会被静默设成 2000W。
        # 直接发原始字节，绕开 json.dumps 对 NaN 的默认拒绝。
        async with http.post(base + "/api/target",
                             data='{"target_power": NaN}',
                             headers={"Content-Type": "application/json"}) as resp:
            data = await resp.json()
        check(resp.status == 400 and not data.get("ok"),
              "NaN 目标功率被拒绝", "HTTP {} {}".format(resp.status, data.get("error")))

        async with http.get(base + "/api/state") as r:
            st = await r.json()
        check(st.get("target_power") != 2000.0, "目标功率没有被改成 2000W",
              str(st.get("target_power")))
        await http.post(base + "/api/stop", json={})

    await with_server(scenario)


async def test_concurrent_stop_and_disconnect() -> None:
    print("\n[6] 并发 停止 + 断开 不应产生 500")
    install_fake_trainer()
    FakeTrainer.should_fail = False

    async def scenario(http, base, server):
        async with http.post(base + "/api/connect", json={"address": "AA:BB"}) as r:
            await r.json()
        async with http.post(base + "/api/start",
                             json={"target_power": 100, "duration_min": 30}) as r:
            await r.json()

        # stop() 内部要等骑行台应答，窗口里 disconnect 会把 self.session 置空，
        # 以前 stop 处理器随后读 self.session.summary 就是 AttributeError → 500
        stop_resp, disc_resp = await asyncio.gather(
            http.post(base + "/api/stop", json={}),
            http.post(base + "/api/disconnect", json={}),
        )
        check(stop_resp.status < 500, "并发停止没有 500",
              "HTTP {}".format(stop_resp.status))
        check(disc_resp.status < 500, "并发断开没有 500",
              "HTTP {}".format(disc_resp.status))

    await with_server(scenario)


async def main() -> int:
    print("=" * 70)
    print("连接流程健壮性测试")
    print("=" * 70)
    await test_successful_connect()
    await test_failed_connect_leaves_clean_state()
    await test_start_blocked_before_connect()
    await test_input_validation()
    await test_nan_target_is_rejected()
    await test_concurrent_stop_and_disconnect()

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
