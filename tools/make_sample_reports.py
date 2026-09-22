#!/usr/bin/env python3
"""生成几份示例训练报告，用于在没有骑行台、或者没时间真骑的时候预览报告界面。

**这些报告是合成的。** 但格式和真实训练完全一致：功率曲线用一阶惯性模型模拟
ERG 的实际响应（起步爬升、飞轮滞后、稳态小幅波动），然后走真实的
`WorkoutSession._build_summary` 计算达标率、功率区间分布、分段明细和降采样曲线。
所以界面上看到的就是真东西，不是手写的假数据。

    python3 tools/make_sample_reports.py            # 生成示例报告
    python3 tools/make_sample_reports.py --clear    # 清空所有训练报告（含真实的）
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from bisect import bisect_right
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibike.custom import CustomStore, build_custom_steps  # noqa: E402
from ibike.ftptest import KIND_TEST, build_test_plan  # noqa: E402
from ibike.reports import ReportStore  # noqa: E402
from ibike.session import ERG_FTMS, ERG_RESISTANCE, WorkoutSession  # noqa: E402
from ibike.simulator import SimulatedTrainer  # noqa: E402
from ibike.workouts import build_plan  # noqa: E402

DAY = 86400.0


# ----------------------------------------------------------------------
# 模拟骑行台的功率响应
# ----------------------------------------------------------------------


def _simulate(plan: List[Dict[str, Any]], *, seed: int, cadence_base: float = 87.0,
              tau: float = 2.5, until: Optional[float] = None,
              resistance_mode: bool = False,
              ceiling_w: Optional[float] = None) -> Dict[str, Any]:
    """按 10Hz 模拟一次骑行，返回功率/踏频序列和累计量。

    原生 ERG 用一阶惯性趋近目标（模拟固件内部闭环），起步时有明显的爬升过程。
    闭环阻力模式收敛更慢、波动更大——那正是它和原生 ERG 的真实差别。

    ``ceiling_w`` 是骑手的能力上限，用来复刻坡道测试：目标一旦超过它，功率就再也
    跟不上，持续几秒后判定力竭并结束——和 session.py 里真实的判据一致。
    """
    rng = random.Random(seed)
    bounds: List[float] = []
    acc = 0.0
    for step in plan:
        acc += float(step["duration_s"])
        bounds.append(acc)
    starts = [0.0] + bounds[:-1]
    total = bounds[-1]
    end = total if until is None else min(until, total)

    dt = 0.1
    power = 0.0
    cadence = cadence_base
    distance = 0.0
    integral = 0.0
    cadence_integral = 0.0
    hr = 58.0
    hr_integral = 0.0
    hr_time = 0.0
    hr_max = 0.0
    cadence_max = 0.0
    max_power = 0.0
    trace: List[Dict[str, Any]] = []
    samples_10hz: List[tuple] = []

    # 闭环阻力模式：复刻 session.py 里真实的控制律——每 4 秒按误差修正一次阻力档位
    res_raw = 60.0
    k_hat: Optional[float] = None
    last_adj = -99.0

    low_since: Optional[float] = None
    step_start = 0.0
    t = 0.0
    last_trace = -1.0
    while t < end:
        idx = min(bisect_right(bounds, t), len(plan) - 1)
        if t - step_start > 0.5:
            pass
        target = float(plan[idx]["target_power"])
        if idx > 0 and starts[idx] != step_start:
            step_start = starts[idx]
            low_since = None

        # 踏频：慢漂移 + 快抖动，和模拟骑行台用的是同一套感觉
        cadence = cadence_base + 3.0 * math.sin(t / 23.0) + 1.8 * math.sin(t / 7.0) \
            + rng.uniform(-1.2, 1.2)
        cadence = max(0.0, cadence)

        if resistance_mode:
            if t - last_adj >= 4.0:
                last_adj = t
                if cadence > 25.0 and power > 0.0 and res_raw > 1.0:
                    sample_k = power / (res_raw * cadence)
                    if 0.0 < sample_k < 100.0:
                        k_hat = sample_k if k_hat is None \
                            else 0.88 * k_hat + 0.12 * sample_k
                error = target - power
                if k_hat and cadence > 25.0 and abs(error) > 3.0:
                    delta = max(-10.0, min(10.0, 0.55 * error / (k_hat * cadence)))
                    res_raw = max(5.0, min(255.0, res_raw + delta))
            # 骑行台的物理响应：功率正比于 阻力 × 踏频，带飞轮惯性
            plant_target = 0.025 * res_raw * cadence
            power += (plant_target - power) * (1.0 - math.exp(-dt / tau))
            power *= (1.0 + rng.uniform(-0.012, 0.012))
        else:
            # 原生 ERG：固件内部闭环，一阶趋近目标；但骑手有功率上限
            goal = target if ceiling_w is None else min(target, ceiling_w)
            power += (goal - power) * (1.0 - math.exp(-dt / tau))
            power *= (1.0 + rng.uniform(-0.013, 0.013))
        power = max(0.0, power)

        # 坡道测试的力竭判定：过了宽限期之后，功率持续跟不上目标就算踩不动了
        if ceiling_w is not None and plan[idx].get("kind") == "test":
            if t - step_start >= 6.0:
                if power < target * 0.80:
                    if low_since is None:
                        low_since = t
                    elif t - low_since >= 5.0:
                        end = t
                        break
                else:
                    low_since = None

        integral += power * dt
        if power > max_power:
            max_power = power
        cadence_integral += cadence * dt
        cadence_max = max(cadence_max, cadence)
        # 心率：跟着功率走，但时间常数远大于功率（约 25 秒），这就是真实心率的样子。
        # 报告里的心率曲线/区间分布靠它，否则示例报告里那块永远是空的。
        hr_target = 58.0 + 130.0 * min(1.0, power / 260.0)
        hr += (hr_target - hr) * (1.0 - math.exp(-dt / 25.0))
        hr *= (1.0 + rng.uniform(-0.004, 0.004))
        # 700c 轮周长 2.105m，中等齿比约 50/17 ≈ 2.94（踏频 87 时约 32km/h）
        distance += (cadence / 60.0 * 2.94 * 2.105) * dt

        samples_10hz.append((t, power))
        hr_integral += hr * dt
        hr_time += dt
        hr_max = max(hr_max, hr)

        if t - last_trace >= 1.0:
            last_trace = t
            trace.append({
                "t": round(t, 1),
                "dt": 1.0,
                "p": round(power, 1),
                "target": round(target, 1),
                "step": idx,
                "cadence": round(cadence, 1),
                "res": round(res_raw, 1) if resistance_mode else None,
                "hr": round(hr, 1),
            })
        t += dt

    return {
        "plan": plan,
        "bounds": bounds,
        "starts": starts,
        "total": total,
        "ridden": end,
        "trace": trace,
        "samples": samples_10hz,
        "integral": integral,
        "max_power": max_power,
        "cadence_integral": cadence_integral,
        "hr_integral": hr_integral,
        "hr_time": hr_time,
        "hr_max": hr_max,
        "cadence_time": end,
        "cadence_max": cadence_max,
        "distance": distance,
    }


def _build_summary(sim: Dict[str, Any], *, plan_name: str, started_at: float,
                   stopped_early: bool, mode: str, config: Dict[str, Any],
                   trainer_name: str) -> Dict[str, Any]:
    """把模拟结果灌进真实的会话对象，用真实逻辑算出总结。"""
    session = WorkoutSession(SimulatedTrainer())
    session.trainer.name = trainer_name            # type: ignore[attr-defined]

    session.plan = sim["plan"]
    session.plan_name = plan_name
    session.duration_s = sim["total"]
    session._step_bounds = sim["bounds"]
    session._step_starts = sim["starts"]
    session._work_indices = [i for i, s in enumerate(sim["plan"])
                             if s.get("kind") == "work"]
    session._work_position = {idx: n + 1 for n, idx in enumerate(session._work_indices)}
    session.step_index = len(sim["plan"]) - 1

    session.active_erg_mode = mode
    session.erg_mode = mode
    session.workout_config = config
    # 正常流程里 _test_meta 是 start() 从 config 里取的；这里绕过了 start，
    # 所以要手动设上，否则总结算不出测试结果
    session._test_meta = config.get("test") or None
    session._test_indices = [i for i, st in enumerate(sim["plan"])
                             if st.get("kind") == KIND_TEST]
    session.target_power = float(sim["plan"][0]["target_power"])

    session._trace = sim["trace"]
    session._power_integral = sim["integral"]
    # 平均功率的分母是"真正有数据的秒数"（见 session._build_summary）：示例数据
    # 是完完整整一段，所以有数据的时间就等于骑行时间。不设这个字段的话
    # avg_power 会算成 None，示例报告里就会出现"平均功率 --"。
    session._sampled_s = sim["ridden"]
    session.max_power = sim["max_power"]
    session.energy_kj = sim["integral"] / 1000.0
    session.distance_m = sim["distance"]
    session._cadence_integral = sim["cadence_integral"]
    session._hr_integral = sim["hr_integral"]
    session._hr_time = sim["hr_time"]
    session._hr_max = sim["hr_max"]
    session.hr_source = "strap"
    session.rr_intervals = 1
    # 心率区间需要一套心率设置，示例报告按最常见的默认值来
    session.hr_config = {"max_hr": 185, "rest_hr": 58, "zone_mode": "reserve",
                         "hr_limit_enabled": False, "hr_limit_bpm": 165}
    session._cadence_time = sim["cadence_time"]
    session._cadence_max = sim["cadence_max"]

    # NP 必须按 10Hz 喂进真实的滑窗累加器，才能得到和真骑一致的结果
    for t, power in sim["samples"]:
        session.current_power = power
        session._update_normalized_power(t)
    session.current_power = None

    # 真实骑行时间一律是 ridden：跑满时它等于 total，而"中途停下"和"坡道踩到
    # 力竭自动结束"这两种情况下它短于 total。以前后者写成 total，于是报告会
    # 声称骑了 38 分钟、可积分只覆盖 21 分钟，平均功率和做功自相矛盾。
    session.elapsed_s = sim["ridden"]
    session.active_s = sim["ridden"]
    reason = "用户停止" if stopped_early else "完成目标时长"

    session.started_at = started_at
    session.finished_at = started_at + sim["ridden"]
    return session._build_summary(reason)


# ----------------------------------------------------------------------
# 示例课表
# ----------------------------------------------------------------------


def _constant_plan(power: int, minutes: float) -> List[Dict[str, Any]]:
    return [{"name": "恒定功率", "kind": "work", "duration_s": minutes * 60,
             "pct_ftp": None, "target_power": power}]


SAMPLES = [
    # (标题, 课表, FTP, 骑行分钟数(用于缩放到大致时长), 模式, 中途停止, 距今第几天, 几点)
    {
        "key": "endurance",
        "plan": lambda: _constant_plan(150, 60),
        "name": "恒定功率",
        "mode": ERG_FTMS,
        "early": False,
        "days_ago": 9, "hour": 19, "minute": 10, "seed": 11,
        "config": {"mode": "constant", "erg_mode": "ftms",
                   "target_power": 150.0, "duration_min": 60.0},
    },
    {
        "key": "4x4",
        "plan": lambda: build_plan("hiit_4x4", None, 245.0),
        "name": "4×4 分钟",
        "mode": ERG_FTMS,
        "early": False,
        "days_ago": 7, "hour": 7, "minute": 30, "seed": 22,
        "config": {"mode": "interval", "erg_mode": "ftms", "ftp": 245.0,
                   "template_id": "hiit_4x4", "template_name": "4×4 分钟",
                   "params": {"warmup_min": 10, "reps": 4, "work_s": 240,
                              "work_pct": 95, "rest_s": 180, "rest_pct": 55,
                              "cooldown_min": 5}},
    },
    {
        "key": "2x20",
        "plan": lambda: build_plan("threshold_2x20", None, 250.0),
        "name": "2×20 阈值",
        "mode": ERG_FTMS,
        "early": False,
        "days_ago": 5, "hour": 18, "minute": 45, "seed": 33,
        "config": {"mode": "interval", "erg_mode": "ftms", "ftp": 250.0,
                   "template_id": "threshold_2x20", "template_name": "2×20 阈值",
                   "params": {"warmup_min": 12, "reps": 2, "work_s": 1200,
                              "work_pct": 98, "rest_s": 300, "rest_pct": 55,
                              "cooldown_min": 8}},
    },
    {
        "key": "30_15",
        "plan": lambda: build_plan("hiit_30_15", None, 240.0),
        "name": "30/15 间歇",
        "mode": ERG_FTMS,
        "early": False,
        "days_ago": 3, "hour": 20, "minute": 5, "seed": 44,
        "config": {"mode": "interval", "erg_mode": "ftms", "ftp": 240.0,
                   "template_id": "hiit_30_15", "template_name": "30/15 间歇",
                   "params": {"warmup_min": 10, "reps": 13, "work_s": 30,
                              "work_pct": 105, "rest_s": 15, "rest_pct": 50,
                              "cooldown_min": 5}},
    },
    {
        "key": "over_under",
        "plan": lambda: build_plan("over_under", None, 240.0),
        "name": "Over-Under 超阈值",
        "mode": ERG_RESISTANCE,      # 演示闭环阻力模式
        "early": False,
        "days_ago": 2, "hour": 7, "minute": 15, "seed": 55,
        "config": {"mode": "interval", "erg_mode": "resistance", "ftp": 240.0,
                   "template_id": "over_under", "template_name": "Over-Under 超阈值",
                   "params": {"warmup_min": 12, "sets": 3, "reps_per_set": 3,
                              "under_s": 120, "under_pct": 95, "over_s": 60,
                              "over_pct": 108, "set_rest_s": 300,
                              "set_rest_pct": 50, "cooldown_min": 8}},
    },
    {
        "key": "quit",
        "plan": lambda: _constant_plan(100, 45),
        "name": "恒定功率",
        "mode": ERG_FTMS,
        "early": True,               # 只骑了 18 分钟就停了
        "until": 18 * 60,
        "days_ago": 1, "hour": 21, "minute": 0, "seed": 66,
        "config": {"mode": "constant", "erg_mode": "ftms",
                   "target_power": 100.0, "duration_min": 45.0},
    },
]


SAMPLE_COURSE_NAME = "示例课程 · 周末耐力"


def _make_custom_course(store: CustomStore) -> Dict[str, Any]:
    """顺便放一门示例课程，好让那份自定义课程的"再来一次"能真正复现。

    按名字查重：重复运行这个脚本不该攒出一堆同名课程。
    """
    payload = {
        "name": SAMPLE_COURSE_NAME,
        "desc": "热身 → 两段耐力 → 一段阈值 → 冷身（示例数据，可以删）",
        "power_mode": "pct",
        "steps": [
            {"name": "热身", "kind": "warmup", "duration_s": 720, "pct": 58},
            {"name": "耐力", "kind": "work", "duration_s": 900, "pct": 72},
            {"name": "恢复", "kind": "recovery", "duration_s": 180, "pct": 52},
            {"name": "阈值", "kind": "work", "duration_s": 600, "pct": 96},
            {"name": "冷身", "kind": "cooldown", "duration_s": 420, "pct": 50},
        ],
    }
    existing = next((c for c in store.list() if c["name"] == SAMPLE_COURSE_NAME), None)
    if existing is not None:
        payload["id"] = existing["id"]          # 有同名课程就更新它，不再新建
    return store.upsert(payload, 248.0)


def _remove_sample_course(store: CustomStore) -> bool:
    existing = next((c for c in store.list() if c["name"] == SAMPLE_COURSE_NAME), None)
    if existing is None:
        return False
    return store.delete(existing["id"])


def main() -> int:
    parser = argparse.ArgumentParser(description="生成示例训练报告")
    parser.add_argument("--clear", action="store_true",
                        help="清空所有训练报告（包括真实训练产生的）")
    args = parser.parse_args()

    reports = ReportStore()
    custom = CustomStore()

    if args.clear:
        removed = reports.clear()
        course_removed = _remove_sample_course(custom)
        print("已清空 {} 份训练报告".format(removed))
        print("已删除示例课程" if course_removed else "（没有示例课程需要删除）")
        return 0

    # 先清掉上一次生成的示例报告，重复运行不该越攒越多
    # （只删带 sample 标记的，用户真实训练的报告一份都不动）
    stale = reports.remove_samples()
    if stale:
        print("已清理上次生成的 {} 份示例报告".format(stale))

    course = _make_custom_course(custom)
    print("示例课程：{}（id {}）".format(course["name"], course["id"]))

    # 加一份 FTP 测试报告：让报告列表里能看到测试结果界面
    samples = list(SAMPLES)
    samples.append({
        "key": "ftp_ramp",
        "plan": lambda: build_test_plan(
            "ramp", {"warmup_min": 5, "cooldown_min": 8, "start_w": 100,
                     "step_w": 20, "max_steps": 25, "step_s": 60}, 250.0),
        "name": "坡道测试",
        "mode": ERG_FTMS,
        "early": False,
        "ceiling_w": 332.0,          # 这位骑手大约在 420W 力竭
        "days_ago": 6, "hour": 7, "minute": 45, "seed": 88,
        "config": {"mode": "test", "erg_mode": "ftms", "ftp": 250.0,
                   "test_id": "ramp", "free_resistance": 90,
                   "test": {"test_id": "ramp", "result_kind": "ramp",
                            "multiplier": 0.75,
                            "source_label": "最好 1 分钟功率",
                            "self_paced": False}},
    })
    samples.append({
        "key": "custom",
        "plan": lambda: build_custom_steps(course, 248.0),
        "name": course["name"],
        "mode": ERG_FTMS,
        "early": False,
        "days_ago": 4, "hour": 9, "minute": 20, "seed": 77,
        "config": {"mode": "custom", "erg_mode": "ftms", "ftp": 248.0,
                   "custom_id": course["id"], "custom_name": course["name"],
                   "power_mode": "pct"},
    })

    now = time.time()
    saved = 0
    for item in samples:
        plan = item["plan"]()
        started = now - item["days_ago"] * DAY
        # 归到当天那个钟点
        day = time.localtime(started)
        started = time.mktime((
            day.tm_year, day.tm_mon, day.tm_mday,
            item["hour"], item["minute"], 0, 0, 0, -1))

        sim = _simulate(plan, seed=item["seed"],
                        until=item.get("until"),
                        ceiling_w=item.get("ceiling_w"),
                        resistance_mode=(item["mode"] == ERG_RESISTANCE))
        summary = _build_summary(
            sim, plan_name=item["name"], started_at=started,
            stopped_early=item["early"], mode=item["mode"],
            config=item["config"], trainer_name="摩刻 iBike")

        report = reports.save(summary, sample=True)
        if report is None:
            print("  保存失败：{}".format(item["name"]))
            continue
        saved += 1
        print("  {:<22} {:>6}  时长 {:>7}  均值 {:>6}  达标率 {}".format(
            item["name"],
            time.strftime("%m-%d %H:%M", time.localtime(started)),
            "{}:{:02d}".format(int(summary["actual_s"]) // 60,
                               int(summary["actual_s"]) % 60),
            "{}W".format(round(summary["avg_power"] or 0)),
            "{:.0f}%".format(summary["in_zone_pct"] or 0)))

    print()
    print("已生成 {} 份示例报告 → {}".format(saved, reports.dir))
    print("打开控制台（或点一下「训练报告」）即可看到。")
    print("不想要了可以执行： python3 tools/make_sample_reports.py --clear")
    return 0


if __name__ == "__main__":
    sys.exit(main())
