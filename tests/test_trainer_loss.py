#!/usr/bin/env python3
"""骑行台掉线 / 重连的测试。

盯的是一个真实发生过的、后果很严重的问题：

    60 分钟 130W 的恒定功率训练，中途暂停休息了一会儿。休息时间长，动感单车
    自动关机，蓝牙链路断掉。用户回到设备页重新连上骑行台——**训练已经结束了**，
    21 分钟处变成一份"提前结束"的报告，再也接不回去。

成因是链路分明：掉线的时候服务端什么都不做（秒表还在跑），而"重新连接"走的
是 _connect_trainer → _disconnect_trainer，那条路会把没骑完的训练收尾并写报告。

修好之后的约定：
  * 掉线 = 把训练**挂起**（冻结计时、保住数据），不是结束；
  * 重新连上骑行台 = 接回这场训练（已骑时长、曲线、心率累计原样保留）；
  * 只有「结束」按钮 / 跑满时长 / 显式点「断开」，才会收尾并写报告。

    python3 tests/test_trainer_loss.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# 测试隔离：绝不能碰用户的真实数据目录。报告是"训练一结束就自动保存"的，
# 不隔离的话跑一次测试就会往 data/reports/ 里留下一条真实报告。
# ---------------------------------------------------------------------------
import os as _os
import tempfile as _tempfile

_os.environ.setdefault("IBIKE_DATA_DIR",
                       _tempfile.mkdtemp(prefix="ibike-test-data-"))

import ibike.server as server_mod  # noqa: E402
from aiohttp import ClientSession, web  # noqa: E402
from ibike.trainer import TrainerError  # noqa: E402

_failures: List[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print("  {} {}{}".format("\033[32m✔\033[0m" if cond else "\033[31m✘\033[0m",
                             label, "  → " + detail if detail else ""))
    if not cond:
        _failures.append(label)
    return cond


class FakeTrainer:
    """可控的真机替身：能喂数据、能在骑到一半时"断电"。"""

    should_fail = False
    fail_message = "连接失败：设备无响应"

    def __init__(self, on_event=None, on_data=None) -> None:
        self.on_event = on_event
        self.connected = False
        self.is_simulator = False
        self.name = "假骑行台"
        self.latest: Dict[str, Any] = {}
        self.last_data_time = 0.0
        self.has_control = False
        self.capabilities = {
            "has_control_point": True,
            "supports_power_target": True,
            "supports_resistance_target": True,
            "resistance_range": {"min_raw": 0, "max_raw": 255},
        }
        # 记下收到的指令，「继续」之后必须真的重新下发目标功率
        self.commands: List[Any] = []

    # -- 连接 ----------------------------------------------------------

    async def connect(self, address: str = "", timeout: float = 0.0) -> None:
        if FakeTrainer.should_fail:
            raise RuntimeError(FakeTrainer.fail_message)
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    # -- 控制（未连接时和真机一样抛 TrainerError） -----------------------

    def _require(self) -> None:
        if not self.connected:
            raise TrainerError("骑行台未连接")

    async def start(self) -> None:
        self._require()
        self.has_control = True
        self.commands.append(("start",))

    async def pause(self) -> None:
        self._require()
        self.commands.append(("pause",))

    async def stop(self) -> None:
        self.commands.append(("stop",))
        self.has_control = False

    async def set_target_power(self, watts: int) -> None:
        self._require()
        self.commands.append(("power", watts))

    async def set_resistance_raw(self, raw: int) -> None:
        self._require()
        self.commands.append(("resistance", raw))

    def state(self) -> Dict[str, Any]:
        return {"kind": "ble", "connected": self.connected, "name": self.name,
                "capabilities": self.capabilities, "machine_info": {},
                "has_control": self.has_control}

    # -- 测试用的helper -------------------------------------------------

    def feed(self, power: float, cadence: float = 85.0) -> None:
        """推一帧数据（真机大约每 0.2 秒推一次）。"""
        self.latest = {"power_w": power, "cadence_rpm": cadence, "speed_kmh": 22.0}
        self.last_data_time = time.monotonic()

    def drop(self) -> None:
        """模拟台子自动关机：链路断掉。

        这就是 TrainerClient._handle_disconnect 做的事——先清掉连接标志和
        latest（否则上层会拿冻结的功率继续积分），然后报一个 disconnected 事件。
        """
        self.connected = False
        self.has_control = False
        self.latest = {}
        self.last_data_time = 0.0
        if self.on_event is not None:
            self.on_event("disconnected", "与骑行台的连接断开了")


def install_fake_trainer() -> None:
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


async def _reports(http, base) -> list:
    async with http.get(base + "/api/reports") as r:
        return (await r.json()).get("reports") or []


async def _state(http, base) -> Dict[str, Any]:
    async with http.get(base + "/api/state") as r:
        return await r.json()


async def _feed_until(server, seconds: float, power: float = 130.0) -> None:
    """在这段时间里持续喂数据，让会话真的采到功率、写出 trace。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        trainer = server.trainer
        if trainer is not None and trainer.connected:
            trainer.feed(power)
        await asyncio.sleep(0.05)


async def _connect(http, base) -> Dict[str, Any]:
    async with http.post(base + "/api/connect", json={"address": "AA:BB"}) as r:
        return await r.json()


async def _start(http, base, minutes: float = 60.0, power: float = 130.0) -> None:
    async with http.post(base + "/api/start",
                         json={"target_power": power,
                               "duration_min": minutes}) as r:
        await r.json()


async def test_pause_then_drop_suspends() -> None:
    print("\n[1] 暂停中台子断电：训练要挂起，不能结束")
    install_fake_trainer()
    FakeTrainer.should_fail = False

    async def scenario(http, base, server):
        reports0 = len(await _reports(http, base))
        await _connect(http, base)
        await _start(http, base)
        feeder = asyncio.ensure_future(_feed_until(server, 1.4))
        await asyncio.sleep(1.5)
        await feeder

        s = await _state(http, base)
        check(s.get("state") == "running", "训练在跑", str(s.get("state")))
        check(s.get("elapsed_s", 0) > 1.0, "计时在走",
              "{:.1f}s".format(s.get("elapsed_s") or 0))

        # 手动静止休息
        async with http.post(base + "/api/pause", json={}) as r:
            await r.json()
        paused_elapsed = (await _state(http, base)).get("elapsed_s")

        # 休息时间太长，台子自动关机
        server.trainer.drop()
        await asyncio.sleep(0.3)

        s = await _state(http, base)
        check(s.get("state") == "paused", "掉线后状态是暂停（挂起），不是结束",
              str(s.get("state")))
        check(s.get("trainer_lost") is True, "快照里标出了「骑行台掉线」")
        check(server.session is not None, "会话还在（没有被扔掉）")
        check(len(await _reports(http, base)) == reports0,
              "掉线**没有**写出报告（这不是训练结束）",
              "{} → {} 份".format(reports0, len(await _reports(http, base))))
        check(abs((s.get("elapsed_s") or 0) - (paused_elapsed or 0)) < 0.5,
              "挂起期间计时冻结", "{} → {}".format(paused_elapsed, s.get("elapsed_s")))

    await with_server(scenario)


async def test_reconnect_rebinds_ride() -> None:
    print("\n[2] 掉线后重新连接：接回这场训练，不是开一场新的")
    install_fake_trainer()
    FakeTrainer.should_fail = False

    async def scenario(http, base, server):
        reports0 = len(await _reports(http, base))
        await _connect(http, base)
        await _start(http, base)
        feeder = asyncio.ensure_future(_feed_until(server, 1.4))
        await asyncio.sleep(1.5)
        await feeder
        async with http.post(base + "/api/pause", json={}) as r:
            await r.json()
        server.trainer.drop()
        await asyncio.sleep(0.2)

        before = await _state(http, base)
        session = server.session
        trace_before = len(session._trace)
        check(trace_before > 0, "掉线前已经有功率曲线点", "{} 个".format(trace_before))

        # ---- 用户回设备页，重新连上骑行台 ----
        info = await _connect(http, base)
        check(info.get("ok"), "重连成功")
        check(len(await _reports(http, base)) == reports0,
              "重连**没有**写出报告（以前这里就是训练被结束的地方）",
              "{} → {} 份".format(reports0, len(await _reports(http, base))))
        check(server.session is session, "还是同一个会话（不是新建的）")
        check(server.session.trainer is server.trainer, "会话已经绑到新连上的骑行台")

        s = info.get("state") or {}
        check(s.get("state") == "paused", "状态仍是暂停，等着用户点「继续」",
              str(s.get("state")))
        check(s.get("trainer_lost") is False, "「掉线」标志已清掉")
        check(not s.get("error"),
              "掉线那一刻攒下的错误已清掉（否则会顶掉「继续」的提示）",
              str(s.get("error")))
        check((s.get("elapsed_s") or 0) >= (before.get("elapsed_s") or 0) - 0.1,
              "已骑时长保留了",
              "{} → {}".format(before.get("elapsed_s"), s.get("elapsed_s")))
        check(len(server.session._trace) >= trace_before,
              "功率曲线点保留了", "{} 个".format(len(server.session._trace)))

        # ---- 点「继续」----
        server.trainer.commands.clear()
        async with http.post(base + "/api/resume", json={}) as r:
            await r.json()
        await asyncio.sleep(0.2)
        s = await _state(http, base)
        check(s.get("state") == "running", "「继续」之后训练重新开始", str(s.get("state")))
        check(("power", 130) in server.trainer.commands,
              "重新下发了目标功率（新链路的 FTMS 状态是干净的）",
              str(server.trainer.commands[:6]))

        # 计时接着往前走
        e0 = s.get("elapsed_s") or 0
        await _feed_until(server, 0.5)
        e1 = (await _state(http, base)).get("elapsed_s") or 0
        check(e1 > e0, "计时接着往下走", "{:.1f} → {:.1f}".format(e0, e1))

        # 收尾：结束并保存，报告里应该有完整的这一段
        async with http.post(base + "/api/stop", json={}) as r:
            await r.json()
        reports = await _reports(http, base)
        check(len(reports) == reports0 + 1, "结束之后才写出一份报告",
              "{} → {} 份".format(reports0, len(reports)))
        if reports:
            check(reports[0].get("reason") == "用户停止", "报告原因是用户停止",
                  str(reports[0].get("reason")))

    await with_server(scenario)


async def test_running_drop_freezes_clock() -> None:
    print("\n[3] 骑到一半直接掉线（没手动暂停）：秒表必须停")
    install_fake_trainer()
    FakeTrainer.should_fail = False

    async def scenario(http, base, server):
        await _connect(http, base)
        await _start(http, base)
        feeder = asyncio.ensure_future(_feed_until(server, 0.8))
        await asyncio.sleep(0.9)
        await feeder

        server.trainer.drop()
        await asyncio.sleep(0.2)
        e0 = (await _state(http, base)).get("elapsed_s") or 0
        # 台子没了以后"休息"了 0.8 秒：这段时间绝不能算进骑行时长
        await asyncio.sleep(0.8)
        s = await _state(http, base)
        e1 = s.get("elapsed_s") or 0
        check(e1 - e0 < 0.3, "掉线期间计时冻结（没有把休息算成骑行）",
              "{:.2f} → {:.2f}".format(e0, e1))
        check(s.get("state") == "paused", "自动转为暂停（挂起）", str(s.get("state")))
        check(s.get("trainer_lost") is True, "「掉线」标志已置上")

    await with_server(scenario)


async def test_failed_reconnect_keeps_ride() -> None:
    print("\n[4] 挂起状态下重连失败：训练不能跟着一起丢")
    install_fake_trainer()
    FakeTrainer.should_fail = False

    async def scenario(http, base, server):
        await _connect(http, base)
        await _start(http, base)
        feeder = asyncio.ensure_future(_feed_until(server, 1.2))
        await asyncio.sleep(1.3)
        await feeder
        async with http.post(base + "/api/pause", json={}) as r:
            await r.json()
        session = server.session
        elapsed = (await _state(http, base)).get("elapsed_s")
        server.trainer.drop()
        await asyncio.sleep(0.2)

        # 用户点了一次连不上的设备
        FakeTrainer.should_fail = True
        async with http.post(base + "/api/connect", json={"address": "AA:BB"}) as r:
            data = await r.json()
        check(r.status == 400 and not data.get("ok"), "重连失败返回 400",
              str(data.get("error")))
        check(server.session is session, "训练会话没有因为一次失败的连接被扔掉")
        s = await _state(http, base)
        check(s.get("state") == "paused", "状态还是暂停", str(s.get("state")))
        check(abs((s.get("elapsed_s") or 0) - (elapsed or 0)) < 0.5,
              "已骑时长没丢",
              "{} → {}".format(elapsed, s.get("elapsed_s")))

        # 再试一次，这次能连上
        FakeTrainer.should_fail = False
        await _connect(http, base)
        s = await _state(http, base)
        check(s.get("state") == "paused" and not s.get("trainer_lost"),
              "重试成功后接回了这场训练", str(s.get("state")))
        async with http.post(base + "/api/stop", json={}) as r:
            await r.json()

    await with_server(scenario)


async def test_explicit_disconnect_still_saves() -> None:
    print("\n[5] 显式点「断开」：仍然是「结束并保存」（老行为不能丢）")
    install_fake_trainer()
    FakeTrainer.should_fail = False

    async def scenario(http, base, server):
        reports0 = len(await _reports(http, base))
        await _connect(http, base)
        await _start(http, base)
        feeder = asyncio.ensure_future(_feed_until(server, 1.2))
        await asyncio.sleep(1.3)
        await feeder

        async with http.post(base + "/api/disconnect", json={}) as r:
            check(r.status == 200, "断开接口正常返回", "HTTP {}".format(r.status))
        check(server.session is None, "会话已收起")
        reports = await _reports(http, base)
        check(len(reports) == reports0 + 1, "断开时把没骑完的训练存成了报告",
              "{} → {} 份".format(reports0, len(reports)))
        if reports:
            check(reports[0].get("reason") == "断开连接", "报告原因是断开连接",
                  str(reports[0].get("reason")))

    await with_server(scenario)


async def test_new_ride_after_loss_is_clean() -> None:
    print("\n[6] 挂起的训练结束之后，开始新训练不能带上旧数据")
    install_fake_trainer()
    FakeTrainer.should_fail = False

    async def scenario(http, base, server):
        await _connect(http, base)
        await _start(http, base)
        feeder = asyncio.ensure_future(_feed_until(server, 1.2))
        await asyncio.sleep(1.3)
        await feeder
        server.trainer.drop()
        await asyncio.sleep(0.2)
        old_elapsed = (await _state(http, base)).get("elapsed_s") or 0
        check(old_elapsed > 1.0, "第一场确实骑了一会儿",
              "{:.1f}s".format(old_elapsed))

        # 台子回来了，但用户不接着骑，重新开始一场
        await _connect(http, base)
        await _start(http, base, minutes=30, power=100)
        s = await _state(http, base)
        check(s.get("state") == "running", "新训练开始", str(s.get("state")))
        check((s.get("elapsed_s") or 0) < 1.0, "新训练的计时从零开始",
              "{:.1f}s".format(s.get("elapsed_s") or 0))
        check(s.get("trainer_lost") is False, "没有残留的掉线标志")
        check(s.get("target_power") == 100.0, "目标功率是新设的",
              str(s.get("target_power")))
        async with http.post(base + "/api/stop", json={}) as r:
            await r.json()

    await with_server(scenario)


async def main() -> int:
    print("=" * 70)
    print("骑行台掉线 / 重连测试")
    print("=" * 70)
    await test_pause_then_drop_suspends()
    await test_reconnect_rebinds_ride()
    await test_running_drop_freezes_clock()
    await test_failed_reconnect_keeps_ride()
    await test_explicit_disconnect_still_saves()
    await test_new_ride_after_loss_is_clean()

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
