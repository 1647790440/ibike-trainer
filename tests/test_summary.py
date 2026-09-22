#!/usr/bin/env python3
"""训练总结的测试。

    python3 tests/test_summary.py
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

from aiohttp import ClientSession, web  # noqa: E402

from ibike.session import WorkoutSession  # noqa: E402
from ibike.server import Server  # noqa: E402

_failures: List[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print("  {} {}{}".format("\033[32m✔\033[0m" if cond else "\033[31m✘\033[0m",
                             label, "  → " + detail if detail else ""))
    if not cond:
        _failures.append(label)
    return cond


# ======================================================================
# 纯函数部分
# ======================================================================


def test_downsample() -> None:
    print("\n[1] 曲线降采样：不能丢掉功率尖峰")

    trace = [{"t": float(i), "dt": 1.0, "p": 100.0, "target": 100.0} for i in range(5000)]
    # 埋一个孤立的尖峰
    trace[3210]["p"] = 480.0

    out = WorkoutSession._downsample(trace, limit=600)
    check(len(out) <= 604, "点数被压到设定的上限以内", "{} 个点".format(len(out)))
    check(any(abs(p["p"] - 480.0) < 0.01 for p in out),
          "孤立的功率尖峰被保留下来（这是用抽点法会丢掉的关键信息）",
          "最大 {:.0f}W".format(max(p["p"] for p in out)))
    check(out[0]["t"] == trace[0]["t"], "起始点保留")
    check(all("t" in p and "p" in p and "target" in p for p in out),
          "输出字段符合前端需要")

    short = WorkoutSession._downsample(trace[:100], limit=600)
    check(len(short) == 100, "点数不多时不压缩", "{} 个点".format(len(short)))


def test_distribution() -> None:
    print("\n[2] 功率区间分布")
    trace = [
        {"t": 0.0, "dt": 10.0, "p": 80.0, "target": 100.0},    # 80%  → 低于 90%
        {"t": 10.0, "dt": 10.0, "p": 94.0, "target": 100.0},   # 94%  → 90–97%
        {"t": 20.0, "dt": 30.0, "p": 100.0, "target": 100.0},  # 100% → 97–103%
        {"t": 50.0, "dt": 20.0, "p": 106.0, "target": 100.0},  # 106% → 103–110%
        {"t": 70.0, "dt": 30.0, "p": 130.0, "target": 100.0},  # 130% → 高于 110%
    ]
    dist = WorkoutSession._power_distribution(trace)
    check(len(dist) == 5, "分成 5 档")
    check(dist[2]["seconds"] == 30.0, "中间的 97–103% 档统计正确",
          "{}s".format(dist[2]["seconds"]))
    check(dist[4]["seconds"] == 30.0, "最高档统计正确", "{}s".format(dist[4]["seconds"]))
    total = sum(b["pct"] for b in dist)
    check(abs(total - 100.0) < 0.5, "各档占比合计约 100%", "{:.1f}%".format(total))

    empty = WorkoutSession._power_distribution([])
    check(all(b["pct"] == 0 for b in empty), "空轨迹不除以零")


def test_normalized_power() -> None:
    print("\n[3] 标准化功率的算法与有效性门槛")
    from ibike.simulator import SimulatedTrainer

    def feed(session, power_at, ticks=10 * 200):
        """按 10Hz 喂数据，power_at(t) 返回该时刻的功率。

        积分、时长都由实际喂进去的数据算出来，避免手写常量和时长对不上。
        注意 elapsed_s 是"计划位置"（跳过时会跳变），active_s 才是真正骑行的时间，
        平均功率以后者为准。
        """
        integral = 0.0
        t = 0.0
        for i in range(ticks):
            t = i / 10.0
            p = power_at(t)
            session.current_power = p
            session._update_normalized_power(t)
            integral += p * 0.1
        session.elapsed_s = t
        session.active_s = t
        session._power_integral = integral
        # 平均功率的分母是"真正有数据的秒数"（见 _build_summary）。
        # 这里整段都有数据，所以它就等于骑行时间；不设的话 avg_power 会是 None。
        session._sampled_s = t

    # 恒定 100W → NP 必须正好是 100W
    s = WorkoutSession(SimulatedTrainer())
    feed(s, lambda t: 100.0, ticks=10 * 200)
    summary = s._build_summary("测试")
    check(summary["normalized_power"] is not None, "长训练会给出标准化功率")
    check(abs(summary["avg_power"] - 100.0) < 0.5, "恒定功率下平均功率为 100W",
          "{:.1f}W".format(summary["avg_power"]))
    check(abs(summary["normalized_power"] - 100.0) < 0.5,
          "恒定功率下 NP 等于平均功率", "{:.1f}W".format(summary["normalized_power"]))

    # 前 60 秒 50W、后 60 秒 150W → 平均 100W，但 NP 应当高于平均
    s2 = WorkoutSession(SimulatedTrainer())
    feed(s2, lambda t: 50.0 if t < 60 else 150.0, ticks=10 * 120)
    summary2 = s2._build_summary("测试")
    check(abs(summary2["avg_power"] - 100.0) < 0.5, "波动数据的平均功率为 100W",
          str(summary2["avg_power"]))
    check(summary2["normalized_power"] > summary2["avg_power"],
          "波动越大 NP 越高（这正是 NP 的意义）",
          "NP {:.1f}W > 平均 {:.1f}W".format(summary2["normalized_power"],
                                             summary2["avg_power"]))

    # 短训练：NP 没有意义，必须留空
    s3 = WorkoutSession(SimulatedTrainer())
    feed(s3, lambda t: 100.0, ticks=10 * 20)   # 只有 20 秒
    summary3 = s3._build_summary("测试")
    check(summary3["normalized_power"] is None,
          "训练不足 60 秒时不显示 NP（否则会算出低于平均值的假数字）",
          str(summary3["normalized_power"]))


def test_np_window_is_really_30_seconds() -> None:
    print("\n[4] 标准化功率的窗口必须真的是 30 秒")
    from ibike.simulator import SimulatedTrainer

    session = WorkoutSession(SimulatedTrainer())
    # 按 10Hz 喂 29 秒（290 个样本）
    for i in range(290):
        session.current_power = 100.0
        session._update_normalized_power(i / 10.0)
    check(session._np_count == 0,
          "29 秒时还没有开始计入 NP（旧实现拿样本数当秒数，3 秒就开始了）",
          "已计入 {} 次".format(session._np_count))

    for i in range(290, 330):
        session.current_power = 100.0
        session._update_normalized_power(i / 10.0)
    check(session._np_count > 0, "跨过 30 秒后开始计入",
          "已计入 {} 次".format(session._np_count))


def test_np_matches_reference() -> None:
    print("\n[5] 与标准 30 秒滑窗参考实现对比")
    from ibike.simulator import SimulatedTrainer

    # 前 60 秒 300W，之后 100W —— 剧烈变化最能暴露窗口长度不对的问题
    samples = [(i / 10.0, 300.0 if i / 10.0 < 60 else 100.0) for i in range(10 * 180)]

    session = WorkoutSession(SimulatedTrainer())
    session.active_s = 180.0
    for t, p in samples:
        session.current_power = p
        session._update_normalized_power(t)
    got = (session._np_sum / session._np_count) ** 0.25

    # 参考实现：窗口 = [t-30, t]，且要求窗口真的横跨 30 秒
    left = 0
    running = 0.0
    total = 0.0
    count = 0
    for right, (t, p) in enumerate(samples):
        running += p
        while samples[left][0] < t - 30.0:
            running -= samples[left][1]
            left += 1
        span = t - samples[left][0]
        if span >= 29.8:
            total += (running / (right - left + 1)) ** 4
            count += 1
    reference = (total / count) ** 0.25

    check(abs(got - reference) < 1.0, "与参考实现一致",
          "本实现 {:.1f}W vs 参考 {:.1f}W".format(got, reference))


# ======================================================================
# 端到端
# ======================================================================


async def run_workout(http: ClientSession, base: str, duration_min: float,
                      stop_early_at: float = 0.0) -> Dict[str, Any]:
    async with http.post(base + "/api/start",
                         json={"target_power": 100, "duration_min": duration_min,
                               "erg_mode": "auto"}) as r:
        assert (await r.json()).get("ok"), "开始训练失败"

    if stop_early_at > 0:
        await asyncio.sleep(stop_early_at)
        async with http.post(base + "/api/stop", json={}) as r:
            await r.json()
    else:
        # 等它自己跑完
        for _ in range(120):
            await asyncio.sleep(1.0)
            async with http.get(base + "/api/state") as r:
                data = await r.json()
            if data.get("state") == "finished":
                break

    async with http.get(base + "/api/state") as r:
        return await r.json()


async def test_summary_end_to_end() -> None:
    print("\n[3] 完整训练结束后生成总结")
    server = Server(use_simulator=True)
    app = server.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = "http://127.0.0.1:{}".format(runner.addresses[0][1])

    try:
        async with ClientSession() as http:
            data = await run_workout(http, base, duration_min=0.4)
            check(data.get("state") == "finished", "训练已结束", str(data.get("state")))

            s = data.get("summary")
            if not check(bool(s), "快照里带回了训练总结"):
                return

            check(s.get("completed") is True, "标记为「完成目标时长」")
            check(abs(s.get("actual_s", 0) - s.get("planned_s", 0)) <= 2.0,
                  "实际时长与计划一致",
                  "{:.0f}s / {:.0f}s".format(s["actual_s"], s["planned_s"]))
            check(s.get("target_power") == 100.0, "记录了目标功率", str(s.get("target_power")))

            # 只有 24 秒，均值会被起步爬升段拉低，所以分两段看：
            # 整体落在合理范围，收敛后的后半段必须真正贴合目标。
            trace = s.get("trace") or []
            check(s.get("avg_power") is not None and 75 <= s["avg_power"] <= 115,
                  "整体平均功率在合理范围（含起步爬升段）",
                  "{:.1f}W".format(s.get("avg_power") or 0))
            if check(len(trace) >= 10, "采集到足够的轨迹点", "{} 个".format(len(trace))):
                tail = trace[len(trace) // 2:]
                tail_avg = sum(p["p"] for p in tail) / len(tail)
                check(abs(tail_avg - 100.0) <= 6.0,
                      "收敛后的后半段功率贴合目标",
                      "{:.1f}W".format(tail_avg))

            check(s.get("max_power", 0) >= (s.get("avg_power") or 0),
                  "最大功率不小于平均功率")
            check(s.get("avg_cadence") is not None, "算了平均踏频",
                  "{:.1f}rpm".format(s.get("avg_cadence") or 0))
            check(s.get("energy_kj", 0) > 0, "算了做功", "{:.1f}kJ".format(s.get("energy_kj") or 0))
            check(s.get("energy_kcal_est", 0) > s.get("energy_kj", 0),
                  "估算消耗大于机械做功（换算效率 < 100%）",
                  "{:.0f}kcal vs {:.1f}kJ".format(s.get("energy_kcal_est") or 0,
                                                  s.get("energy_kj") or 0))
            check(s.get("distance_m", 0) > 0, "算了距离",
                  "{:.2f}km".format((s.get("distance_m") or 0) / 1000))

            pct = s.get("in_zone_pct")
            check(pct is not None and 0 <= pct <= 100, "算了控功率达标率",
                  "{}%".format(pct))
            check(s.get("in_zone_tolerance_w") == 5.0, "达标容差为目标 ±5%（且不低于 5W）",
                  str(s.get("in_zone_tolerance_w")))
            check(s.get("mean_abs_deviation_w") is not None, "算了平均绝对偏差",
                  "{}W".format(s.get("mean_abs_deviation_w")))
            check(s.get("started_at") and s.get("finished_at"), "记录了起止时间")
            check(bool(s.get("erg_mode_label")), "记录了控功率方式", str(s.get("erg_mode_label")))
            check(s.get("requested_erg_mode") == "auto", "记录了请求的模式（供「再来一次」用）")

            dist = s.get("distribution") or []
            check(len(dist) == 5, "带了 5 档功率分布")
            check(abs(sum(b["pct"] for b in dist) - 100.0) < 1.0,
                  "分布合计约 100%", "{:.1f}%".format(sum(b["pct"] for b in dist)))

            check(len(trace) >= 10, "带了功率曲线轨迹", "{} 个点".format(len(trace)))
            # 保留 res / cadence / hr 是有意的：报告是事后复盘问题的唯一依据，
            # 把这几列丢掉的话"阻力为什么突然掉了""心率怎么飘成这样"都无从查起
            check(all(set(p.keys()) == {"t", "p", "target", "res", "cadence", "hr"}
                      for p in trace),
                  "轨迹带上了功率、目标、阻力、踏频和心率",
                  str(sorted(trace[0].keys())) if trace else "无")

    finally:
        await runner.cleanup()


async def test_early_stop_and_dismiss() -> None:
    print("\n[4] 提前结束 & 关闭总结")
    server = Server(use_simulator=True)
    app = server.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = "http://127.0.0.1:{}".format(runner.addresses[0][1])

    try:
        async with ClientSession() as http:
            data = await run_workout(http, base, duration_min=30.0, stop_early_at=6.0)
            s = data.get("summary") or {}
            check(data.get("state") == "finished", "提前结束也进入 finished")
            check(s.get("completed") is False, "标记为「提前结束」而不是完成")
            check(s.get("actual_s", 999) < 60, "实际时长反映的是真实骑行时间",
                  "{:.0f}s".format(s.get("actual_s") or 0))
            check("用户停止" in str(s.get("reason")), "记录了结束原因", str(s.get("reason")))

            async with http.post(base + "/api/dismiss", json={}) as r:
                d = await r.json()
            check(d.get("ok"), "关闭总结成功")

            async with http.get(base + "/api/state") as r:
                st = await r.json()
            check(st.get("state") == "idle", "关闭后回到 idle", str(st.get("state")))
            check(st.get("summary") is None, "总结已被清除")
            check((st.get("trainer") or {}).get("connected") is True,
                  "关闭总结不影响骑行台连接")

            # 关掉之后应该能立刻再来一次
            async with http.post(base + "/api/start",
                                 json={"target_power": 120, "duration_min": 1,
                                       "erg_mode": "auto"}) as r:
                d = await r.json()
            check(d.get("ok"), "关闭总结后可以马上开始新的训练")
            async with http.get(base + "/api/state") as r:
                st = await r.json()
            check(st.get("summary") is None, "新训练开始时旧总结已清掉")

    finally:
        await runner.cleanup()


async def main() -> int:
    print("=" * 70)
    print("训练总结测试")
    print("=" * 70)
    test_downsample()
    test_distribution()
    test_normalized_power()
    test_np_window_is_really_30_seconds()
    test_np_matches_reference()
    await test_summary_end_to_end()
    await test_early_stop_and_dismiss()

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
