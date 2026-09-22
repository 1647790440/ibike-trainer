#!/usr/bin/env python3
"""训练会话状态机的健壮性测试。

这里盯的是"反复开始/暂停/停止/关闭"这类操作顺序会不会留下坏状态。这类问题在
正常点击流程里往往看不出来，但在两个标签页同时操作、或者直接调接口时就会暴露，
而且后果通常很难查（比如一个永不放手的孤儿循环）。

    python3 tests/test_session_lifecycle.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import List

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

from ibike.session import (STALE_AFTER_S, STATE_ERROR, STATE_FINISHED,  # noqa: E402
                           STATE_IDLE, STATE_PAUSED, STATE_RUNNING, WorkoutSession)
from ibike.simulator import SimulatedTrainer  # noqa: E402
from ibike.trainer import TrainerError  # noqa: E402

_failures: List[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print("  {} {}{}".format("\033[32m✔\033[0m" if cond else "\033[31m✘\033[0m",
                             label, "  → " + detail if detail else ""))
    if not cond:
        _failures.append(label)
    return cond


def live_run_tasks() -> List[asyncio.Task]:
    """当前存活的主循环任务（排除测试自己）。"""
    out = []
    for task in asyncio.all_tasks():
        if task.done():
            continue
        coro = task.get_coro()
        if getattr(coro, "__qualname__", "").endswith("WorkoutSession._run"):
            out.append(task)
    return out


async def new_session() -> tuple:
    trainer = SimulatedTrainer(cadence=85.0)
    await trainer.connect()
    return WorkoutSession(trainer), trainer


async def test_pause_then_start() -> None:
    print("\n[1] 暂停中再开一次，不能留下孤儿循环")
    session, trainer = await new_session()
    try:
        await session.start(100, duration_min=10, erg_mode="auto")
        await asyncio.sleep(0.4)
        check(len(live_run_tasks()) == 1, "开始后有且只有一个主循环",
              "{} 个".format(len(live_run_tasks())))

        await session.pause()
        await session.start(100, duration_min=10, erg_mode="auto")
        await asyncio.sleep(0.4)
        check(len(live_run_tasks()) == 1, "暂停中重开后仍然只有一个主循环",
              "{} 个".format(len(live_run_tasks())))

        await session.stop()
        await asyncio.sleep(0.4)
        check(len(live_run_tasks()) == 0, "停止后所有主循环都退出了",
              "{} 个".format(len(live_run_tasks())))
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_repeated_cycles() -> None:
    print("\n[2] 反复 开始→停止→关闭 不会累积")
    session, trainer = await new_session()
    try:
        for i in range(4):
            await session.start(100, duration_min=10, erg_mode="auto")
            await asyncio.sleep(0.15)
            await session.stop()
            await asyncio.sleep(0.1)
            await session.dismiss()
            await asyncio.sleep(0.05)
        check(len(live_run_tasks()) == 0, "四轮循环后没有任务堆积",
              "{} 个".format(len(live_run_tasks())))
        check(session.state == STATE_IDLE, "状态回到 idle", session.state)
        check(session.summary is None, "总结已清空")
        check(session.elapsed_s == 0.0, "计时已归零", "{:.1f}".format(session.elapsed_s))
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_stop_is_idempotent() -> None:
    print("\n[3] 重复停止 / 未开始时停止都是安全的")
    session, trainer = await new_session()
    try:
        await session.stop()
        check(session.state == STATE_IDLE, "还没开始就停止：状态不变", session.state)

        await session.start(100, duration_min=10, erg_mode="auto")
        await asyncio.sleep(0.2)
        await session.stop()
        first = session.summary
        await session.stop()          # 再停一次
        check(session.state == STATE_FINISHED, "重复停止不改变状态", session.state)
        check(session.summary is first, "重复停止不会把总结重建一遍")
        check(len(live_run_tasks()) == 0, "重复停止后没有残留循环")
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_start_while_running_is_rejected() -> None:
    print("\n[4] 运行中再开一次会被明确拒绝（而不是悄悄起第二个循环）")
    session, trainer = await new_session()
    try:
        await session.start(100, duration_min=10, erg_mode="auto")
        await asyncio.sleep(0.2)
        try:
            await session.start(100, duration_min=10, erg_mode="auto")
            check(False, "运行中重开应当报错")
        except RuntimeError as exc:
            check("进行中" in str(exc), "报错信息清晰", str(exc))
        check(len(live_run_tasks()) == 1, "原来的循环还在，没有被搞坏",
              "{} 个".format(len(live_run_tasks())))
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_dismiss_only_after_finish() -> None:
    print("\n[5] 只有结束状态才能关闭总结")
    session, trainer = await new_session()
    try:
        await session.start(100, duration_min=10, erg_mode="auto")
        await asyncio.sleep(0.2)
        await session.dismiss()
        check(session.state == STATE_RUNNING, "运行中调用关闭：状态不受影响",
              session.state)
        check(len(live_run_tasks()) == 1, "循环也没被误杀",
              "{} 个".format(len(live_run_tasks())))
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_timing_rate() -> None:
    print("\n[6] 训练计时与真实时间同步（不多算也不漏算）")
    session, trainer = await new_session()
    try:
        await session.start(100, duration_min=10, erg_mode="auto")
        await asyncio.sleep(0.5)
        start = session.elapsed_s
        wall0 = time.time()
        await asyncio.sleep(3.0)
        wall = time.time() - wall0
        advanced = session.elapsed_s - start
        ratio = advanced / wall
        check(0.9 <= ratio <= 1.1, "计时速率约为 1.0×",
              "{:.2f}×（{:.1f}s / {:.1f}s）".format(ratio, advanced, wall))
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_auto_finish_clears_task() -> None:
    print("\n[7] 跑满时长自动结束后循环自行退出")
    session, trainer = await new_session()
    try:
        await session.start(100, duration_min=0.05, erg_mode="auto")   # 3 秒
        for _ in range(40):
            await asyncio.sleep(0.2)
            if session.state == STATE_FINISHED:
                break
        check(session.state == STATE_FINISHED, "状态为已完成", session.state)
        await asyncio.sleep(0.3)
        check(len(live_run_tasks()) == 0, "循环已经自己退出（不是靠取消）",
              "{} 个".format(len(live_run_tasks())))
        check(session.summary is not None, "总结已生成")
        check(session.summary.get("completed") is True, "标记为完成目标时长")
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_stale_data_stops_integration() -> None:
    print("\n[8] 设备停止上报后，不能继续按冻结的功率算下去")
    session, trainer = await new_session()
    try:
        await session.start(100, duration_min=30, erg_mode="resistance")
        await asyncio.sleep(3.0)
        trainer.set_publishing(False)          # 链路还在，只是不再上报
        await asyncio.sleep(STALE_AFTER_S + 2.0)

        snap = session.snapshot()
        check(snap["stale_data"] is True, "识别出数据已经陈旧")
        check(snap["power"] is None, "不再把最后一帧的值当作当前功率",
              str(snap["power"]))
        check(snap["cadence"] is None, "踏频同样不再显示冻结值")

        energy_before = snap["energy_kj"]
        resistance_before = snap["resistance_raw"]
        await asyncio.sleep(4.0)
        snap2 = session.snapshot()
        check(abs(snap2["energy_kj"] - energy_before) < 0.01,
              "做功不再继续累积（旧实现会按冻结功率一直加）",
              "{:.2f} → {:.2f} kJ".format(energy_before, snap2["energy_kj"]))
        check(snap2["resistance_raw"] == resistance_before,
              "闭环控制不再按冻结功率调阻力",
              "{} → {}".format(resistance_before, snap2["resistance_raw"]))

        # 数据恢复后应当自动继续
        trainer.set_publishing(True)
        await asyncio.sleep(2.0)
        snap3 = session.snapshot()
        check(snap3["stale_data"] is False, "数据恢复后告警自动解除")
        check(snap3["power"] is not None, "功率恢复显示")
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_skip_all_is_not_completed() -> None:
    print("\n[9] 一路跳过到终点，不能算「训练完成」")
    session, trainer = await new_session()
    try:
        plan = [{"name": "第{}段".format(i + 1), "kind": "work",
                 "duration_s": 6.0, "target_power": 100} for i in range(4)]
        await session.start(plan=plan, plan_name="跳过测试", erg_mode="auto")
        # 立刻跳到最后一节，只骑最后 6 秒
        for _ in range(3):
            await session.skip_step(1)
        for _ in range(60):
            await asyncio.sleep(0.3)
            if session.state == STATE_FINISHED:
                break
        summary = session.summary or {}
        check(session.state == STATE_FINISHED, "状态为已完成", session.state)
        check(summary.get("skipped_s", 0) > 10, "记录了跳过的时间",
              "{:.1f}s".format(summary.get("skipped_s", 0)))
        check(summary.get("actual_s", 999) < 12, "实际骑行时间很短",
              "{:.1f}s".format(summary.get("actual_s", 0)))
        check(summary.get("completed") is False,
              "不会被标成「训练完成」（旧实现用计划位置判断，会误判为完成）")
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_dismiss_clears_normalized_power() -> None:
    print("\n[10] 关闭总结后不该继续报上一场的标准化功率")
    session, trainer = await new_session()
    try:
        await session.start(100, duration_min=10, erg_mode="auto")
        await asyncio.sleep(0.3)
        # 直接注入一个"长训练"的 NP 累积值，省得真等 60 秒
        session.active_s = 120.0
        session._np_sum = 3.4e11
        session._np_count = 671
        await session.stop()
        check(session.snapshot()["normalized_power"] is not None,
              "停止后 NP 有值", str(session.snapshot()["normalized_power"]))

        await session.dismiss()
        snap = session.snapshot()
        check(snap["state"] == STATE_IDLE, "回到 idle", snap["state"])
        check(snap["normalized_power"] is None, "NP 已归零",
              str(snap["normalized_power"]))
        check(snap["power_max"] == 0.0, "最大功率已归零", str(snap["power_max"]))
        check(snap["elapsed_s"] == 0.0, "计时已归零")
    finally:
        await session.aclose()
        await trainer.disconnect()


class _FailingTargetTrainer(SimulatedTrainer):
    """固件接受 Start 但拒绝设定目标功率（回 Operation Failed）。"""

    async def set_target_power(self, watts: int) -> None:
        raise TrainerError("设定目标功率失败：Operation Failed")


async def test_start_failure_leaves_consistent_state() -> None:
    print("\n[11] 开始训练时下发失败，状态不能自相矛盾")
    trainer = _FailingTargetTrainer(cadence=85.0)
    await trainer.connect()
    session = WorkoutSession(trainer)
    try:
        try:
            await session.start(100, duration_min=10, erg_mode="ftms")
            check(False, "下发失败时应当抛错")
        except TrainerError as exc:
            check("目标功率" in str(exc), "抛出了明确的错误", str(exc))
        check(session.state == STATE_ERROR, "状态是 error 而不是 idle",
              session.state)
        check(bool(session.error_message), "记录了错误信息")
        check(trainer.has_control is False,
              "已经把骑行台放开（旧实现会留下「状态是 idle 但设备被占住」）")
        check(len(live_run_tasks()) == 0, "没有残留主循环")
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_concurrent_stop_sends_one_command() -> None:
    print("\n[12] 并发停止只下发一次 STOP")
    session, trainer = await new_session()
    try:
        await session.start(100, duration_min=10, erg_mode="auto")
        await asyncio.sleep(0.3)
        await asyncio.gather(session.stop(), session.stop(), session.stop())
        stops = [c for c in trainer._commands if c[0] == "stop"]
        check(len(stops) == 1, "STOP 只下发了一次",
              "{} 次".format(len(stops)))
        check(session.state == STATE_FINISHED, "状态为 finished", session.state)
        check(session.summary is not None, "总结生成了一次")
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_resume_keeps_resistance() -> None:
    """暂停再继续不能把闭环控制调好的阻力丢掉。

    真实事故：用户骑了 1 小时 130W，中途正常暂停了一次，继续之后阻力从 17-18
    突然掉到 6，然后一分多钟才慢慢爬回 17。原因是 resume() 重建了控制器并
    按"量程的四分之一"重新播种，把已经收敛的档位整个扔掉了。
    """
    print("\n[13] 暂停继续不该重置闭环控制的阻力档位")
    session, trainer = await new_session()
    try:
        await session.start(130, duration_min=30, erg_mode="resistance")
        # 等闭环收敛
        for _ in range(200):
            await asyncio.sleep(0.5)
            snap = session.snapshot()
            if snap["resistance_raw"] is not None and snap["controller_k"]:
                break
        await asyncio.sleep(10)
        before = session.snapshot()
        check(before["resistance_raw"] is not None, "闭环已经给出档位",
              str(before["resistance_raw"]))

        await session.pause()
        await session.resume()
        await asyncio.sleep(0.5)
        after = session.snapshot()

        delta = abs((after["resistance_raw"] or 0) - (before["resistance_raw"] or 0))
        check(delta < 2.0, "继续后档位基本不变（修复前会掉到量程的 1/4）",
              "%.1f → %.1f（差 %.1f）" % (
                  (before["resistance_raw"] or 0) / 10,
                  (after["resistance_raw"] or 0) / 10, delta / 10))
        check(after["controller_k"] == before["controller_k"],
              "在线辨识出的系数 k 也保留了",
              "{} → {}".format(before["controller_k"], after["controller_k"]))
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_mode_changes_are_recorded() -> None:
    """控功率方式的切换必须记进总结。

    只看最终模式是查不出"阻力为什么突然变了"的——得知道什么时候切的、为什么切。
    """
    print("\n[14] 控功率方式的切换会被记录")
    # 这台模拟台声明支持目标功率但实际不执行，会触发自动降级
    session, trainer = await new_session()
    try:
        trainer.responds_to_target_power = False
        trainer.advertise_power_target = True
        trainer.capabilities["supports_power_target"] = True
        await session.start(100, duration_min=30, erg_mode="auto",
                            config={"mode": "constant", "erg_mode": "auto",
                                    "target_power": 100.0, "duration_min": 30.0})
        check(session.active_erg_mode == "ftms", "一开始用的是原生 ERG")
        for _ in range(160):
            await asyncio.sleep(0.5)
            if session.active_erg_mode == "resistance":
                break
        check(session.active_erg_mode == "resistance", "自动降级到闭环阻力")
        await session.stop()
        changes = (session.summary or {}).get("mode_changes") or []
        check(any(c.get("kind") == "probe" for c in changes),
              "先记录了降级前的阶跃探测",
              str([c.get("kind") for c in changes]))
        switches = [c for c in changes if c.get("to") == "resistance"]
        if check(len(switches) >= 1, "总结里记录了切换", str(changes)):
            item = switches[0]
            check(item["from"] == "ftms" and item["to"] == "resistance",
                  "记录里含切换方向", "{} → {}".format(item["from"], item["to"]))
            check("降级" in item["reason"], "记录里含切换原因", item["reason"])
            check(isinstance(item["t"], (int, float)) and item["t"] > 0,
                  "记录里含切换时间点", "第 {:.0f} 秒".format(item["t"]))
            check(item["t"] > max(c["t"] for c in changes if c.get("kind") == "probe"),
                  "切换发生在探测之后")
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_explicit_ftms_is_never_downgraded() -> None:
    """用户明确选了「原生 ERG」时，程序绝不能自作主张切走。

    自动降级是 auto 模式为了兼容性兜的底，不该反过来覆盖用户的明确选择。
    真机复盘的教训：选了 auto 的 60 分钟骑行在 15 分钟处被自动降级，
    阻力从 17.8 掉到 6.4 再慢慢爬回来——用户要的是"别背着我改主意"。
    """
    print("\n[15] 明确选「原生 ERG」时不会自动降级")
    session, trainer = await new_session()
    try:
        trainer.responds_to_target_power = False
        trainer.advertise_power_target = True
        trainer.capabilities["supports_power_target"] = True
        # 把判据压到最短，如果这条规则失效，几秒内就会切走
        session.FALLBACK_GRACE_S = 1.0
        session.FALLBACK_HOLD_S = 2.0
        session.PROBE_WAIT_S = 2.0
        await session.start(100, duration_min=30, erg_mode="ftms")
        for _ in range(30):       # 15 秒，足够触发降级判据好几轮
            await asyncio.sleep(0.5)
        check(session.active_erg_mode == "ftms", "仍然停留在原生 ERG",
              str(session.active_erg_mode))
        check(not (session.summary or {}).get("mode_changes"),
              "总结里没有任何模式切换记录",
              str((session.summary or {}).get("mode_changes")))

        # 对照组：同样一台"装死"的台子，auto 模式就该降级
        session2, trainer2 = await new_session()
        try:
            trainer2.responds_to_target_power = False
            trainer2.advertise_power_target = True
            trainer2.capabilities["supports_power_target"] = True
            session2.FALLBACK_GRACE_S = 1.0
            session2.FALLBACK_HOLD_S = 2.0
            session2.PROBE_WAIT_S = 2.0
            await session2.start(100, duration_min=30, erg_mode="auto")
            for _ in range(30):
                await asyncio.sleep(0.5)
                if session2.active_erg_mode == "resistance":
                    break
            check(session2.active_erg_mode == "resistance",
                  "同样的台子在 auto 下会降级", str(session2.active_erg_mode))
        finally:
            await session2.aclose()
            await trainer2.disconnect()
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_stale_gap_does_not_dilute_average() -> None:
    """数据断链的那一段时间，不能算进"平均功率"的分母。

    断链时功率积分停了，但 active_s 还在走。早期实现拿 active_s 当分母，
    实测一次 20 秒的掉线就把 100W 的骑行在报告里写成 45W——而同一份报告里的
    做功和区间占比是用有效样本算的，于是报告自己跟自己矛盾。
    """
    print("\n[16] 数据断链不会把平均功率摊薄")
    import statistics
    session, trainer = await new_session()
    try:
        await session.start(120, duration_min=30, erg_mode="auto")
        await asyncio.sleep(6.0)
        trainer.set_publishing(False)          # 链路还在，只是不再上报
        await asyncio.sleep(20.0)
        trainer.set_publishing(True)
        await asyncio.sleep(6.0)
        await session.stop()

        summary = session.summary or {}
        powers = [p["p"] for p in session._trace if p.get("p") is not None]
        avg = summary.get("avg_power")
        curve = statistics.mean(powers) if powers else None
        check(summary.get("sampled_s") is not None, "报告里记了「有数据的时间」",
              "{} 秒".format(summary.get("sampled_s")))
        check(summary.get("sampled_s", 0) < summary.get("actual_s", 0),
              "断链那段没有算进有数据的时间",
              "有数据 {}s / 总共 {}s".format(
                  summary.get("sampled_s"), summary.get("actual_s")))
        if check(avg is not None and curve is not None, "报告里有平均功率"):
            check(abs(avg - curve) < 3.0,
                  "平均功率和曲线对得上（修复前 45W vs 100W）",
                  "报告 {:.1f}W / 曲线 {:.1f}W".format(avg, curve))
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_pause_is_not_charged_to_the_step() -> None:
    """暂停的那段时间不能算成"这一小节骑了多久"。

    trace 里每个点的 dt 是"距上一个点的间隔"。暂停期间不记点，但 _last_trace
    还停在暂停前——恢复后的第一个点就会带上一整段暂停时间。它会被算进分段时长、
    区间分布，甚至 FTP 测试的"计时段完成度"（实测能算出 150% 这种数字）。
    """
    print("\n[17] 暂停时间不会被算进这一小节")
    from ibike.workouts import build_plan
    session, trainer = await new_session()
    plan = build_plan("hiit_30_15",
                      {"warmup_min": 0, "cooldown_min": 0, "reps": 4,
                       "work_s": 30, "rest_s": 15}, 200.0)
    try:
        await session.start(200, duration_min=30, erg_mode="auto", plan=plan,
                            plan_name="30/15 间歇", config={"mode": "interval"})
        await asyncio.sleep(3.0)
        await session.pause()
        await asyncio.sleep(8.0)          # 暂停 8 秒
        await session.resume()
        await asyncio.sleep(5.0)
        await session.stop()

        dts = [p["dt"] for p in session._trace if p.get("dt")]
        check(bool(dts), "轨迹里有采样点", "{} 个".format(len(dts)))
        if dts:
            check(max(dts) < 3.0,
                  "没有哪个采样点的 dt 把整段暂停吞进去（修复前会是 8 秒以上）",
                  "最大 dt = {:.1f}s".format(max(dts)))
        rows = (session.summary or {}).get("intervals") or []
        total_actual = sum(r.get("actual_s") or 0 for r in rows)
        sampled = (session.summary or {}).get("sampled_s") or 0
        check(total_actual <= sampled + 1.5,
              "分段时长之和不超过真正有数据的时间",
              "分段 {:.1f}s / 有数据 {:.1f}s".format(total_actual, sampled))
    finally:
        await session.aclose()
        await trainer.disconnect()


async def main() -> int:
    print("=" * 70)
    print("训练会话状态机健壮性测试")
    print("=" * 70)
    await test_pause_then_start()
    await test_repeated_cycles()
    await test_stop_is_idempotent()
    await test_start_while_running_is_rejected()
    await test_dismiss_only_after_finish()
    await test_timing_rate()
    await test_auto_finish_clears_task()
    await test_stale_data_stops_integration()
    await test_skip_all_is_not_completed()
    await test_dismiss_clears_normalized_power()
    await test_start_failure_leaves_consistent_state()
    await test_concurrent_stop_sends_one_command()
    await test_resume_keeps_resistance()
    await test_mode_changes_are_recorded()
    await test_explicit_ftms_is_never_downgraded()
    await test_stale_gap_does_not_dilute_average()
    await test_pause_is_not_charged_to_the_step()

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
