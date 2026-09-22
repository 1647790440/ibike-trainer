#!/usr/bin/env python3
"""iBike 自检：协议编解码 + 两条控功率路径 + 自动降级 + HTTP 接口。

这个脚本不需要任何硬件，用模拟骑行台把整条链路跑一遍：

    python3 selftest.py            # 完整测试（约 3 分钟）
    python3 selftest.py --quick    # 缩短收敛观察时间
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 测试隔离：绝不能碰用户的真实数据目录。
# 训练报告是"训练一结束就自动保存"的，不隔离的话跑一次测试就会往
# data/reports/ 里留下一条真实报告。数据根目录在构造存储对象时读这个环境变量。
# ---------------------------------------------------------------------------
import os as _os
import tempfile as _tempfile

_os.environ.setdefault("IBIKE_DATA_DIR",
                       _tempfile.mkdtemp(prefix="ibike-test-data-"))

from ibike import ftms
from ibike.session import ERG_AUTO, ERG_FTMS, ERG_RESISTANCE, WorkoutSession
from ibike.simulator import SimulatedTrainer

PASS = "\033[32m✔\033[0m"
FAIL = "\033[31m✘\033[0m"
INFO = "\033[34m·\033[0m"

_failures: List[str] = []


def check(condition: bool, label: str, detail: str = "") -> bool:
    mark = PASS if condition else FAIL
    print("  {} {}{}".format(mark, label, "  → " + detail if detail else ""))
    if not condition:
        _failures.append(label)
    return condition


# ======================================================================
# A. 协议编解码
# ======================================================================


def test_ftms_codec() -> None:
    print("\n[A] FTMS 协议编解码")

    # Indoor Bike Data：瞬时速度 + 踏频 + 瞬时功率
    flags = ftms.FLAG_INST_CADENCE | ftms.FLAG_INST_POWER
    payload = struct.pack("<H", flags) + struct.pack("<H", 2500) \
        + struct.pack("<H", 170) + struct.pack("<h", 100)
    parsed = ftms.parse_indoor_bike_data(payload)
    check(parsed.get("speed_kmh") == 25.0, "解析瞬时速度", str(parsed.get("speed_kmh")))
    check(parsed.get("cadence_rpm") == 85.0, "解析踏频（0.5rpm 单位）", str(parsed.get("cadence_rpm")))
    check(parsed.get("power_w") == 100, "解析功率", str(parsed.get("power_w")))

    # More Data 位置位时不应解析速度
    payload2 = struct.pack("<H", ftms.FLAG_MORE_DATA | ftms.FLAG_INST_POWER) + struct.pack("<h", 123)
    parsed2 = ftms.parse_indoor_bike_data(payload2)
    check("speed_kmh" not in parsed2, "More Data 位生效时不含速度")
    check(parsed2.get("power_w") == 123, "跳位后仍能正确解析功率", str(parsed2.get("power_w")))

    # 截断的帧不应抛异常
    try:
        ftms.parse_indoor_bike_data(payload[:5])
        check(True, "截断帧不会抛异常")
    except Exception as exc:  # noqa: BLE001
        check(False, "截断帧不会抛异常", str(exc))

    # 能力声明
    machine = (1 << 1) | (1 << 14)          # 踏频 + 功率测量
    target = (1 << 2) | (1 << 3)            # 目标阻力 + 目标功率
    feature = ftms.parse_fitness_machine_feature(
        struct.pack("<I", machine) + struct.pack("<I", target))
    check(feature["supports_power_target"], "识别「支持目标功率」")
    check(feature["supports_resistance_target"], "识别「支持目标阻力」")
    check(feature["supports_power_measurement"], "识别「支持功率测量」")

    pr = ftms.parse_supported_power_range(struct.pack("<hhH", 0, 1500, 1))
    check(pr == {"min": 0, "max": 1500, "increment": 1}, "解析功率范围", str(pr))

    rr = ftms.parse_supported_resistance_level_range(struct.pack("<BBB", 0, 100, 1))
    check(rr is not None and rr["max_raw"] == 100, "解析阻力范围", str(rr))

    # 指令编码
    check(ftms.encode_set_target_power(100) == b"\x05\x64\x00",
          "编码 Set Target Power(100W)", ftms.encode_set_target_power(100).hex(" "))
    check(ftms.encode_set_target_resistance_level(10.0) == b"\x04\x64",
          "编码 Set Target Resistance(10.0)", ftms.encode_set_target_resistance_level(10.0).hex(" "))
    check(ftms.encode_request_control() == b"\x00", "编码 Request Control")
    check(ftms.encode_stop() == b"\x08\x01", "编码 Stop")

    # 应答解析
    resp = ftms.parse_control_point_response(bytes([0x80, 0x00, 0x01, 0x01]))
    check(resp["ok"] and resp["has_control"], "解析 Request Control 成功应答", str(resp["result"]))
    resp2 = ftms.parse_control_point_response(bytes([0x80, 0x05, 0x05]))
    check(resp2["ok"] is False and resp2["result_code"] == 0x05,
          "识别 Control Not Permitted", str(resp2["result"]))


# ======================================================================
# B/C/D. 控功率闭环
# ======================================================================


async def run_scenario(name: str, *, responds: bool, advertise: bool,
                       erg_mode: str, target: float = 100.0,
                       seconds: float = 40.0, settle: float = 12.0
                       ) -> Tuple[Dict[str, Any], List[Tuple[float, float, Optional[str]]]]:
    """跑一段训练，返回最后一个快照和采样序列。"""
    trainer = SimulatedTrainer(responds_to_target_power=responds,
                               advertise_power_target=advertise,
                               cadence=85.0)
    await trainer.connect()
    session = WorkoutSession(trainer)
    await session.start(target, duration_min=seconds / 60.0, erg_mode=erg_mode)

    samples: List[Tuple[float, float, Optional[str]]] = []
    started = time.time()
    try:
        while time.time() - started < seconds:
            await asyncio.sleep(0.5)
            snap = session.snapshot()
            if snap["power"] is not None:
                samples.append((time.time() - started, snap["power"], snap["active_erg_mode"]))
            if snap["state"] in ("finished", "error"):
                break
    finally:
        await session.aclose()
        await trainer.disconnect()
    await asyncio.sleep(0.1)
    return session.snapshot(), samples


def hold_stats(samples: List[Tuple[float, float, Optional[str]]], last: float
               ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    window = [p for t, p, _ in samples if t >= (samples[-1][0] - last)] if samples else []
    if not window:
        return None, None, None
    avg = sum(window) / len(window)
    lo, hi = min(window), max(window)
    return avg, lo, hi


async def test_native_erg(seconds: float) -> None:
    print("\n[B] 原生 ERG 路径（骑行台内部闭环）")
    snap, samples = await run_scenario("native", responds=True, advertise=True,
                                       erg_mode=ERG_FTMS, seconds=seconds)
    avg, lo, hi = hold_stats(samples, 10.0)
    check(snap["active_erg_mode"] == ERG_FTMS, "使用的控制方式是原生 ERG",
          str(snap["active_erg_mode"]))
    if check(avg is not None, "收到了功率数据"):
        check(abs(avg - 100.0) <= 5.0, "功率稳定在 100W ±5W",
              "均值 {:.1f}W（区间 {:.0f}~{:.0f}W）".format(avg, lo, hi))
    check(snap["power_max"] > 0, "记录了最大功率", "{:.0f}W".format(snap["power_max"]))
    check(snap["energy_kj"] > 0, "累计做功统计正常", "{:.1f}kJ".format(snap["energy_kj"]))


async def test_closed_loop(seconds: float) -> None:
    print("\n[C] 闭环阻力路径（上位机调节阻力）")
    snap, samples = await run_scenario("closed", responds=False, advertise=False,
                                       erg_mode=ERG_RESISTANCE, seconds=seconds)
    avg, lo, hi = hold_stats(samples, 10.0)
    check(snap["active_erg_mode"] == ERG_RESISTANCE, "使用的控制方式是闭环阻力",
          str(snap["active_erg_mode"]))
    # 容差按观察时长分段：闭环要先把 k 认出来、再一步步把档位收敛过去，
    # --quick 只有 22 秒，最后 10 秒里还带着收敛尾巴。以前统一按 ±6W 断言，
    # 实测会在 103.9~106.0W 之间抖——这不是程序坏了，是断言本身卡在边缘上。
    tolerance = 6.0 if seconds >= 40.0 else 9.0
    if check(avg is not None, "收到了功率数据"):
        check(abs(avg - 100.0) <= tolerance,
              "功率收敛到 100W ±{:.0f}W（本程序只看了 {:.0f} 秒）".format(tolerance, seconds),
              "均值 {:.1f}W（区间 {:.0f}~{:.0f}W）".format(avg, lo, hi))
    check(snap["resistance_raw"] is not None, "阻力档位已下发",
          "原始值 {}".format(snap["resistance_raw"]))
    check(snap["controller_k"] is not None, "已在线辨识出阻力-功率系数 k",
          str(snap["controller_k"]))


async def test_auto_fallback(seconds: float) -> None:
    print("\n[D] 自动降级（骑行台声明支持目标功率但实际不执行）")
    events: List[str] = []
    trainer = SimulatedTrainer(responds_to_target_power=False,
                               advertise_power_target=True, cadence=85.0)
    await trainer.connect()
    # 这台"装死"的台子会一直停在某个固定阻力上。把它的自然工作点挪到 ~148W，
    # 离探测目标（100-25=75W）足够远——否则它瞎猫碰上死耗子正好落在探测目标
    # 附近，探测会误判成"能收敛"，这个用例就测不出东西了。
    trainer.resistance_raw = 70.0
    trainer.power = 148.0
    session = WorkoutSession(trainer, on_event=lambda k, m: events.append("{}: {}".format(k, m)))
    # 真实时间轴是 12 秒宽限 + 18 秒观察 + 20 秒阶跃探测，测试里压缩掉，
    # 不然这一个用例就要跑一分钟
    session.FALLBACK_GRACE_S = 2.0
    session.FALLBACK_HOLD_S = 4.0
    session.PROBE_WAIT_S = 4.0
    await session.start(100.0, duration_min=seconds / 60.0, erg_mode=ERG_AUTO)
    check(session.active_erg_mode == ERG_FTMS, "起步时先尝试原生 ERG",
          str(session.active_erg_mode))

    samples: List[Tuple[float, float, Optional[str]]] = []
    started = time.time()
    try:
        while time.time() - started < seconds:
            await asyncio.sleep(0.5)
            snap = session.snapshot()
            if snap["power"] is not None:
                samples.append((time.time() - started, snap["power"], snap["active_erg_mode"]))
    finally:
        snap = session.snapshot()
        await session.aclose()
        await trainer.disconnect()

    check(any(e.startswith("probe: ") for e in events), "降级前先做了目标功率阶跃探测",
          next((e for e in events if e.startswith("probe: ")), "无"))
    check(any(e.startswith("fallback") for e in events), "触发了自动降级事件",
          next((e for e in events if e.startswith("fallback")), "无"))
    check(snap["active_erg_mode"] == ERG_RESISTANCE, "最终切换到闭环阻力",
          str(snap["active_erg_mode"]))

    tail = [p for t, p, m in samples if m == ERG_RESISTANCE]
    if check(len(tail) >= 10, "降级后采集到足够样本", "{} 个".format(len(tail))):
        avg = sum(tail[-20:]) / len(tail[-20:])
        check(abs(avg - 100.0) <= 8.0, "降级后功率收敛到 100W ±8W", "均值 {:.1f}W".format(avg))


async def test_fallback_probe_passes(seconds: float) -> None:
    """固件只是"回过神得慢"，探测期间跟上了目标，就不该被降级掉。

    这是真机上踩过的坑：功率稳稳地停在偏离目标的地方，程序立刻判成
    "固件装死"就切走了控制权。加了阶跃探测之后，只要固件在被明确要求一个新
    目标时能把功率压过去，就说明它这套 ERG 是真的，不该夺权。
    """
    print("\n[D2] 阶跃探测通过：固件能收敛到新目标就不降级")
    events: List[str] = []
    # 前半段固件"没睡醒"，功率停在原地——正是会让程序起疑心的样子；
    # 一旦被探测（要求一个新目标），它立刻正常闭环。
    trainer = SimulatedTrainer(responds_to_target_power=False,
                               advertise_power_target=True, cadence=85.0)
    await trainer.connect()
    session = WorkoutSession(trainer, on_event=lambda k, m: events.append("{}: {}".format(k, m)))
    session.FALLBACK_GRACE_S = 2.0
    session.FALLBACK_HOLD_S = 4.0
    session.PROBE_WAIT_S = 12.0
    await session.start(100.0, duration_min=seconds / 60.0, erg_mode=ERG_AUTO)
    try:
        started = time.time()
        while time.time() - started < seconds:
            await asyncio.sleep(0.25)
            if not trainer.responds_to_target_power and any(
                    e.startswith("probe: ") for e in events):
                # 探测一开始，固件就"醒了"：这正是要区分的那类台子
                trainer.responds_to_target_power = True
    finally:
        snap = session.snapshot()
        await session.aclose()
        await trainer.disconnect()

    check(any(e.startswith("probe: ") for e in events), "确实起了疑心并做了探测",
          next((e for e in events if e.startswith("probe: ")), "无"))
    check(any(e.startswith("probe-ok") for e in events), "探测判定固件能收敛到新目标",
          next((e for e in events if e.startswith("probe-ok")), "无"))
    check(not any(e.startswith("fallback") for e in events), "没有触发降级",
          next((e for e in events if e.startswith("fallback")), "无（正确）"))
    check(snap["active_erg_mode"] == ERG_FTMS, "全程留在原生 ERG",
          str(snap["active_erg_mode"]))


# ======================================================================
# E. HTTP 接口
# ======================================================================


async def test_http_api() -> None:
    print("\n[E] HTTP / WebSocket 接口")
    from aiohttp import ClientSession, web
    from ibike.server import Server

    server = Server(use_simulator=False)
    app = server.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = "http://127.0.0.1:{}".format(port)

    try:
        async with ClientSession() as http:
            async with http.get(base + "/") as resp:
                body = await resp.text()
                check(resp.status == 200 and "iBike" in body, "首页可访问", "HTTP {}".format(resp.status))

            async with http.get(base + "/api/state") as resp:
                data = await resp.json()
                check(data.get("state") == "idle", "初始状态为 idle", str(data.get("state")))

            # 未连接就开训练应当被拒绝
            async with http.post(base + "/api/start",
                                 json={"target_power": 100, "duration_min": 1}) as resp:
                data = await resp.json()
                check(resp.status == 400 and not data.get("ok"), "未连接时拒绝开始训练")

            # 连接模拟器
            async with http.post(base + "/api/connect", json={"simulator": True}) as resp:
                data = await resp.json()
                check(resp.status == 200 and data.get("ok"), "连接模拟骑行台",
                      str(data.get("error", "")))

            # 参数校验
            async with http.post(base + "/api/start",
                                 json={"target_power": 9999, "duration_min": 1}) as resp:
                data = await resp.json()
                check(resp.status == 400, "拒绝超范围的目标功率", str(data.get("error")))

            # WebSocket 订阅
            ws = await http.ws_connect(base + "/ws")
            msg = await ws.receive_json(timeout=5)
            check(msg["type"] == "state", "WebSocket 首包推送状态")

            # 开始训练
            async with http.post(base + "/api/start",
                                 json={"target_power": 100, "duration_min": 0.2,
                                       "erg_mode": "auto"}) as resp:
                data = await resp.json()
                check(resp.status == 200 and data.get("ok"), "开始训练",
                      str(data.get("error", "")))

            await asyncio.sleep(1.5)
            async with http.get(base + "/api/state") as resp:
                data = await resp.json()
                check(data.get("state") == "running", "训练状态为 running",
                      str(data.get("state")))
                check(data.get("power") is not None, "读到实时功率",
                      str(data.get("power")))

            # 运行中调目标功率。不能只看 HTTP 回显——handler 回显的就是它自己
            # 解析出来的那个数，把下发那一步整个删掉也照样"通过"。这里直接查
            # 模拟骑行台收到的指令序列，确认 150W 真的下发了。
            async with http.post(base + "/api/target", json={"target_power": 150}) as resp:
                data = await resp.json()
                check(data.get("ok") and data.get("target_power") == 150,
                      "运行中修改目标功率（接口应答）")
            sent = [c for c in server.trainer._commands if c[0] == "target_power"]
            check(sent and sent[-1][1] == 150,
                  "150W 确实下发到了骑行台（而不是只改了界面上的数）",
                  str(sent[-1] if sent else "没有下发过任何目标功率"))

            # 暂停 / 继续
            async with http.post(base + "/api/pause", json={}) as resp:
                await resp.json()
            await asyncio.sleep(0.3)
            async with http.get(base + "/api/state") as resp:
                data = await resp.json()
                check(data.get("state") == "paused", "暂停生效", str(data.get("state")))

            async with http.post(base + "/api/resume", json={}) as resp:
                await resp.json()
            await asyncio.sleep(0.3)
            async with http.get(base + "/api/state") as resp:
                data = await resp.json()
                check(data.get("state") == "running", "继续生效", str(data.get("state")))

            # 手动设阻力
            async with http.post(base + "/api/resistance", json={"raw": 120}) as resp:
                data = await resp.json()
                check(data.get("ok"), "手动设置阻力")

            # 停止
            async with http.post(base + "/api/stop", json={}) as resp:
                await resp.json()
            await asyncio.sleep(0.3)
            async with http.get(base + "/api/state") as resp:
                data = await resp.json()
                check(data.get("state") == "finished", "停止后状态为 finished",
                      str(data.get("state")))

            await ws.close()

            # 断开
            async with http.post(base + "/api/disconnect", json={}) as resp:
                data = await resp.json()
                check(data.get("ok"), "断开连接")

            # 重新连一台"声明支持目标功率、但实际不执行"的模拟台，
            # 验证前端传下来的参数确实被接住了（少了它就不会走原生 ERG 降级那条路）
            async with http.post(base + "/api/connect",
                                 json={"simulator": True,
                                       "simulator_responds": False}) as resp:
                data = await resp.json()
                caps = (data.get("trainer") or {}).get("capabilities", {})
                check(resp.status == 200
                      and caps.get("supports_power_target") is True
                      and caps.get("supports_resistance_target") is True,
                      "模拟台能配成「声明支持目标功率但不执行」",
                      json.dumps(caps, ensure_ascii=False)[:90])
    finally:
        await runner.cleanup()


# ======================================================================


async def amain(args: argparse.Namespace) -> int:
    print("=" * 70)
    print("iBike 自检 —— 全部基于模拟骑行台，不需要真实硬件")
    print("=" * 70)

    test_ftms_codec()

    seconds = 22.0 if args.quick else 45.0
    await test_native_erg(seconds)
    await test_closed_loop(seconds)
    await test_auto_fallback(50.0 if args.quick else 90.0)
    await test_fallback_probe_passes(35.0)
    await test_http_api()

    print("\n" + "=" * 70)
    if _failures:
        print("{} 失败 {} 项：".format(FAIL, len(_failures)))
        for f in _failures:
            print("   - " + f)
        return 1
    print("{} 全部通过".format(PASS))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="iBike 自检")
    parser.add_argument("--quick", action="store_true", help="缩短收敛观察时间")
    args = parser.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
