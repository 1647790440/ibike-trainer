#!/usr/bin/env python3
"""心率带（标准蓝牙心率服务）的测试。

    python3 tests/test_heartrate.py
"""

from __future__ import annotations

import asyncio
import os as _os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# 测试隔离：绝不能碰用户的真实数据目录
# ---------------------------------------------------------------------------
_os.environ.setdefault("IBIKE_DATA_DIR",
                       tempfile.mkdtemp(prefix="ibike-test-data-"))

from ibike.heartrate import (HR_MODE_MAX, HR_MODE_RESERVE,  # noqa: E402
                             SimulatedHeartRate, hr_zone_of, hr_zones,
                             parse_heart_rate_measurement)
from ibike.session import WorkoutSession  # noqa: E402
from ibike.simulator import SimulatedTrainer  # noqa: E402

_failures: List[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print("  {} {}{}".format("\033[32m✔\033[0m" if cond else "\033[31m✘\033[0m",
                             label, "  → " + detail if detail else ""))
    if not cond:
        _failures.append(label)
    return cond


def frame(flags: int, *chunks: bytes) -> bytes:
    return bytes([flags]) + b"".join(chunks)


def u8(v: int) -> bytes:
    return bytes([v])


def u16(v: int) -> bytes:
    return v.to_bytes(2, "little")


# ======================================================================
# 1. 0x2A37 解析
# ======================================================================


def test_parse() -> None:
    print("\n[1] 心率测量帧解析（0x2A37）")

    # 只有 8 位心率
    got = parse_heart_rate_measurement(frame(0x00, u8(150)))
    check(got["heart_rate_bpm"] == 150, "8 位心率", str(got["heart_rate_bpm"]))
    check(got["rr_intervals_ms"] == [], "没有 RR 位时不解析 RR")

    # 16 位心率
    got = parse_heart_rate_measurement(frame(0x01, u16(180)))
    check(got["heart_rate_bpm"] == 180, "16 位心率", str(got["heart_rate_bpm"]))

    # 能量消耗字段（有它就说明帧里多 2 字节，跳过之后才轮到 RR）
    got = parse_heart_rate_measurement(frame(0x09, u16(150), u16(100)))
    check(got["heart_rate_bpm"] == 150 and got["energy_kj"] == 100,
          "跳过能量消耗字段", "hr={} kJ={}".format(got["heart_rate_bpm"], got["energy_kj"]))

    # RR 间期：单位 1/1024 秒
    got = parse_heart_rate_measurement(
        frame(0x19, u16(150), u16(0), u16(1024), u16(512)))
    check(got["rr_intervals_ms"] == [1000.0, 500.0], "RR 换算成毫秒（1/1024 秒）",
          str(got["rr_intervals_ms"]))

    # 坏 RR 被丢掉，但心率照常给出来
    got = parse_heart_rate_measurement(
        frame(0x10, u8(120), u16(10), u16(1024)))
    check(got["heart_rate_bpm"] == 120 and got["rr_intervals_ms"] == [1000.0],
          "明显不合理的 RR 被丢掉，不拖累心率", str(got["rr_intervals_ms"]))

    # 接触状态
    check(parse_heart_rate_measurement(frame(0x06, u8(100)))["contact"] == "已接触",
          "解析电极接触状态")

    # 截断帧不能抛异常——通知回调里抛异常会把订阅整个搞掉
    for bad in (b"", b"\x01", b"\x01\x2c", b"\x19\x2c\x01"):
        try:
            parse_heart_rate_measurement(bad)
            ok = True
        except Exception as exc:            # noqa: BLE001
            ok, _ = False, exc
        check(ok, "截断帧 {} 不抛异常".format(bad.hex() or "(空)"))

    # 物理上不可能的心率要丢掉，而不是照单全收
    got = parse_heart_rate_measurement(frame(0x00, u8(5)))
    check(got["heart_rate_bpm"] is None and got.get("implausible") == 5,
          "心率 5bpm 判为坏帧")
    got = parse_heart_rate_measurement(frame(0x01, u16(300)))
    check(got["heart_rate_bpm"] is None, "心率 300bpm 判为坏帧")


# ======================================================================
# 2. 心率区间
# ======================================================================


def test_zones() -> None:
    print("\n[2] 心率区间")

    bands = hr_zones(200)
    check(bands is not None and len(bands) == 5, "按最大心率切出 5 个区")
    check(bands[0]["min_bpm"] == 0.0 and bands[0]["max_bpm"] == 120.0,
          "Z1 上界 = 60% HRmax", str(bands[0]))
    check(bands[4]["max_bpm"] is None, "最后一区没有上界")

    check(hr_zones(None) is None, "没设最大心率时算不出区间")
    check(hr_zone_of(150, None) is None, "没设最大心率时不猜区间")

    # 储备心率（Karvonen）：同样 150bpm 会被算得更"高"
    by_max = hr_zone_of(150, 200, 50, HR_MODE_MAX)
    by_res = hr_zone_of(150, 200, 50, HR_MODE_RESERVE)
    check(by_max["code"] != by_res["code"],
          "两种算法给出不同的区间（Karvonen 更严）",
          "{} vs {}".format(by_max["label"], by_res["label"]))

    # 区间必须首尾相接，不能有缝（否则心率会掉进缝里）
    bands = hr_zones(190, 55, HR_MODE_RESERVE)
    for a, b in zip(bands, bands[1:]):
        check(abs(a["max_bpm"] - b["min_bpm"]) < 0.01,
              "{} 和 {} 首尾相接".format(a["code"], b["code"]),
              "{} / {}".format(a["max_bpm"], b["min_bpm"]))

    # 储备心率模式下低于静息心率也归 Z1，不会返回 None
    check(hr_zone_of(50, 190, 60, HR_MODE_RESERVE)["code"] == "Z1",
          "低于静息心率时归 Z1")


# ======================================================================
# 3. 训练会话：入库、区间分布、上限保护
# ======================================================================


async def test_session_heart_rate() -> None:
    print("\n[3] 心率进轨迹与报告")
    trainer = SimulatedTrainer(cadence=85.0)
    await trainer.connect()
    hr = SimulatedHeartRate(rest_hr=60, max_hr=190)
    await hr.connect()
    trainer.on_data = lambda d: hr.set_power(d.get("power_w") or 0)
    session = WorkoutSession(trainer, heart_rate=hr)
    try:
        await session.start(220.0, duration_min=10, erg_mode="auto",
                            config={"heart_rate": {"max_hr": 190, "rest_hr": 60,
                                                   "zone_mode": "reserve"}})
        await asyncio.sleep(8.0)
        snap = session.snapshot()
        check(snap["heart_rate"] is not None, "快照里有实时心率",
              str(snap["heart_rate"]))
        check(snap["hr_source"] == "strap", "标明来源是独立心率带",
              str(snap["hr_source"]))
        check((snap["hr_zone"] or {}).get("code") is not None, "算出当前心率区间",
              str((snap["hr_zone"] or {}).get("label")))
        check(snap["hr_stale"] is False, "心率数据是新鲜的")

        await session.stop()
        stats = (session.summary or {}).get("heart_rate")
        if check(stats is not None, "报告里有心率区块"):
            check(stats["avg_bpm"] is not None and stats["max_bpm"] is not None,
                  "给出平均/最大心率",
                  "{} / {} bpm".format(stats["avg_bpm"], stats["max_bpm"]))
            check(stats["max_bpm"] >= stats["avg_bpm"], "最大不小于平均")
            zones = stats["zones"]
            check(len(zones) == 5, "5 个心率区间都给了")
            check(abs(sum(z["pct"] for z in zones) - 100.0) < 1.0,
                  "区间占比合计 100%", "{:.1f}%".format(sum(z["pct"] for z in zones)))
            check(stats["rr_seen"] is True, "记录了这根带子会给 RR 间期")

        # 轨迹里要有心率列，而且和报告的平均值对得上
        hrs = [p["hr"] for p in session._trace if p.get("hr") is not None]
        check(len(hrs) >= 4, "轨迹里逐点记了心率", "{} 个点".format(len(hrs)))
        if hrs and stats:
            import statistics
            check(abs(statistics.mean(hrs) - stats["avg_bpm"]) < 5.0,
                  "报告平均值和轨迹一致",
                  "{:.1f} vs {:.1f}".format(statistics.mean(hrs), stats["avg_bpm"]))

        # 没接心率带的那次训练，报告里就不该有这个区块
        plain = SimulatedTrainer()
        await plain.connect()
        session2 = WorkoutSession(plain)          # 注意：不传 heart_rate
        try:
            await session2.start(100.0, duration_min=1, erg_mode="auto")
            await asyncio.sleep(0.5)
            await session2.stop()
            check((session2.summary or {}).get("heart_rate") is None,
                  "没接心率带时报告里不出现心率区块")
        finally:
            await session2.aclose()
            await plain.disconnect()
    finally:
        await session.aclose()
        await trainer.disconnect()
        await hr.disconnect()


async def test_hr_cap_protection() -> None:
    print("\n[4] 心率上限保护（刹车）")
    trainer = SimulatedTrainer(cadence=85.0)
    await trainer.connect()
    hr = SimulatedHeartRate(rest_hr=60, max_hr=190)
    await hr.connect()
    trainer.on_data = lambda d: hr.set_power(d.get("power_w") or 0)
    session = WorkoutSession(trainer, heart_rate=hr)
    try:
        await session.start(280.0, duration_min=10, erg_mode="auto",
                            config={"heart_rate": {"max_hr": 190, "rest_hr": 60,
                                                   "hr_limit_enabled": True,
                                                   "hr_limit_bpm": 115}})
        plan_power = 280.0
        await asyncio.sleep(30.0)
        lowered = session.target_power
        check(lowered < plan_power, "心率持续超上限后目标被下调",
              "{:.0f}W → {:.0f}W".format(plan_power, lowered))
        check(session._hr_cap_note != "", "记录了下调原因", session._hr_cap_note)
        # 下调幅度是每次 5%，且不低于本段原目标的 80%
        check(lowered >= plan_power * 0.80 - 0.5, "没有降到 80% 以下",
              "{:.0f}W（下限 {:.0f}W）".format(lowered, plan_power * 0.80))

        await session.stop()
        stats = (session.summary or {}).get("heart_rate") or {}
        check(stats.get("cap_active") is True, "报告里标明上限保护是开着的")
        check(stats.get("cap_note"), "报告里记录了下调这件事",
              str(stats.get("cap_note"))[:40])

        # 关掉保护：同样超上限也不该动目标
        session2 = WorkoutSession(trainer, heart_rate=hr)
        await session2.start(280.0, duration_min=10, erg_mode="auto",
                             config={"heart_rate": {"max_hr": 190,
                                                    "hr_limit_enabled": False}})
        await asyncio.sleep(12.0)
        check(session2.target_power == 280.0, "没开保护时不动目标功率",
              "{:.0f}W".format(session2.target_power))
        await session2.aclose()
    finally:
        await session.aclose()
        await trainer.disconnect()
        await hr.disconnect()


async def test_strap_staleness() -> None:
    print("\n[5] 心率带掉线：不能继续显示冻结的心率")
    trainer = SimulatedTrainer(cadence=85.0)
    await trainer.connect()
    hr = SimulatedHeartRate(rest_hr=60, max_hr=190)
    await hr.connect()
    session = WorkoutSession(trainer, heart_rate=hr)
    try:
        await session.start(100.0, duration_min=10, erg_mode="auto")
        await asyncio.sleep(2.0)
        before = session.snapshot()["heart_rate"]
        check(before is not None, "先有心率读数", str(before))
        hr.set_publishing(False)              # 带子还在连、但不再推数据
        await asyncio.sleep(12.0)
        snap = session.snapshot()
        check(snap["heart_rate"] is None,
              "超过 10 秒没数据后不再显示冻结值", str(snap["heart_rate"]))
        check(snap["hr_stale"] is True, "并且明确标出「没收到数据」")
    finally:
        await session.aclose()
        await trainer.disconnect()
        await hr.disconnect()


# ======================================================================
# 6. HTTP 接口
# ======================================================================


async def test_hr_endpoints() -> None:
    """心率带的三个接口：扫描 / 连接 / 断开。

    用假的 HeartRateClient 顶掉真实蓝牙——测试环境里当然没有心率带，
    但接口行为、地址记忆、和训练会话的衔接都必须测到。
    """
    print("\n[6] 心率带接口")
    import ibike.server as server_mod
    from aiohttp import ClientSession, web

    from ibike.ble import RawDevice

    class FakeHr:
        devices = [{"address": "AA:BB", "name": "Magene H603", "rssi": -55}]
        fail_connect = False
        # 服务端现在调用的是"从同一次扫描结果里筛"，所以这里提供分类函数
        @staticmethod
        def classify_straps(raw):
            class D:
                def __init__(self, d):
                    self._d = d

                def to_dict(self):
                    return self._d
            return [D(d) for d in FakeHr.devices]

        def __init__(self, on_data=None, on_event=None, auto_reconnect=True):
            self.on_data = on_data
            self.on_event = on_event
            self.name = ""
            self.address = ""
            self.connected = False
            self.latest = {"heart_rate_bpm": 128, "rr_intervals_ms": [470.0]}
            self.last_data_time = 0.0
            self.battery = 77
            self.rr_seen = True
            self.HR_STALE_AFTER_S = 10.0

        async def connect(self, address: str, name: str = "", timeout: float = 20.0):
            if FakeHr.fail_connect:
                from ibike.heartrate import HeartRateError
                raise HeartRateError("这根带子连不上（测试）")
            self.address = address
            # 名字由调用方从扫描结果里带过来（连接时不再重新扫一遍广播）
            self.name = name or "Magene H603"
            self.connected = True
            import time as _t
            self.last_data_time = _t.monotonic()

        async def disconnect(self):
            self.connected = False
            self.latest = {}
            self.last_data_time = 0.0

        def state(self):
            return {"kind": "heart_rate", "connected": self.connected,
                    "name": self.name, "address": self.address,
                    "battery": self.battery, "heart_rate": 128,
                    "rr_seen": self.rr_seen, "stale": False}

        def is_stale(self):
            return not self.connected

    # 服务端只扫一次广播（ibike.ble.scan_raw），骑行台和心率带都从这一份结果里筛。
    # 测试里当然不能真扫蓝牙，所以把这一步换成桩，并记下被调用了几次——
    # "只扫一次"本身就是这个改动要守住的性质。
    scan_calls = []

    async def fake_scan_raw(timeout: float = 8.0):
        scan_calls.append(timeout)
        return [RawDevice(address="AA:BB", name="Magene H603", rssi=-55,
                          service_uuids=["0000180d-0000-1000-8000-00805f9b34fb"]),
                RawDevice(address="CC:DD", name="我的骑行台", rssi=-60,
                          service_uuids=["00001826-0000-1000-8000-00805f9b34fb"])]

    original = server_mod.HeartRateClient
    original_scan = server_mod.scan_raw
    server_mod.HeartRateClient = FakeHr
    server_mod.scan_raw = fake_scan_raw
    FakeHr.fail_connect = False
    server = server_mod.Server(use_simulator=False)
    app = server.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = "http://127.0.0.1:{}".format(port)
    try:
        async with ClientSession() as http:
            # 没扫描就连接：要给一句人话，而不是 500
            async with http.post(base + "/api/hr/connect", json={}) as r:
                body = await r.json()
            check(r.status == 400 and not body.get("ok"),
                  "没扫描过、也没记住地址时给干净的 400", str(body.get("error")))

            async with http.post(base + "/api/scan", json={"timeout": 2}) as r:
                body = await r.json()
            check(body.get("ok") and len(body["hr_devices"]) == 1,
                  "一次扫描就返回心率带列表", str(body.get("hr_devices")))
            check(body["hr_devices"][0]["name"] == "Magene H603", "设备名带回来了")
            check(isinstance(body.get("devices"), list),
                  "同一次扫描也返回了骑行台列表", str(body.get("devices")))
            check(len(scan_calls) == 1,
                  "只发生了一次广播扫描（不是骑行台扫一遍、心率带再扫一遍）",
                  "扫了 {} 次".format(len(scan_calls)))

            # 连接（不给地址 → 自动选扫到的第一根）
            async with http.post(base + "/api/hr/connect", json={}) as r:
                body = await r.json()
            check(body.get("ok") and body["hr"]["connected"], "连接成功", str(body))
            check(server.settings.get("hr_strap_address") == "AA:BB",
                  "记住了这根带子的地址（下次自动重连要用）",
                  str(server.settings.get("hr_strap_address")))

            async with http.get(base + "/api/state") as r:
                state = await r.json()
            hr = state.get("hr_client") or {}
            check(hr.get("connected") is True, "状态里能看到心率带",
                  str(hr.get("name")))
            check((hr.get("saved") or {}).get("address") == "AA:BB",
                  "状态里带着「上次连的那根」（没连上时也能一键重连）")

            # 训练时心率要能进报告。
            # 注意顺序：连模拟骑行台会把心率源换成"模拟心率带"（模拟模式自带一根），
            # 所以要在那之后再连一次我们这根假的，才能测到真实心率带那条路。
            await http.post(base + "/api/connect", json={"simulator": True})
            async with http.post(base + "/api/hr/connect",
                                 json={"address": "AA:BB"}) as r:
                check((await r.json()).get("ok"), "连接骑行台后仍能单独连心率带")
            await http.post(base + "/api/start",
                            json={"mode": "constant", "target_power": 150,
                                  "duration_min": 5})
            await asyncio.sleep(2.5)
            async with http.post(base + "/api/stop", json={}) as r:
                summary = (await r.json()).get("summary") or {}
            stats = summary.get("heart_rate")
            check(stats is not None and stats.get("avg_bpm") == 128,
                  "训练报告里带上了心率", str(stats and stats.get("avg_bpm")))

            # 断开：对象要留着（界面得能显示"配过、但没连上"）
            async with http.post(base + "/api/hr/disconnect", json={}) as r:
                check((await r.json()).get("ok"), "断开返回 ok")
            async with http.get(base + "/api/state") as r:
                hr = (await r.json()).get("hr_client") or {}
            check(hr is not None and hr.get("connected") is False,
                  "断开后仍能看到这根带子，只是未连接", str(hr))

            # 连接失败要给可读的错误，而且不能留下半连接状态
            FakeHr.fail_connect = True
            async with http.post(base + "/api/hr/connect",
                                 json={"address": "CC:DD"}) as r:
                body = await r.json()
            check(r.status == 400 and "连不上" in str(body.get("error")),
                  "连接失败给出原因", str(body.get("error")))
            async with http.get(base + "/api/state") as r:
                hr = (await r.json()).get("hr_client") or {}
            check(hr.get("connected") is False,
                  "失败后没有留下「已连接」的假状态")
    finally:
        await runner.cleanup()
        server_mod.HeartRateClient = original
        server_mod.scan_raw = original_scan


# ======================================================================
# 7. 空闲时的实时数据（设备页要在开始训练之前就能看出连没连上）
# ======================================================================


async def test_idle_live_data() -> None:
    """还没点「开始训练」时，快照里也要有实时功率/踏频/心率。

    训练主循环只在训练进行时跑，所以取数不能只挂在它上面——否则设备页在
    "连接成功、还没开始"这个状态下什么都看不到，用户没法确认到底连上没有，
    而这恰恰是设备页存在的意义。服务端每 0.25 秒的广播循环会调
    `session.monitor_tick()` 来补上这一段。
    """
    print("\n[7] 空闲时（未开始训练）也有实时数据")
    import ibike.server as server_mod
    from aiohttp import ClientSession, web

    server = server_mod.Server(use_simulator=True)
    app = server.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        async with ClientSession() as http:
            await asyncio.sleep(2.0)            # 等广播循环跑几拍
            async with http.get("http://127.0.0.1:{}/api/state".format(port)) as r:
                snap = await r.json()
            check(snap.get("state") == "idle", "确认还没有开始训练", snap.get("state"))
            check(snap.get("power") is not None, "空闲时也有实时功率",
                  str(snap.get("power")))
            check(snap.get("cadence") is not None, "空闲时也有实时踏频",
                  str(snap.get("cadence")))
            check(snap.get("heart_rate") is not None, "空闲时也有实时心率",
                  str(snap.get("heart_rate")))
            check((snap.get("hr_zone") or {}).get("code") is not None,
                  "空闲时也按当前设置算出心率区间",
                  str((snap.get("hr_zone") or {}).get("label")))
            check(len(snap.get("hr_zones") or []) == 5, "空闲时也给出区间表")
            check((snap.get("trainer") or {}).get("connected") is True,
                  "骑行台显示已连接")
            check((snap.get("hr_client") or {}).get("connected") is True,
                  "心率带显示已连接")

            # 空闲时的取数不能污染训练统计
            session = server.session
            check(session.elapsed_s == 0.0 and session.active_s == 0.0,
                  "空闲读数没有推进计时",
                  "{}s / {}s".format(session.elapsed_s, session.active_s))
            check(session._power_integral == 0.0, "空闲读数没有累计做功",
                  str(session._power_integral))
            check(len(session._trace) == 0, "空闲读数没有写进训练轨迹",
                  "{} 个点".format(len(session._trace)))
    finally:
        await runner.cleanup()


# ======================================================================
# 8. 骑行台和心率带各自独立接断
# ======================================================================


async def test_trainer_and_strap_are_independent() -> None:
    """断开骑行台不能把心率带的读数一起带走。

    这两台是**两个独立外设**，可以各自接断。但心率的实时取数以前只发生在训练
    会话里，而会话是跟着骑行台创建/销毁的——于是"断开骑行台"会连会话一起清掉，
    心率带的数字跟着消失，而状态还写着"已连接"，自相矛盾。
    """
    print("\n[8] 断开骑行台不影响心率带")
    import ibike.server as server_mod
    from aiohttp import ClientSession, web

    server = server_mod.Server(use_simulator=True)
    app = server.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = "http://127.0.0.1:{}".format(port)
    try:
        async with ClientSession() as http:
            await asyncio.sleep(2.0)
            async with http.get(base + "/api/state") as r:
                before = await r.json()
            check(before.get("heart_rate") is not None, "断开前有心率读数",
                  str(before.get("heart_rate")))

            await http.post(base + "/api/disconnect", json={})
            await asyncio.sleep(1.5)
            async with http.get(base + "/api/state") as r:
                after = await r.json()
            hr = after.get("hr_client") or {}
            check(hr.get("connected") is True, "骑行台断开后心率带仍是已连接",
                  str(hr.get("name")))
            check(after.get("heart_rate") is not None,
                  "心率读数还在（以前会跟着会话一起消失）",
                  str(after.get("heart_rate")))
            check(after.get("hr_contact") is not None, "电极状态也还在",
                  str(after.get("hr_contact")))
            check((after.get("hr_zone") or {}).get("code") is not None,
                  "心率区间照常算", str((after.get("hr_zone") or {}).get("label")))
            check(after.get("power") is None, "骑行台那边确实已经断开（功率没了）")

            await http.post(base + "/api/hr/disconnect", json={})
            await asyncio.sleep(1.0)
            async with http.get(base + "/api/state") as r:
                final = await r.json()
            check(final.get("heart_rate") is None, "断开心率带之后读数才消失",
                  str(final.get("heart_rate")))
            check(final.get("hr_contact") is None, "电极状态也清掉")
    finally:
        await runner.cleanup()


def main() -> int:
    print("=" * 70)
    print("心率带测试")
    print("=" * 70)
    test_parse()
    test_zones()
    asyncio.run(test_session_heart_rate())
    asyncio.run(test_hr_cap_protection())
    asyncio.run(test_strap_staleness())
    asyncio.run(test_hr_endpoints())
    asyncio.run(test_idle_live_data())
    asyncio.run(test_trainer_and_strap_are_independent())

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
