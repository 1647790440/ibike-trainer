#!/usr/bin/env python3
"""间歇训练（HIIT / 乳酸阈值）的测试。

    python3 tests/test_intervals.py
"""

from __future__ import annotations

import asyncio
import sys
import time
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

from ibike.session import ERG_AUTO, STATE_FINISHED, WorkoutSession  # noqa: E402
from ibike.server import Server  # noqa: E402
from ibike.simulator import SimulatedTrainer  # noqa: E402
from ibike.workouts import (TEMPLATES, build_plan, default_params,  # noqa: E402
                            plan_stats, zone_of)

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


def test_zone_classification() -> None:
    print("\n[1] 训练区间划分")
    cases = [(0.45, "Z1"), (0.60, "Z2"), (0.80, "Z3"), (0.98, "Z4"),
             (1.10, "Z5"), (1.50, "Z6"), (2.00, "Z6")]
    for pct, expected in cases:
        got = zone_of(pct)["code"]
        check(got == expected, "{:.0f}% FTP → {}".format(pct * 100, expected),
              "实际 {}".format(got))


def test_templates_build() -> None:
    print("\n[2] 六个内置方案都能正确生成")
    check(len(TEMPLATES) == 6, "内置 6 个方案", "{} 个".format(len(TEMPLATES)))
    for t in TEMPLATES:
        steps = build_plan(t["id"], None, 250.0)
        st = plan_stats(steps)
        ok = bool(steps) and all(s["duration_s"] > 0 and s["target_power"] > 0 for s in steps)
        check(ok, "{:<14} 步骤合法".format(t["name"]),
              "{} 段 / {:.0f} 分钟 / 峰值 {}W".format(
                  st["step_count"], st["total_s"] / 60, st["peak_power"]))
        # 第一段必须是热身，最后一段必须是冷身
        check(steps[0]["kind"] == "warmup", "{:<14} 以热身开始".format(t["name"]),
              steps[0]["name"])
        check(steps[-1]["kind"] == "cooldown", "{:<14} 以冷身结束".format(t["name"]),
              steps[-1]["name"])


def test_ftp_scaling() -> None:
    print("\n[3] 强度按 FTP 线性缩放（这是「科学」的关键）")
    a = build_plan("hiit_4x4", None, 200.0)
    b = build_plan("hiit_4x4", None, 300.0)
    # 除去收尾夹到 20W 下限的低强度段，其余应当严格按比例
    ratios = [round(y["target_power"] / x["target_power"], 2)
              for x, y in zip(a, b)
              if x["target_power"] > 30 and y["target_power"] > 30]
    check(ratios and all(abs(r - 1.5) < 0.05 for r in ratios),
          "FTP 从 200 提到 300，各段功率按 1.5 倍缩放",
          "比例样本 {}".format(ratios[:4]))

    # 高强度段的 %FTP 应当落在配方声明的位置
    hard = [s for s in a if s["kind"] == "work"]
    check(all(abs(s["pct_ftp"] - 0.95) < 1e-6 for s in hard),
          "4×4 高强度段确实是 95% FTP", "{}".format(hard[0]["pct_ftp"]))


def test_params_clamped() -> None:
    print("\n[4] 参数越界会被夹回合法范围")
    steps = build_plan("hiit_4x4", {"reps": 999, "work_pct": 500, "rest_s": -50}, 250.0)
    reps = len([s for s in steps if s["kind"] == "work"])
    check(1 <= reps <= 10, "离谱的组数被夹到上限内", "{} 组".format(reps))
    hard = [s for s in steps if s["kind"] == "work"][0]
    check(hard["pct_ftp"] <= 1.20 + 1e-6, "离谱的强度被夹到上限内",
          "{:.0f}% FTP".format(hard["pct_ftp"] * 100))

    try:
        build_plan("no_such_template", None, 250.0)
        check(False, "未知方案应当报错")
    except ValueError as exc:
        check("未知" in str(exc), "未知方案报错清晰", str(exc))


# ======================================================================
# 会话推进
# ======================================================================


def test_step_index_lookup() -> None:
    print("\n[5] 步骤定位（含边界）")
    session = WorkoutSession(SimulatedTrainer())
    session._step_bounds = [10.0, 20.0, 30.0]
    cases = [(0.0, 0), (9.9, 0), (10.0, 1), (19.9, 1), (20.0, 2),
             (29.9, 2), (30.0, 2), (999.0, 2)]
    for t, expected in cases:
        got = session._step_index_at(t)
        check(got == expected, "t={:.1f}s → 第 {} 段".format(t, expected + 1),
              "实际第 {} 段".format(got + 1))


def test_interval_settle_grace() -> None:
    print("\n[6] 分段统计会跳过目标切换后的过渡期")
    session = WorkoutSession(SimulatedTrainer())
    session.plan = [
        {"name": "高强度", "kind": "work", "duration_s": 60, "target_power": 200},
        {"name": "恢复", "kind": "recovery", "duration_s": 20, "target_power": 100},
    ]
    session._step_starts = [0.0, 60.0]
    session._step_bounds = [60.0, 80.0]
    session._work_position = {0: 1}

    trace = []
    for i in range(60):
        trace.append({"t": float(i), "dt": 1.0, "p": 200.0, "target": 200.0, "step": 0})
    # 恢复段：飞轮惯性导致前 5 秒还挂在高功率上，之后才落到 100W
    for i in range(20):
        trace.append({"t": 60.0 + i, "dt": 1.0,
                      "p": 200.0 if i < 5 else 100.0, "target": 100.0, "step": 1})

    rows = session._interval_breakdown(trace)
    check(len(rows) == 2, "两段各一行")
    work, rest = rows

    check(abs(work["avg_power"] - 200.0) < 0.1, "高强度段实际功率 200W",
          "{:.1f}W".format(work["avg_power"]))
    check(work["in_zone_pct"] == 100.0, "高强度段达标率 100%",
          "{:.0f}%".format(work["in_zone_pct"]))

    check(rest["settle_s"] == 5.0, "恢复段的过渡期是 5 秒", str(rest["settle_s"]))
    check(abs(rest["avg_power"] - 100.0) < 0.1,
          "过渡期被排除后，恢复段实际功率就是 100W",
          "{:.1f}W（若把过渡期算进去会是 125W）".format(rest["avg_power"]))
    check(rest["in_zone_pct"] is None,
          "恢复段不给达标率——恢复段的目标是降下来，飞轮几秒内降不到目标值，"
          "拿它算达标率只会得到一片 0%")

    # 过渡期不能超过该段时长的四分之一，否则极短的高强度段会被整个吃掉
    session.plan[0]["duration_s"] = 8
    rows2 = session._interval_breakdown(trace)
    check(rows2[0]["settle_s"] == 2.0, "极短段落的过渡期按 25% 收缩",
          str(rows2[0]["settle_s"]))


async def test_session_advances_plan() -> None:
    print("\n[6] 会话按时间自动推进各段")
    plan = [
        {"name": "热身", "kind": "warmup", "duration_s": 6.0, "target_power": 60},
        {"name": "高强度", "kind": "work", "duration_s": 6.0, "target_power": 150},
        {"name": "恢复", "kind": "recovery", "duration_s": 6.0, "target_power": 50},
        {"name": "高强度", "kind": "work", "duration_s": 6.0, "target_power": 150},
        {"name": "冷身", "kind": "cooldown", "duration_s": 6.0, "target_power": 50},
    ]
    trainer = SimulatedTrainer(responds_to_target_power=True, cadence=85.0)
    await trainer.connect()
    events: List[str] = []
    session = WorkoutSession(trainer, on_event=lambda k, m: events.append(k))

    await session.start(plan=plan, plan_name="测试间歇", erg_mode=ERG_AUTO)
    check(session.duration_s == 30.0, "总时长由计划累加得出", "{:.0f}s".format(session.duration_s))
    check(session.step_index == 0, "从第一段开始")
    check(session.target_power == 60, "起始目标功率取第一段", str(session.target_power))

    seen: List[tuple] = []
    started = time.time()
    while time.time() - started < 33:
        await asyncio.sleep(0.4)
        snap = session.snapshot()
        seen.append((snap["step_index"], snap["step_name"], snap["target_power"]))
        if snap["state"] == STATE_FINISHED:
            break

    indices = [i for i, _, _ in seen]
    check(max(indices) == 4, "推进到了最后一段", "到过第 {} 段".format(max(indices) + 1))
    check(indices == sorted(indices), "步骤只前进不后退")

    # 检查每段的目标功率确实跟着变
    by_step: Dict[int, set] = {}
    for idx, _, power in seen:
        by_step.setdefault(idx, set()).add(power)
    check(by_step.get(1) == {150}, "第 2 段目标功率是 150W", str(by_step.get(1)))
    check(by_step.get(2) == {50}, "第 3 段目标功率是 50W", str(by_step.get(2)))
    check(events.count("interval") >= 4, "每次换段都发了通知",
          "{} 次".format(events.count("interval")))

    snap = session.snapshot()
    check(snap["is_interval"] is True, "标记为间歇训练")
    check(len(snap["plan"]) == 5, "快照里带着完整计划")
    check(snap["work_count"] == 2, "识别出 2 个高强度段", str(snap["work_count"]))
    check(snap["state"] == STATE_FINISHED, "跑满计划后自动结束", str(snap["state"]))

    summary = snap["summary"] or {}
    rows = summary.get("intervals") or []
    check(len(rows) == 5, "总结里每一段都有一行", "{} 行".format(len(rows)))
    check(rows[1]["name"] == "高强度" and rows[1]["target_power"] == 150,
          "分段的名称与目标正确",
          "{} {}W".format(rows[1]["name"], rows[1]["target_power"]))
    check(rows[1]["work_position"] == 1, "标注了这是第 1 个高强度段")
    check(summary.get("plan_name") == "测试间歇", "总结里记录了方案名")

    await session.aclose()
    await trainer.disconnect()


async def test_skip_step() -> None:
    print("\n[7] 跳过某一段 / 回退一段")
    plan = [{"name": "第{}段".format(i + 1), "kind": "work", "duration_s": 30.0,
             "target_power": 100 + i * 20} for i in range(4)]
    trainer = SimulatedTrainer(cadence=85.0)
    await trainer.connect()
    session = WorkoutSession(trainer)
    await session.start(plan=plan, plan_name="跳过测试", erg_mode=ERG_AUTO)

    await asyncio.sleep(1.0)
    check(session.step_index == 0, "起始在第 1 段")

    await session.skip_step(1)
    check(session.step_index == 1, "向后跳一段", "第 {} 段".format(session.step_index + 1))
    check(session.target_power == 120, "目标功率跟着切到该段", str(session.target_power))
    check(abs(session.elapsed_s - 30.0) < 1.0, "已用时间对齐到该段起点",
          "{:.1f}s".format(session.elapsed_s))

    await session.skip_step(1)
    await session.skip_step(1)
    check(session.step_index == 3, "连续跳到第 4 段")

    await session.skip_step(1)
    check(session.step_index == 3, "最后一段再往后跳不会越界")

    await session.skip_step(-1)
    check(session.step_index == 2, "可以回退一段")

    await session.skip_step(-99)
    check(session.step_index == 0, "回退不会越界到负数")

    await session.aclose()
    await trainer.disconnect()


# ======================================================================
# HTTP
# ======================================================================


async def test_http_interval_flow() -> None:
    print("\n[8] HTTP：模板 / 预览 / 启动 / 跳过 / 总结")
    server = Server(use_simulator=True)
    app = server.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = "http://127.0.0.1:{}".format(runner.addresses[0][1])

    try:
        async with ClientSession() as http:
            async with http.get(base + "/api/templates") as r:
                data = await r.json()
            templates = data.get("templates") or []
            check(len(templates) == 6, "模板列表可获取", "{} 个".format(len(templates)))
            check(all("build" not in t for t in templates),
                  "模板里不含不可序列化的构建函数")
            check(all(t.get("params") for t in templates), "每个模板都带参数定义")

            # 预览
            async with http.post(base + "/api/plan",
                                 json={"template_id": "hiit_4x4", "ftp": 250}) as r:
                preview = await r.json()
            check(preview.get("ok"), "方案预览成功")
            check(len(preview.get("steps") or []) == 9, "预览返回 9 段",
                  "{} 段".format(len(preview.get("steps") or [])))
            stats = preview.get("stats") or {}
            check(abs(stats.get("total_s", 0) - 2400) < 1, "预览统计总时长正确",
                  "{:.0f}s".format(stats.get("total_s", 0)))
            check(stats.get("peak_power") == 238, "预览统计峰值功率正确",
                  str(stats.get("peak_power")))

            # FTP 越界
            async with http.post(base + "/api/start",
                                 json={"mode": "interval", "template_id": "hiit_4x4",
                                       "ftp": 5000}) as r:
                bad = await r.json()
            check(r.status == 400 and not bad.get("ok"), "拒绝离谱的 FTP",
                  str(bad.get("error")))

            # 未知模板
            async with http.post(base + "/api/start",
                                 json={"mode": "interval", "template_id": "nope",
                                       "ftp": 250}) as r:
                bad = await r.json()
            check(r.status == 400, "拒绝未知模板", str(bad.get("error")))

            # 用最短的合法参数真正跑一次
            # 高强度段要够长，才可能产生"有效样本"：分段统计会跳过每段开头的
            # 过渡期（min(5 秒, 段的 25%)），段太短的话 valid_s 一直是 0，
            # 高强度段达标率就永远是 None——那样这条断言等于没测。
            short = {"warmup_min": 0, "cooldown_min": 0, "reps": 4,
                     "work_s": 20, "rest_s": 10}
            async with http.post(base + "/api/start",
                                 json={"mode": "interval", "template_id": "hiit_30_15",
                                       "ftp": 250, "params": short,
                                       "erg_mode": "auto"}) as r:
                started = await r.json()
            check(started.get("ok"), "间歇训练启动成功", str(started.get("error")))

            async with http.get(base + "/api/state") as r:
                st = await r.json()
            check(st.get("state") == "running", "状态为 running", str(st.get("state")))
            check(st.get("is_interval") is True, "标记为间歇训练")
            check(st.get("step_name") == "高强度", "当前段是高强度", str(st.get("step_name")))
            check(st.get("step_remaining_s", 0) > 0, "有本段剩余时间",
                  "{:.1f}s".format(st.get("step_remaining_s", 0)))
            check(st.get("work_index") == 1 and st.get("work_count") == 4,
                  "组次显示为第 1/4 组",
                  "{}/{}".format(st.get("work_index"), st.get("work_count")))
            check(st.get("next_step_name") is not None, "给出了下一段",
                  str(st.get("next_step_name")))

            # 跳过当前段
            before = st.get("step_index")
            async with http.post(base + "/api/interval/skip", json={"delta": 1}) as r:
                await r.json()
            async with http.get(base + "/api/state") as r:
                st2 = await r.json()
            check(st2.get("step_index") == before + 1, "跳过一段生效",
                  "第 {} 段 → 第 {} 段".format(before + 1, st2.get("step_index") + 1))

            # 睡够过渡期 + 若干秒有效采样，达标率才有东西可算
            await asyncio.sleep(9.0)
            async with http.post(base + "/api/stop", json={}) as r:
                stopped = await r.json()
            summary = stopped.get("summary") or {}
            check(summary.get("is_interval") is True, "总结标记为间歇训练")
            rows = summary.get("intervals") or []
            check(len(rows) >= 1, "总结里带了分段明细",
                  "{} 行".format(len(rows)))
            # 分段明细只包含真正踩过的段——跳过的那段没有数据，不该出现在里面。
            # 达标率只对高强度段给出；恢复/热身/冷身留空。
            check(all((r.get("in_zone_pct") is None) for r in rows if r["kind"] != "work"),
                  "非高强度段不给达标率")
            # 这个场景里高强度段正好被跳过、剩下的数据都在过渡期里，所以
            # 达标率**本来就应该**是 None（没有有效样本时不硬凑一个数）。
            # 达标率本身另有一段专门的手搓轨迹验证，见 test_work_in_zone_pct()。
            check(summary.get("work_in_zone_pct") is None,
                  "高强度段没有有效样本时不硬凑达标率",
                  str(summary.get("work_in_zone_pct")))
            check(all("settle_s" in r for r in rows), "每一行都标明了过渡期长度")
            # 跳过的 10 秒不能被算成骑过的时间
            check(summary.get("skipped_s", 0) > 5,
                  "记录了跳过的时间", "{:.1f}s".format(summary.get("skipped_s", 0)))
            check(summary.get("actual_s", 999) < 15,
                  "实际时长只算真正骑行的时间（不含跳过）",
                  "{:.1f}s".format(summary.get("actual_s", 0)))

            # 「再来一次」：配置存在服务端，能原样复现同一个方案和参数
            async with http.post(base + "/api/repeat", json={}) as r:
                again = await r.json()
            check(again.get("ok"), "结束后「再来一次」可用", str(again.get("error")))
            async with http.get(base + "/api/state") as r:
                st3 = await r.json()
            check(st3.get("state") == "running", "重复训练已开始", str(st3.get("state")))
            check(st3.get("plan_name") == "30/15 间歇", "重复的是同一个方案",
                  str(st3.get("plan_name")))
            check(st3.get("step_count") == 7, "参数也原样复现（4 组而不是默认 13 组）",
                  "{} 段".format(st3.get("step_count")))
            check(st3.get("summary") is None, "重新开始后旧总结已清掉")
            await http.post(base + "/api/stop", json={})

    finally:
        await runner.cleanup()


async def test_work_in_zone_pct() -> None:
    """高强度段达标率必须真的是个数，而不是恒为 None。

    之前这条只有 `"work_in_zone_pct" in summary` 这样的断言——把实现改成永远
    返回 None 也照样通过，而"高强度段踩得准不准"恰恰是间歇训练最该看的数字。
    """
    print("\n[7] 高强度段达标率")
    from ibike.session import WorkoutSession
    from ibike.simulator import SimulatedTrainer

    plan = [
        {"name": "热身", "kind": "warmup", "duration_s": 60.0, "target_power": 100},
        {"name": "高强度", "kind": "work", "duration_s": 60.0, "target_power": 200},
        {"name": "恢复", "kind": "recovery", "duration_s": 60.0, "target_power": 100},
    ]
    trainer = SimulatedTrainer()
    await trainer.connect()
    session = WorkoutSession(trainer)
    await session.start(200.0, duration_min=3, erg_mode="ftms", plan=plan,
                        plan_name="测试课", config={"mode": "interval"})
    await session.aclose()

    # 手搓轨迹：高强度段每一秒都在目标 ±5% 内
    trace = []
    t = 0.0
    for idx, step in enumerate(plan):
        for _ in range(int(step["duration_s"])):
            target = float(step["target_power"])
            power = target if step["kind"] == "work" else target * 0.6
            trace.append({"t": t, "dt": 1.0, "p": power, "target": target, "step": idx})
            t += 1.0
    session._trace = trace
    session.active_s = t
    session._sampled_s = t

    summary = session._build_summary("测试")
    wiz = summary.get("work_in_zone_pct")
    check(isinstance(wiz, (int, float)) and abs(wiz - 100.0) < 0.01,
          "高强度段全程踩在目标上时达标率是 100%", str(wiz))
    rows = {r["kind"]: r for r in summary["intervals"]}
    check(rows.get("work", {}).get("in_zone_pct") == wiz,
          "总达标率和分段里的高强度段一致",
          "{} vs {}".format(rows.get("work", {}).get("in_zone_pct"), wiz))
    check(rows.get("recovery", {}).get("in_zone_pct") is None,
          "恢复段不给达标率")
    await trainer.disconnect()


async def main() -> int:
    print("=" * 70)
    print("间歇训练（HIIT / 乳酸阈值）测试")
    print("=" * 70)
    test_zone_classification()
    test_templates_build()
    test_ftp_scaling()
    test_params_clamped()
    test_step_index_lookup()
    test_interval_settle_grace()
    await test_session_advances_plan()
    await test_skip_step()
    await test_http_interval_flow()
    await test_work_in_zone_pct()

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
