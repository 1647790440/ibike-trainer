#!/usr/bin/env python3
"""闭环控制器调参用的小工具：打印功率轨迹和波动幅度。

    python3 tools/tune_closed_loop.py [--seconds 90] [--target 100] [--beta 0.55]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibike.session import ERG_RESISTANCE, WorkoutSession  # noqa: E402
from ibike.simulator import SimulatedTrainer  # noqa: E402


async def main(args) -> int:
    trainer = SimulatedTrainer(responds_to_target_power=False,
                               advertise_power_target=False,
                               cadence=args.cadence)
    await trainer.connect()

    session = WorkoutSession(trainer)
    if args.beta is not None or args.interval is not None:
        # 通过替换类属性来试不同参数（只在调参时用）
        if args.interval is not None:
            WorkoutSession.RESISTANCE_INTERVAL_S = args.interval
    await session.start(args.target, duration_min=args.seconds / 60.0,
                        erg_mode=ERG_RESISTANCE)
    if args.beta is not None:
        session.controller.beta = args.beta
    if args.deadband is not None:
        session.controller.deadband_w = args.deadband

    samples = []
    started = time.time()
    while time.time() - started < args.seconds:
        await asyncio.sleep(0.5)
        snap = session.snapshot()
        if snap["power"] is None:
            continue
        t = time.time() - started
        samples.append((t, snap["power"], snap["resistance_raw"]))
        bar = "#" * max(0, int((snap["power"] - args.target * 0.4) / 2))
        print("  t={:5.1f}s P={:5.1f}W R={:5.1f} {}".format(
            t, snap["power"], (snap["resistance_raw"] or 0) / 10.0, bar))

    await session.aclose()
    await trainer.disconnect()

    tail = [p for t, p, _ in samples if t >= args.seconds - args.tail]
    if tail:
        avg = sum(tail) / len(tail)
        lo, hi = min(tail), max(tail)
        dev = sum(abs(p - avg) for p in tail) / len(tail)
        print("\n稳定段（最后 {:.0f}s，{} 个样本）".format(args.tail, len(tail)))
        print("  均值 {:6.1f}W   区间 {:5.1f} ~ {:5.1f}W   峰峰值 {:5.1f}W   平均绝对偏差 {:.1f}W"
              .format(avg, lo, hi, hi - lo, dev))
        print("  与目标偏差 {:+5.1f}W".format(avg - args.target))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--tail", type=float, default=30.0)
    ap.add_argument("--target", type=float, default=100.0)
    ap.add_argument("--cadence", type=float, default=85.0)
    ap.add_argument("--beta", type=float, default=None)
    ap.add_argument("--deadband", type=float, default=None)
    ap.add_argument("--interval", type=float, default=None)
    sys.exit(asyncio.run(main(ap.parse_args())))
