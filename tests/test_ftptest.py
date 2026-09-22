#!/usr/bin/env python3
"""FTP 测试功能的测试。

    python3 tests/test_ftptest.py
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
# 测试隔离：绝不能碰用户的真实数据目录（训练报告是训练一结束就自动保存的）
# ---------------------------------------------------------------------------
_os.environ.setdefault("IBIKE_DATA_DIR",
                       tempfile.mkdtemp(prefix="ibike-test-data-"))

from ibike.ftptest import (KIND_TEST, TESTS, build_test_plan,  # noqa: E402
                           clamp_params, get_test, plan_stats)
from ibike.session import (ERG_FREE, ERG_FTMS, STATE_FINISHED,  # noqa: E402
                           WorkoutSession)
from ibike.simulator import SimulatedTrainer  # noqa: E402

_failures: List[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print("  {} {}{}".format("\033[32m✔\033[0m" if cond else "\033[31m✘\033[0m",
                             label, "  → " + detail if detail else ""))
    if not cond:
        _failures.append(label)
    return cond


# ======================================================================
# 方案构建
# ======================================================================


def test_protocols_build() -> None:
    print("\n[1] 三个测试方案都能生成合法课表")
    check(len(TESTS) == 3, "内置 3 个测试方案", "{} 个".format(len(TESTS)))
    for t in TESTS:
        steps = build_test_plan(t["id"], None, 200.0)
        st = plan_stats(steps)
        ok = bool(steps) and all(s["duration_s"] > 0 and s["target_power"] > 0
                                 for s in steps)
        check(ok, "{:<12} 步骤合法".format(t["name"]),
              "{} 段 / 测量段 {} 段".format(st["step_count"], st["measured_count"]))
        measured = [s for s in steps if s.get("kind") == KIND_TEST]
        check(len(measured) >= 1, "{:<12} 有被测量的段落".format(t["name"]),
              "{} 段".format(len(measured)))
        check(steps[0]["kind"] == "warmup", "{:<12} 以热身开始".format(t["name"]),
              steps[0]["name"])
        check(steps[-1]["kind"] == "cooldown", "{:<12} 以冷身结束".format(t["name"]),
              steps[-1]["name"])


def test_ramp_shape() -> None:
    print("\n[2] 坡道测试：逐级递增")
    # max_steps 的下限是 10（少于 10 级的坡道没有意义），所以这里期望 10 级
    steps = build_test_plan("ramp", {"warmup_min": 0, "cooldown_min": 0,
                                     "start_w": 100, "step_w": 20,
                                     "max_steps": 10, "step_s": 60}, 200.0)
    powers = [s["target_power"] for s in steps]
    check(powers == [100 + 20 * i for i in range(10)], "功率逐级 +20W", str(powers))
    check(all(s["kind"] == KIND_TEST for s in steps), "全部标记为测量段")
    check(all(s["duration_s"] == 60 for s in steps), "每级 60 秒")

    t = get_test("ramp")
    check(t["erg_mode"] == "ftms",
          "坡道测试用 ERG（它必须由骑行台逐级加载）", t["erg_mode"])
    check(t["result"]["multiplier"] == 0.75, "FTP 系数是 75%",
          str(t["result"]["multiplier"]))


def test_twenty_structure() -> None:
    print("\n[3] 20 分钟测试：必须含 5 分钟全力，且不用 ERG")
    t = get_test("twenty")
    check(t["erg_mode"] == "free", "用自由骑行而不是 ERG", t["erg_mode"])
    check(t["result"]["multiplier"] == 0.95, "FTP 系数是 95%",
          str(t["result"]["multiplier"]))

    steps = build_test_plan("twenty", None, 200.0)
    names = [s["name"] for s in steps]
    blowout = [s for s in steps if "全力" in s["name"]]
    check(len(blowout) == 1, "含一次 5 分钟全力（清空无氧用）",
          str([s["name"] for s in blowout]))
    measured = [s for s in steps if s.get("kind") == KIND_TEST]
    check(len(measured) == 1 and measured[0]["duration_s"] == 1200,
          "只有计时段被测量，且是 20 分钟",
          "{} 段 {}".format(len(measured), measured[0]["duration_s"] if measured else 0))
    # 全力段必须在计时段之前
    idx_blowout = steps.index(blowout[0])
    idx_test = steps.index(measured[0])
    check(idx_blowout < idx_test, "5 分钟全力排在计时段之前",
          "第 {} 段 → 第 {} 段".format(idx_blowout + 1, idx_test + 1))
    check(any("快频" in n for n in names), "含快频唤醒")


def test_eight_structure() -> None:
    print("\n[4] 8 分钟测试：两次全力、系数 90%")
    t = get_test("eight")
    check(t["erg_mode"] == "free", "用自由骑行", t["erg_mode"])
    check(t["result"]["multiplier"] == 0.90, "FTP 系数是 90%",
          str(t["result"]["multiplier"]))
    steps = build_test_plan("eight", None, 200.0)
    measured = [s for s in steps if s.get("kind") == KIND_TEST]
    check(len(measured) == 2, "两次被测量的段落", "{} 次".format(len(measured)))
    check(all(s["duration_s"] == 480 for s in measured), "每次 8 分钟")


def test_param_clamping() -> None:
    print("\n[5] 参数越界会被夹回合法范围")
    merged = clamp_params("ramp", {"start_w": 9999, "step_w": -50, "max_steps": 1000})
    check(merged["start_w"] == 250, "离谱的起始功率被夹住", str(merged["start_w"]))
    check(merged["step_w"] == 5, "离谱的递增值被夹住", str(merged["step_w"]))
    check(merged["max_steps"] == 40, "离谱的级数被夹住", str(merged["max_steps"]))
    try:
        build_test_plan("nope", None, 200.0)
        check(False, "未知方案应当报错")
    except ValueError as exc:
        check("未知" in str(exc), "未知方案报错清晰", str(exc))


# ======================================================================
# 会话行为
# ======================================================================


async def test_ramp_runs_and_computes_ftp() -> None:
    print("\n[6] 坡道测试：跑完并算出 FTP")
    trainer = SimulatedTrainer(responds_to_target_power=True, cadence=85.0)
    await trainer.connect()
    session = WorkoutSession(trainer)

    # 短坡道：热身/冷身 0，裁到 3 级 × 30 秒（协议本身的下限是 10 级，
    # 这里只是为了测试跑得快一点而截断）
    plan = build_test_plan("ramp", {"warmup_min": 0, "cooldown_min": 0,
                                    "start_w": 100, "step_w": 20,
                                    "max_steps": 10, "step_s": 30}, 200.0)[:3]
    await session.start(plan=plan, plan_name="坡道测试", erg_mode=ERG_FTMS,
                        config={"mode": "test", "erg_mode": "ftms", "ftp": 200.0,
                                "test": {"test_id": "ramp", "result_kind": "ramp",
                                         "multiplier": 0.75,
                                         "source_label": "最好 1 分钟功率",
                                         "self_paced": False}})

    check(session.snapshot()["is_test"] is True, "快照标记为测试")
    check(session.snapshot()["test_level"] == 1, "从第 1 级开始")

    for _ in range(260):        # 3 级 × 30 秒 ≈ 90 秒
        await asyncio.sleep(0.5)
        if session.state == STATE_FINISHED:
            break
    snap = session.snapshot()
    result = (snap.get("summary") or {}).get("test_result")
    check(session.state == STATE_FINISHED, "测试已结束", session.state)
    if check(result is not None, "总结里带测试结果"):
        print("      推算 FTP = {}W（{} {}W × {}）".format(
            result["ftp"], result["source_label"],
            result["source_power"], result["multiplier"]))
        check(abs(result["ftp"] - result["source_power"] * 0.75) <= 1,
              "FTP 等于最好 1 分钟功率的 75%")
        # 这条坡道只有 3 级，全踩完了也没力竭 —— 必须判为无效，
        # 否则会给出一个"看着正经、其实毫无意义"的数字
        check(result["valid"] is False, "坡道太短（跑完所有级也没力竭）判为无效")
        check("坡道太短" in result["detail"], "给出了可操作的提示",
              result["detail"])
    check((snap.get("summary") or {}).get("completed") is False,
          "结果无效时不算「测试完成」")
    await session.aclose()
    await trainer.disconnect()


async def test_ramp_stops_on_failure() -> None:
    print("\n[7] 坡道测试：踩不动了会自动结束")
    # 踏频故意压到 40：功率撑不住目标，应当判力竭
    trainer = SimulatedTrainer(responds_to_target_power=True, cadence=40.0)
    await trainer.connect()
    session = WorkoutSession(trainer)
    plan = build_test_plan("ramp", {"warmup_min": 0, "cooldown_min": 0,
                                    "start_w": 100, "step_w": 20,
                                    "max_steps": 12, "step_s": 60}, 200.0)
    events: List[str] = []
    session.on_event = lambda k, m: events.append("{}: {}".format(k, m))
    await session.start(plan=plan, plan_name="坡道测试", erg_mode=ERG_FTMS,
                        config={"mode": "test", "erg_mode": "ftms", "ftp": 200.0,
                                "test": {"test_id": "ramp", "result_kind": "ramp",
                                         "multiplier": 0.75, "self_paced": False}})

    for _ in range(120):
        await asyncio.sleep(0.5)
        if session.state == STATE_FINISHED:
            break
    snap = session.snapshot()
    summary = snap.get("summary") or {}
    check(session.state == STATE_FINISHED, "测试已结束", session.state)
    check("力竭" in str(summary.get("reason")), "结束原因是力竭",
          str(summary.get("reason")))
    check(any(e.startswith("test:") for e in events), "发了测试结束的通知",
          next((e for e in events if e.startswith("test:")), "无"))
    check(snap["test_level"] < 12, "没有跑完 12 级（提前终止）",
          "停在第 {} 级".format(snap.get("test_level")))
    await session.aclose()
    await trainer.disconnect()


async def test_free_mode_sends_no_power() -> None:
    print("\n[8] 自由骑行：不下发任何目标功率")
    trainer = SimulatedTrainer(responds_to_target_power=True, cadence=85.0)
    await trainer.connect()
    session = WorkoutSession(trainer)
    plan = build_test_plan("twenty", {"warmup_min": 0, "cooldown_min": 0,
                                      "opener_reps": 0, "blowout_min": 3,
                                      "test_min": 8}, 200.0)
    await session.start(plan=plan, plan_name="20 分钟测试", erg_mode=ERG_FREE,
                        config={"mode": "test", "erg_mode": "free", "ftp": 200.0,
                                "test": {"test_id": "twenty",
                                         "result_kind": "best_segment",
                                         "multiplier": 0.95, "self_paced": True}})

    check(session.active_erg_mode == ERG_FREE, "控制方式是自由骑行")
    check(session.snapshot()["erg_mode_label"] == "自由骑行", "界面标签正确")
    await asyncio.sleep(4.0)

    ops = [c for c in trainer._commands if c[0] == "target_power"]
    check(ops == [], "全程没有下发过目标功率（这正是测试有效的前提）",
          "下发了 {} 次".format(len(ops)))
    res_cmds = [c for c in trainer._commands if c[0] == "resistance"]
    check(len(res_cmds) == 1, "只在开始时设了一次阻力",
          "设了 {} 次".format(len(res_cmds)))
    check(session.snapshot()["free_resistance"] is not None, "快照里带阻力档位",
          str(session.snapshot().get("free_resistance")))

    # 自由骑行中微调阻力
    await session.set_free_resistance(120)
    check(session.free_resistance == 120, "可以微调阻力档位")
    check(any(c == ("resistance", 120) for c in trainer._commands),
          "微调后的档位已下发")

    await session.aclose()
    await trainer.disconnect()


async def test_segment_ftp_from_best_segment() -> None:
    print("\n[9] 计时段测试：从较好的一段算 FTP")
    trainer = SimulatedTrainer(responds_to_target_power=True, cadence=85.0)
    await trainer.connect()
    session = WorkoutSession(trainer)

    # 手工造两段测量段，直接注入轨迹来验证计算逻辑
    plan = [
        {"name": "第 1 次", "kind": KIND_TEST, "duration_s": 60, "target_power": 200},
        {"name": "恢复", "kind": "recovery", "duration_s": 30, "target_power": 100},
        {"name": "第 2 次", "kind": KIND_TEST, "duration_s": 60, "target_power": 220},
    ]
    await session.start(plan=plan, plan_name="8 分钟测试", erg_mode=ERG_FREE,
                        config={"mode": "test", "erg_mode": "free", "ftp": 200.0,
                                "test": {"test_id": "eight",
                                         "result_kind": "best_segment",
                                         "multiplier": 0.90,
                                         "source_label": "较好一次的平均功率",
                                         "self_paced": True}})
    await session.aclose()

    # 直接喂一段轨迹：第 1 段 200W、第 2 段 240W
    trace = []
    for i in range(60):
        trace.append({"t": float(i), "dt": 1.0, "p": 200.0, "target": 200.0, "step": 0})
    for i in range(30):
        trace.append({"t": 60.0 + i, "dt": 1.0, "p": 100.0, "target": 100.0, "step": 1})
    for i in range(60):
        trace.append({"t": 90.0 + i, "dt": 1.0, "p": 240.0, "target": 220.0, "step": 2})
    session._trace = trace
    session.active_s = 150.0
    session.duration_s = 150.0

    result = session._compute_test_result(trace)
    if check(result is not None, "算出了测试结果"):
        check(result["source_power"] == 240.0, "取的是较好的那一段（240W）",
              "{}W".format(result["source_power"]))
        check(result["ftp"] == 216, "FTP = 240 × 90% = 216",
              "{}W".format(result["ftp"]))
        check(result["valid"] is True, "结果有效")
    await trainer.disconnect()


async def test_ramp_valid_when_failed_early() -> None:
    print("\n[11] 坡道测试：力竭（没跑完全部级数）→ 结果有效")
    trainer = SimulatedTrainer(cadence=85.0)
    await trainer.connect()
    session = WorkoutSession(trainer)
    plan = build_test_plan("ramp", {"warmup_min": 0, "cooldown_min": 0,
                                    "start_w": 100, "step_w": 20,
                                    "max_steps": 12, "step_s": 60}, 200.0)
    await session.start(plan=plan, plan_name="坡道测试", erg_mode=ERG_FTMS,
                        config={"mode": "test", "erg_mode": "ftms", "ftp": 200.0,
                                "test": {"test_id": "ramp", "result_kind": "ramp",
                                         "multiplier": 0.75, "self_paced": False}})
    await session.aclose()

    # 构造一段轨迹：踩到第 6 级力竭（共 12 级），最后 1 分钟平均 320W
    trace = []
    t = 0.0
    for level in range(6):
        watts = 100.0 + 20.0 * level
        for _ in range(60):
            trace.append({"t": t, "dt": 1.0, "p": watts, "target": watts, "step": level})
            t += 1.0
    # 最后一级是最高功率，最好 1 分钟应当取到最后这 60 秒
    session._trace = trace
    session.active_s = t
    session.duration_s = sum(float(s["duration_s"]) for s in plan)

    # 同样的功率形状，但采样点每 2 秒一个（主循环被骑行台应答堵住时就是这样）。
    # "最好 1 分钟"必须按**时间**取窗口；按点数取 60 个点会横跨 2 分钟，
    # 把最后那一级的权重摊薄，FTP 直接算低一截。
    slow = []
    t2 = 0.0
    for level in range(6):
        watts = 100.0 + 20.0 * level
        for _ in range(30):                     # 每级 30 点 × 2 秒 = 60 秒
            slow.append({"t": t2, "dt": 2.0, "p": watts, "target": watts, "step": level})
            t2 += 2.0
    got = WorkoutSession._best_window_power(slow, 60.0)
    check(got is not None and abs(got - 200.0) <= 0.01,
          "采样间隔 2 秒时，「最好 1 分钟」仍取到最后一级 200W",
          "{:.1f}W".format(got if got is not None else -1))

    result = session._compute_test_result(trace)
    if check(result is not None, "算出了测试结果"):
        check(result["valid"] is True, "结果有效（踩到第 6 级力竭，还有余级）",
              result["detail"])
        check(abs(result["source_power"] - 200.0) <= 0.01,
              "最好 1 分钟功率取自最后一级", "{}W".format(result["source_power"]))
        check(result["ftp"] == 150, "FTP = 200 × 75% = 150",
              "{}W".format(result["ftp"]))
    await trainer.disconnect()


async def test_test_marked_as_completed() -> None:
    print("\n[10] 力竭结束的坡道测试不该被标成「提前结束」")
    trainer = SimulatedTrainer(cadence=85.0)
    await trainer.connect()
    session = WorkoutSession(trainer)
    plan = build_test_plan("ramp", {"warmup_min": 0, "cooldown_min": 0,
                                    "start_w": 100, "step_w": 20,
                                    "max_steps": 12, "step_s": 60}, 200.0)
    await session.start(plan=plan, plan_name="坡道测试", erg_mode=ERG_FTMS,
                        config={"mode": "test", "erg_mode": "ftms", "ftp": 200.0,
                                "test": {"test_id": "ramp", "result_kind": "ramp",
                                         "multiplier": 0.75, "self_paced": False}})
    await session.aclose()

    # 构造"踩到第 4 级力竭"的轨迹，然后走真实的 stop() 流程
    trace = []
    t = 0.0
    for level in range(4):
        watts = 100.0 + 20.0 * level
        for _ in range(60):
            trace.append({"t": t, "dt": 1.0, "p": watts, "target": watts, "step": level})
            t += 1.0
    session._trace = trace
    session.active_s = t
    session.elapsed_s = t
    session.duration_s = sum(float(x["duration_s"]) for x in plan)

    await session.stop(reason="力竭，测试结束")
    summary = session.summary or {}
    result = summary.get("test_result") or {}
    check(summary.get("completed") is True,
          "踩到力竭（没跑满整张课表）也算「完成」——因为力竭就是测试的正常终点",
          "completed={} detail={}".format(summary.get("completed"), result.get("detail")))
    check(result.get("valid") is True, "结果有效")
    check(result.get("ftp") == 120, "FTP = 160 × 75% = 120（最后一级 160W）",
          "{}W".format(result.get("ftp")))
    await trainer.disconnect()


async def test_ramp_not_interrupted_by_fallback() -> None:
    """坡道测试不能被"自动降级"打断。

    坡道每级都在抬高目标，功率本来就滞后几秒——这正好落进自动降级那条
    "功率很稳、但稳定偏离目标"的判据里。一旦降级成闭环阻力，阻力被锁死，
    坡道再也加不上去，测试就废了。这个 bug 真的发生过。
    """
    print("\n[12] 坡道测试不会被自动降级打断")
    trainer = SimulatedTrainer(responds_to_target_power=True, cadence=85.0)
    await trainer.connect()
    session = WorkoutSession(trainer)
    # 步进加大一点，好在 60 秒内爬过几级（同样能验证降级问题）
    plan = build_test_plan("ramp", {"warmup_min": 0, "cooldown_min": 0,
                                    "start_w": 100, "step_w": 40,
                                    "max_steps": 10, "step_s": 30}, 250.0)
    await session.start(plan=plan, plan_name="坡道测试", erg_mode=ERG_FTMS,
                        config={"mode": "test", "erg_mode": "ftms", "ftp": 250.0,
                                "test": {"test_id": "ramp", "result_kind": "ramp",
                                         "multiplier": 0.75, "self_paced": False}})

    modes = []
    powers = []
    for _ in range(120):        # 约 60 秒，足够跨过两次换级、也超过降级判定的窗口
        await asyncio.sleep(0.5)
        modes.append(session.active_erg_mode)
        if session.current_power is not None:
            powers.append(session.current_power)
        if session.state != "running":
            break

    check(all(m == ERG_FTMS for m in modes),
          "全程保持原生 ERG，没有被误判成「骑行台不响应目标功率」",
          "出现过：{}".format(sorted(set(modes))))
    check(session.snapshot().get("test_level", 0) >= 3, "坡道正常逐级往上走",
          "第 {} 级".format(session.snapshot().get("test_level")))
    if powers:
        # 60 秒只够踩到第 3 级，所以拿第 2 级的目标当参照就够说明问题了
        check(max(powers) > 130, "功率确实被逐级推上去了（降级的话会锁在低位）",
              "最高 {:.0f}W".format(max(powers)))
    await session.aclose()
    await trainer.disconnect()


async def main() -> int:
    print("=" * 70)
    print("FTP 测试功能测试")
    print("=" * 70)
    test_protocols_build()
    test_ramp_shape()
    test_twenty_structure()
    test_eight_structure()
    test_param_clamping()
    await test_ramp_runs_and_computes_ftp()
    await test_ramp_stops_on_failure()
    await test_free_mode_sends_no_power()
    await test_segment_ftp_from_best_segment()
    await test_ramp_valid_when_failed_early()
    await test_test_marked_as_completed()
    await test_ramp_not_interrupted_by_fallback()

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
