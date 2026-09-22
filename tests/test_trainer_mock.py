#!/usr/bin/env python3
"""用假的 bleak 客户端驱动真实的 TrainerClient，覆盖真机代码路径。

`simulator.py` 走的是另一条分支，完全绕过了 `trainer.py`——也就是真正和蓝牙
打交道的那部分代码。这个测试把 `ibike.trainer.BleakClient` 换成一个模拟的
GATT 外设，让 `TrainerClient.connect/start/set_target_power` 这些方法真实执行，
从而验证：服务发现、能力探测、订阅、指令下发、应答匹配、数据解析。

    python3 tests/test_trainer_mock.py
"""

from __future__ import annotations

import asyncio
import struct
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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

from ibike import ftms, trainer as trainer_mod  # noqa: E402
from ibike.session import WorkoutSession  # noqa: E402
from ibike.trainer import TrainerClient, TrainerError  # noqa: E402

_failures: List[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print("  {} {}{}".format("\033[32m✔\033[0m" if cond else "\033[31m✘\033[0m",
                             label, "  → " + detail if detail else ""))
    if not cond:
        _failures.append(label)
    return cond


# ======================================================================
# 模拟 BLE 外设
# ======================================================================


class FakeChar:
    def __init__(self, uuid: str, properties: List[str]) -> None:
        self.uuid = uuid
        self.properties = properties
        self.handle = abs(hash(uuid)) % 10000


class FakeService:
    def __init__(self, uuid: str, chars: List[FakeChar]) -> None:
        self.uuid = uuid
        self.characteristics = chars


class FakeServices:
    def __init__(self, services: List[FakeService]) -> None:
        self._services = services

    def __iter__(self):
        return iter(self._services)


class FakeBleakClient:
    """一台符合 FTMS 规范的智能骑行台。

    ``quirks`` 用来模拟真机上的各种毛病：
      - no_feature_char: 不提供 Fitness Machine Feature 特征值
      - silent_control_point: 收下指令但从不回应答
      - control_not_permitted: 第一次下发目标功率返回 Control Not Permitted
    """

    # 记录最后一次被构造出来的实例，方便测试断言
    last_instance: Optional["FakeBleakClient"] = None

    def __init__(self, address: str, disconnected_callback: Optional[Callable] = None,
                 timeout: float = 10.0, quirks: Optional[Dict[str, bool]] = None,
                 **kwargs: Any) -> None:
        self.address = address
        self._disconnected_callback = disconnected_callback
        self._timeout = timeout
        self.quirks = quirks or {}
        self.connected = False
        self.notifications: Dict[str, Callable] = {}
        self.writes: List[tuple] = []
        self._services = self._build_services()
        FakeBleakClient.last_instance = self

    # -- GATT 表 -------------------------------------------------------

    def _build_services(self) -> FakeServices:
        dis = FakeService(ftms.DIS_SERVICE, [
            FakeChar(ftms.CHAR_MANUFACTURER_NAME, ["read"]),
            FakeChar(ftms.CHAR_MODEL_NUMBER, ["read"]),
            FakeChar(ftms.CHAR_FIRMWARE_REVISION, ["read"]),
        ])
        bas = FakeService(ftms.BAS_SERVICE, [
            FakeChar(ftms.CHAR_BATTERY_LEVEL, ["read", "notify"]),
        ])

        # 完全没有 FTMS，只有一个厂商私有服务
        if self.quirks.get("no_ftms_service"):
            vendor = FakeService("0000fff0-0000-1000-8000-00805f9b34fb", [
                FakeChar("0000fff1-0000-1000-8000-00805f9b34fb", ["notify"]),
                FakeChar("0000fff2-0000-1000-8000-00805f9b34fb", ["write"]),
            ])
            return FakeServices([dis, bas, vendor])

        ftms_chars = []
        # 有控制点但没有骑行数据
        if not self.quirks.get("no_bike_data"):
            ftms_chars.append(FakeChar(ftms.CHAR_INDOOR_BIKE_DATA, ["notify"]))
        ftms_chars += [
            FakeChar(ftms.CHAR_SUPPORTED_POWER_RANGE, ["read"]),
            FakeChar(ftms.CHAR_SUPPORTED_RESISTANCE_LEVEL_RANGE, ["read"]),
            FakeChar(ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT, ["write", "indicate"]),
            FakeChar(ftms.CHAR_FITNESS_MACHINE_STATUS, ["notify"]),
        ]
        if not self.quirks.get("no_feature_char"):
            ftms_chars.insert(0, FakeChar(ftms.CHAR_FITNESS_MACHINE_FEATURE, ["read"]))
        ftms_service = FakeService(ftms.FTMS_SERVICE, ftms_chars)
        return FakeServices([dis, bas, ftms_service])

    # -- BleakClient 接口 ----------------------------------------------

    async def connect(self, **kwargs: Any) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        if self._disconnected_callback is not None:
            self._disconnected_callback(self)

    @property
    def services(self) -> FakeServices:
        return self._services

    async def read_gatt_char(self, uuid: Any) -> bytearray:
        u = getattr(uuid, "uuid", uuid)
        if u == ftms.CHAR_MANUFACTURER_NAME:
            return bytearray(b"MOKFITNESS")
        if u == ftms.CHAR_MODEL_NUMBER:
            return bytearray(b"iBike-S10")
        if u == ftms.CHAR_FIRMWARE_REVISION:
            return bytearray(b"1.2.3")
        if u == ftms.CHAR_BATTERY_LEVEL:
            return bytearray([88])
        if u == ftms.CHAR_FITNESS_MACHINE_FEATURE:
            # 低 32 位：踏频(1) + 功率(14)；高 32 位：目标阻力(2) + 目标功率(3)
            return bytearray(struct.pack("<I", (1 << 1) | (1 << 14))
                             + struct.pack("<I", (1 << 2) | (1 << 3)))
        if u == ftms.CHAR_SUPPORTED_POWER_RANGE:
            return bytearray(struct.pack("<hhH", 0, 1500, 1))
        if u == ftms.CHAR_SUPPORTED_RESISTANCE_LEVEL_RANGE:
            return bytearray(struct.pack("<BBB", 0, 255, 1))
        raise ValueError("未知特征值 {}".format(u))

    async def start_notify(self, uuid: Any, callback: Callable, **kwargs: Any) -> None:
        u = getattr(uuid, "uuid", uuid)
        if not self.connected:
            raise RuntimeError("未连接")
        self.notifications[u] = callback

    async def stop_notify(self, uuid: Any) -> None:
        self.notifications.pop(getattr(uuid, "uuid", uuid), None)

    async def write_gatt_char(self, uuid: Any, data: Any, response: Optional[bool] = None) -> None:
        u = getattr(uuid, "uuid", uuid)
        payload = bytes(data)
        self.writes.append((u, payload))
        if u != ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT:
            return
        if self.quirks.get("silent_control_point"):
            return
        op = payload[0]
        result = 0x01
        if op == ftms.OP_SET_TARGET_POWER and self.quirks.get("control_not_permitted"):
            # 只拒绝第一次，重申请控制权后就正常了
            self.quirks["control_not_permitted"] = False
            result = 0x05
        reply = bytes([ftms.RESPONSE_CODE, op, result])
        if op == ftms.OP_REQUEST_CONTROL:
            reply += bytes([0x01])
        await self._emit(ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT, reply)

    async def _emit(self, uuid: str, payload: bytes) -> None:
        cb = self.notifications.get(uuid)
        if cb is None:
            return
        await asyncio.sleep(self.quirks.get("response_delay", 0.01))
        result = cb(FakeChar(uuid, []), bytearray(payload))
        if asyncio.iscoroutine(result):
            await result

    # -- 测试辅助 ------------------------------------------------------

    async def push_bike_data(self, power: int, cadence: float, speed_kmh: float = 30.0) -> None:
        flags = ftms.FLAG_INST_CADENCE | ftms.FLAG_INST_POWER
        payload = struct.pack("<H", flags) + struct.pack("<H", int(speed_kmh * 100)) \
            + struct.pack("<H", int(cadence * 2)) + struct.pack("<h", power)
        await self._emit(ftms.CHAR_INDOOR_BIKE_DATA, payload)

    def ops_sent(self) -> List[int]:
        return [p[0] for u, p in self.writes if u == ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT]


# ======================================================================
# 测试
# ======================================================================


def install_fake(quirks: Optional[Dict[str, bool]] = None) -> None:
    """把 trainer 模块里的 BleakClient 换成假的。"""

    def factory(address, disconnected_callback=None, timeout=10.0, **kwargs):
        return FakeBleakClient(address, disconnected_callback=disconnected_callback,
                               timeout=timeout, quirks=quirks)

    trainer_mod.BleakClient = factory


async def test_connect() -> None:
    print("\n[1] 真机代码路径：连接与能力探测")
    install_fake()
    t = TrainerClient()
    await t.connect("AA:BB:CC:DD:EE:FF")

    check(t.connected is True, "连接后 connected 为 True")
    st = t.state()
    check(st["connected"] is True, "state() 里 connected 为 True（前端靠这个点亮按钮）",
          str(st["connected"]))
    check(st["kind"] == "ble", "kind 正确标记为 ble", str(st["kind"]))
    check(st["name"] == "MOKFITNESS iBike-S10", "读到了厂商与型号", st["name"])

    caps = t.capabilities
    check(caps["has_control_point"] is True, "识别出控制点")
    check(caps["supports_power_target"] is True, "识别出支持目标功率")
    check(caps["supports_resistance_target"] is True, "识别出支持目标阻力")
    check(caps["power_range"] == {"min": 0, "max": 1500, "increment": 1},
          "解析出功率范围", str(caps["power_range"]))
    check(caps["resistance_range"]["max_raw"] == 255, "解析出阻力范围")
    check(st["machine_info"]["battery"] == 88, "读到电量", str(st["machine_info"]["battery"]))

    client = FakeBleakClient.last_instance
    assert client is not None
    check(ftms.CHAR_INDOOR_BIKE_DATA in client.notifications, "订阅了 Indoor Bike Data")
    check(ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT in client.notifications,
          "订阅了控制点应答（indicate）")

    # 数据解析要能一路走到 latest
    await client.push_bike_data(150, 90.0, 32.5)
    await asyncio.sleep(0.05)
    check(t.latest.get("power_w") == 150, "功率数据解析正确", str(t.latest.get("power_w")))
    check(t.latest.get("cadence_rpm") == 90.0, "踏频数据解析正确", str(t.latest.get("cadence_rpm")))

    await t.disconnect()
    check(t.connected is False, "断开后 connected 为 False")


async def test_control_sequence() -> None:
    print("\n[2] 真机代码路径：控制指令序列")
    install_fake()
    t = TrainerClient()
    await t.connect("AA:BB:CC:DD:EE:FF")
    client = FakeBleakClient.last_instance
    assert client is not None

    await t.start()
    ops = client.ops_sent()
    check(ops[:2] == [ftms.OP_REQUEST_CONTROL, ftms.OP_START_OR_RESUME],
          "start() 先申请控制权再 Start/Resume",
          " ".join(hex(o) for o in ops))
    check(t.has_control is True, "拿到控制权标记")

    await t.set_target_power(100)
    check(ftms.OP_SET_TARGET_POWER in client.ops_sent(), "下发了目标功率")
    last = [p for u, p in client.writes if u == ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT][-1]
    check(last == b"\x05\x64\x00", "目标功率编码正确", last.hex(" "))

    await t.set_resistance_raw(120)
    last = [p for u, p in client.writes if u == ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT][-1]
    check(last == b"\x04\x78", "阻力原始值编码正确", last.hex(" "))

    await t.stop()
    check(ftms.OP_STOP_OR_PAUSE in client.ops_sent(), "停止指令已下发")

    await t.disconnect()


async def test_control_not_permitted() -> None:
    print("\n[3] 真机代码路径：控制权被拒后自动重申请")
    install_fake({"control_not_permitted": True})
    t = TrainerClient()
    await t.connect("AA:BB:CC:DD:EE:FF")
    client = FakeBleakClient.last_instance
    assert client is not None

    await t.start()
    before = client.ops_sent().count(ftms.OP_SET_TARGET_POWER)
    await t.set_target_power(100)   # 第一次失败 -> 重申请 -> 重发
    after = client.ops_sent().count(ftms.OP_SET_TARGET_POWER)
    check(after - before >= 2, "Control Not Permitted 后重新申请并重发",
          "SET_TARGET_POWER 下发 {} 次".format(after - before))
    check(client.ops_sent().count(ftms.OP_REQUEST_CONTROL) >= 2, "重新申请了控制权")

    await t.disconnect()


async def test_silent_control_point() -> None:
    print("\n[4] 真机代码路径：固件不回应答时不应卡死")
    install_fake({"silent_control_point": True})
    t = TrainerClient()
    await t.connect("AA:BB:CC:DD:EE:FF")

    import time
    started = time.time()
    await t.start()          # 不应抛异常，也不应无限等待
    elapsed = time.time() - started
    check(elapsed < 30.0, "无应答时仍能在有限时间内返回", "{:.1f}s".format(elapsed))

    # 关键：训练主循环里保活指令每 2 秒发一次，如果每次都等满超时，主循环会被
    # 彻底堵死。连续几次收不到应答后就该放宽等待。
    for _ in range(3):
        await t.set_target_power(150)      # 前几次可能还在用较长的超时
    ramp_started = time.time()
    for _ in range(5):
        await t.set_target_power(150)
    ramp = time.time() - ramp_started
    check(t._response_timeout == trainer_mod.CP_FAST_TIMEOUT_S,
          "连续超时后等待时间自动放宽", "{:.1f}s".format(t._response_timeout))
    check(ramp < 2.5, "放宽后 5 次下发不再逐次等满超时（否则要 12 秒以上）",
          "{:.1f}s".format(ramp))

    await t.disconnect()


async def test_no_feature_char() -> None:
    print("\n[5] 真机代码路径：没有 Feature 特征值（廉价固件）")
    install_fake({"no_feature_char": True})
    t = TrainerClient()
    await t.connect("AA:BB:CC:DD:EE:FF")
    check(t.connected is True, "仍然连接成功")
    check(t.capabilities["supports_power_target"] is True,
          "缺声明时保守地按“支持目标功率”处理，留给上层实际试",
          str(t.capabilities["supports_power_target"]))
    await t.disconnect()


async def test_session_over_real_path() -> None:
    print("\n[6] 真机代码路径：完整训练会话")
    install_fake()
    t = TrainerClient()
    await t.connect("AA:BB:CC:DD:EE:FF")
    client = FakeBleakClient.last_instance
    assert client is not None

    session = WorkoutSession(t)
    await session.start(100.0, duration_min=1.0, erg_mode="auto")
    check(session.active_erg_mode == "ftms", "自动模式选中原生 ERG",
          str(session.active_erg_mode))

    await client.push_bike_data(98, 88.0)
    await asyncio.sleep(0.3)
    snap = session.snapshot()
    check(snap["power"] == 98, "会话读到功率", str(snap["power"]))
    check(snap["state"] == "running", "会话状态为 running", str(snap["state"]))
    # 关键：前端按钮依赖这个字段
    check(snap["trainer"]["connected"] is True,
          "快照里 trainer.connected 为 True（前端据此启用“开始训练”）")

    await session.aclose()
    await t.disconnect()


async def test_not_an_ftms_device() -> None:
    """这台设备根本不是 FTMS——正是最容易被误判成"连上了却用不了"的情况。"""
    print("\n[7] 真机代码路径：不是 FTMS 设备时必须给出可诊断的错误")
    install_fake({"no_ftms_service": True})
    t = TrainerClient()
    try:
        await t.connect("AA:BB:CC:DD:EE:FF")
        check(False, "非 FTMS 设备应当连接失败")
    except TrainerError as exc:
        msg = str(exc)
        check("FTMS" in msg, "错误信息点明了缺少 FTMS", msg[:60] + "…")
        check("1826" not in msg or "实际暴露" in msg, "错误信息给出了下一步怎么做")
        # 关键：错误里必须带上实际的服务列表，用户才能直接反馈问题
        check("fff0" in msg, "错误信息里包含设备实际暴露的服务 UUID（可据此排查）",
              msg[msg.find("实际暴露"):][:80] if "实际暴露" in msg else msg[:80])
    check(t.connected is False, "失败后没有留下已连接状态")


async def test_control_point_without_bike_data() -> None:
    print("\n[8] 真机代码路径：有控制点但没有骑行数据")
    install_fake({"no_bike_data": True})
    t = TrainerClient()
    try:
        await t.connect("AA:BB:CC:DD:EE:FF")
        check(False, "缺少骑行数据时应当连接失败（否则开训练会毫无反应）")
    except TrainerError as exc:
        msg = str(exc)
        check("骑行数据" in msg, "错误信息说明了缺少骑行数据", msg[:50] + "…")
        check("2ad9" in msg, "错误信息里列出了它实际有的特征值（便于定位）",
              msg[msg.find("实际暴露"):][:90] if "实际暴露" in msg else msg[:90])


async def test_slow_but_working_firmware() -> None:
    """响应慢但确实会应答的固件，不应该被当成"不回应答"。

    这一点很重要：应答里没有请求序号，一旦放宽到很短的超时，上一个请求的迟到应答
    就会被算到下一个请求头上，把上一次的失败结果报成这一次的失败。
    """
    print("\n[9] 真机代码路径：响应慢但正常的固件不触发「不再等待应答」")
    install_fake({"response_delay": 0.5})
    t = TrainerClient()
    await t.connect("AA:BB:CC:DD:EE:FF")
    t._response_timeout = 0.3        # 故意设得比固件响应时间还短

    for _ in range(6):
        try:
            await t.set_target_power(120)
        except TrainerError:
            pass
        await asyncio.sleep(0.6)

    check(t._cp_responded is True, "确认这个固件其实是会应答的")
    check(t._response_timeout != trainer_mod.CP_FAST_TIMEOUT_S,
          "没有因为超时就把等待时间放宽",
          "当前 {:.1f}s".format(t._response_timeout))
    await t.disconnect()


async def main() -> int:
    print("=" * 70)
    print("TrainerClient 真机代码路径测试（用模拟 GATT 外设）")
    print("=" * 70)

    await test_connect()
    await test_control_sequence()
    await test_control_not_permitted()
    await test_silent_control_point()
    await test_no_feature_char()
    await test_session_over_real_path()
    await test_not_an_ftms_device()
    await test_control_point_without_bike_data()
    await test_slow_but_working_firmware()

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
