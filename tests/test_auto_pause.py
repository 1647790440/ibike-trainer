#!/usr/bin/env python3
"""自动暂停 / 自动继续的测试。

背景（真人真事）：135W 骑到第 42 分钟来了个电话，骑手停下来接电话，没有按暂停。
那 2 分钟被算成了骑行时长，报告里的平均功率从 135W 摊成 130W、达标率掉了一大截。
所以现在踩停超过 15 秒就自动停表，重新踩起来自动继续。

这里盯住的关键行为：

- 功率和踏频**都**归零才算"没在骑"（只看功率会把自由骑行里的轻踩误判成停）
- 停表期间骑行台**不动**（软暂停）：一旦下发暂停指令，固件可能放开阻力甚至
  停推数据，就再也检测不到"骑手回来了"
- 自动继续需要踏频连续达标几秒（碰一下曲柄不该开表）
- **手动**暂停、骑行台掉线挂起都不自动继续——那两种情况下用户就是要它停着
- 坡道测试不受影响：踩不动了仍然结束成"力竭"，不会被自动暂停抢先

    python3 tests/test_auto_pause.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# 测试隔离：绝不能碰用户的真实数据目录（报告是训练一结束就自动保存的）
# ---------------------------------------------------------------------------
import os as _os
import tempfile as _tempfile

_os.environ.setdefault("IBIKE_DATA_DIR",
                       _tempfile.mkdtemp(prefix="ibike-test-data-"))

from ibike.ftptest import build_test_plan  # noqa: E402
from ibike.session import (STATE_FINISHED, STATE_PAUSED, STATE_RUNNING,  # noqa: E402
                           WorkoutSession)
from ibike.trainer import TrainerError  # noqa: E402

_failures: List[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print("  {} {}{}".format("\033[32m✔\033[0m" if cond else "\033[31m✘\033[0m",
                             label, "  → " + detail if detail else ""))
    if not cond:
        _failures.append(label)
    return cond


class HandTrainer:
    """手动喂数据的骑行台替身：功率和踏频想设多少设多少。

    自动暂停的判据就是这两列，所以需要一个"精确"的台子；模拟器那台带慢漂移和
    抖动（base_cadence=0 时踏频还能漂到 6rpm），测阈值会不稳。
    """

    def __init__(self, on_event=None) -> None:
        self.on_event = on_event
        self.connected = False
        self.is_simulator = False
        self.name = "手搓骑行台"
        self.latest: Dict[str, Any] = {}
        self.last_data_time = 0.0
        self.has_control = False
        self.capabilities = {
            "has_control_point": True,
            "supports_power_target": True,
            "supports_resistance_target": True,
            "resistance_range": {"min_raw": 0, "max_raw": 255},
        }
        self.commands: List[Any] = []
        self.power = 0.0
        self.cadence = 0.0

    async def connect(self, address: str = "", timeout: float = 0.0) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

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

    # -- 测试用 ---------------------------------------------------------

    def ride(self, power: float = 135.0, cadence: float = 80.0) -> None:
        self.power, self.cadence = float(power), float(cadence)
        self._push()

    def coast(self) -> None:
        """完全停踩：功率和踏频都是 0。"""
        self.power, self.cadence = 0.0, 0.0
        self._push()

    def _push(self) -> None:
        self.latest = {"power_w": self.power, "cadence_rpm": self.cadence,
                       "speed_kmh": 20.0 if self.cadence else 0.0}
        self.last_data_time = time.monotonic()


async def _start(session: WorkoutSession, trainer: HandTrainer,
                 minutes: float = 10.0) -> List[str]:
    """开一场训练，并在整个测试期间持续喂当前这组数据。"""
    events: List[str] = []
    session.on_event = lambda k, m: events.append(k)
    await trainer.connect()
    await session.start(135.0, duration_min=minutes, erg_mode="ftms")
    return events


async def _feed(trainer: HandTrainer, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        trainer._push()
        await asyncio.sleep(0.05)


def _compress(session: WorkoutSession) -> None:
    """把时间轴压到秒级，不必真等 15 秒。"""
    session.AUTO_PAUSE_HOLD_S = 0.6
    session.AUTO_RESUME_HOLD_S = 0.3
    session.AUTO_RESUME_RETRY_S = 1.0


async def test_pause_when_stopped() -> None:
    print("\n[1] 功率和踏频都归零超过阈值 → 自动暂停，秒表冻住")
    trainer = HandTrainer()
    session = WorkoutSession(trainer)
    _compress(session)
    try:
        events = await _start(session, trainer)
        trainer.ride()
        await _feed(trainer, 0.8)
        elapsed = session.elapsed_s
        sampled = session._sampled_s
        check(session.state == STATE_RUNNING, "开始骑行后状态是 running", session.state)

        # 接电话：停踩
        trainer.coast()
        await _feed(trainer, 0.6 + 0.5)

        check(session.state == STATE_PAUSED, "停踩后自动暂停了", session.state)
        check(session.auto_paused is True, "标记为「自动暂停」（只有它能自动继续）")
        check("auto-pause" in events, "发了自动暂停的事件", str(events))
        check("pause" not in trainer.commands,
              "**没有**给骑行台下发暂停指令（软暂停：停了就收不到踏频了）",
              str(trainer.commands))
        check(abs(session.elapsed_s - elapsed) < 0.4,
              "停表期间计时不再增长", "{:.2f} → {:.2f}".format(elapsed, session.elapsed_s))
        # 这一条才是这个功能的要害：停止累计必须**当场**生效，不能等满 15 秒
        # （否则每次自动暂停都会给报告留下十几秒 0W，平均功率照样被摊薄）
        check(abs(session._sampled_s - sampled) < 0.4,
              "平均功率的分母没有把停踩那段算进去",
              "{:.2f} → {:.2f}".format(sampled, session._sampled_s))
        check(all(x["p"] > 0 for x in session._trace),
              "轨迹里没有 0W 的采样点",
              str([x["p"] for x in session._trace][-4:]))
        check(session.snapshot()["auto_paused"] is True,
              "快照里带 auto_paused（前端据此写提示）")
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_resume_when_pedaling() -> None:
    print("\n[2] 重新踩起来 → 自动继续（并重新下发目标功率）")
    trainer = HandTrainer()
    session = WorkoutSession(trainer)
    _compress(session)
    try:
        events = await _start(session, trainer)
        trainer.ride()
        await _feed(trainer, 0.8)
        trainer.coast()
        await _feed(trainer, 1.1)
        check(session.state == STATE_PAUSED, "先进入自动暂停", session.state)

        # 回来了，踩起来
        trainer.commands.clear()
        trainer.ride()
        await _feed(trainer, 0.3 + 0.5)

        check(session.state == STATE_RUNNING, "踩起来之后自动继续了", session.state)
        check(session.auto_paused is False, "自动暂停标志已清掉")
        check(any(c[0] == "power" for c in trainer.commands),
              "重新下发了目标功率（新接管要重发）", str(trainer.commands[:4]))
        check("resumed" in events, "发了继续的事件", str(events))

        e0 = session.elapsed_s
        await _feed(trainer, 0.6)
        check(session.elapsed_s > e0, "计时接着走",
              "{:.2f} → {:.2f}".format(e0, session.elapsed_s))
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_short_blip_does_not_resume() -> None:
    print("\n[3] 碰一下曲柄不该开表（踏频达标但不够久）")
    trainer = HandTrainer()
    session = WorkoutSession(trainer)
    _compress(session)
    try:
        await _start(session, trainer)
        trainer.ride()
        await _feed(trainer, 0.8)
        trainer.coast()
        await _feed(trainer, 1.1)
        check(session.state == STATE_PAUSED, "先在自动暂停状态", session.state)

        # 只踩了一下（不足 AUTO_RESUME_HOLD_S）
        trainer.ride()
        await _feed(trainer, 0.15)
        check(session.state == STATE_PAUSED,
              "踩不到 {} 秒不会自动继续".format(session.AUTO_RESUME_HOLD_S),
              session.state)
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_manual_pause_never_auto_resumes() -> None:
    print("\n[4] 手动按下的暂停：踩起来也不许自动继续")
    trainer = HandTrainer()
    session = WorkoutSession(trainer)
    _compress(session)
    try:
        await _start(session, trainer)
        trainer.ride()
        await _feed(trainer, 0.8)

        await session.pause()           # 用户自己按的
        check(session.auto_paused is False, "手动暂停不置「自动暂停」标志")
        check(("pause",) in trainer.commands, "手动暂停要真的让骑行台松开",
              str(trainer.commands[-2:]))

        trainer.ride()
        await _feed(trainer, 1.0)
        check(session.state == STATE_PAUSED,
              "踩起来也不会自动继续（要用户自己点「继续」）", session.state)
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_trainer_lost_never_auto_resumes() -> None:
    print("\n[5] 骑行台掉线挂起：踩起来也不许自动继续（台子都没了）")
    trainer = HandTrainer()
    session = WorkoutSession(trainer)
    _compress(session)
    try:
        await _start(session, trainer)
        trainer.ride()
        await _feed(trainer, 0.8)

        session.handle_trainer_lost("与骑行台的连接断开了")
        check(session.state == STATE_PAUSED, "掉线后挂起", session.state)
        check(session.auto_paused is False, "挂起不算自动暂停")
        check(session.trainer_lost is True, "「掉线」标志置上")

        trainer.ride()
        await _feed(trainer, 1.0)
        check(session.state == STATE_PAUSED, "踩起来也不会自动继续", session.state)
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_ramp_test_still_ends_on_exhaustion() -> None:
    print("\n[6] 坡道测试踩不动了：仍然结束成「力竭」，不被自动暂停抢先")
    from ibike.simulator import SimulatedTrainer

    # 自动暂停压到 0.6 秒（远快于坡道的力竭判据）：如果护栏没生效，测试会在
    # 力竭之前就被自动暂停停住，然后永远等不到结论
    trainer = SimulatedTrainer(responds_to_target_power=True, cadence=80.0)
    await trainer.connect()
    session = WorkoutSession(trainer)
    _compress(session)
    plan = build_test_plan("ramp", {"warmup_min": 0, "cooldown_min": 0,
                                    "start_w": 100, "step_w": 20,
                                    "max_steps": 12, "step_s": 60}, 200.0)
    try:
        await session.start(plan=plan, plan_name="坡道测试", erg_mode="ftms",
                            config={"mode": "test", "erg_mode": "ftms", "ftp": 200.0,
                                    "test": {"test_id": "ramp", "result_kind": "ramp",
                                             "multiplier": 0.75, "self_paced": False}})
        check(session._is_ramp_test, "会话知道自己在跑坡道测试")
        # 骑手踩不动了：踏频掉到 0
        trainer.base_cadence = 0.0
        for _ in range(60):
            await asyncio.sleep(0.5)
            if session.state == STATE_FINISHED:
                break
        summary = session.snapshot().get("summary") or {}
        check(session.state == STATE_FINISHED, "测试结束了（不是被自动暂停卡住）",
              session.state)
        check("力竭" in str(summary.get("reason")), "结束原因是力竭",
              str(summary.get("reason")))
        check(session.auto_paused is False, "全程没被自动暂停接管")
    finally:
        await session.aclose()
        await trainer.disconnect()


async def test_zero_power_but_pedaling_is_not_stopped() -> None:
    print("\n[7] 只有功率为 0（但还在踩）：不算停下来")
    trainer = HandTrainer()
    session = WorkoutSession(trainer)
    _compress(session)
    try:
        await _start(session, trainer)
        # 自由骑行里"在踩但只有几瓦"是常态：只看功率会误判成停踩
        trainer.ride(power=0.0, cadence=70.0)
        await _feed(trainer, 1.2)
        check(session.state == STATE_RUNNING,
              "有踏频就不自动暂停（用「与」而不是「或」的理由）", session.state)
        check(session.auto_paused is False, "没有进入自动暂停")
    finally:
        await session.aclose()
        await trainer.disconnect()


async def main() -> int:
    print("=" * 70)
    print("自动暂停 / 自动继续测试")
    print("=" * 70)
    await test_pause_when_stopped()
    await test_resume_when_pedaling()
    await test_short_blip_does_not_resume()
    await test_manual_pause_never_auto_resumes()
    await test_trainer_lost_never_auto_resumes()
    await test_ramp_test_still_ends_on_exhaustion()
    await test_zero_power_but_pedaling_is_not_stopped()

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
