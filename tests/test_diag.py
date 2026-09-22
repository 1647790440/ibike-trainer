#!/usr/bin/env python3
"""原生 ERG 诊断脚本的测试。

诊断的价值全在"判读"那一步：数据是对的、结论是错的，比没有诊断更糟。
所以这里既测判读规则的每一条分支（用构造出来的采样序列，快且确定），
也真的对着模拟骑行台跑一遍完整流程（确保阶段编排本身能跑通）。

    python3 tests/test_diag.py
"""

from __future__ import annotations

import asyncio
import os as _os
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# 测试隔离：绝不能碰用户的真实数据目录
# ---------------------------------------------------------------------------
_os.environ.setdefault("IBIKE_DATA_DIR",
                       tempfile.mkdtemp(prefix="ibike-test-data-"))

from ibike.diag import Recorder, analyze, run  # noqa: E402
from ibike.simulator import SimulatedTrainer  # noqa: E402

_failures: List[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print("  {} {}{}".format("\033[32m✔\033[0m" if cond else "\033[31m✘\033[0m",
                             label, "  → " + detail if detail else ""))
    if not cond:
        _failures.append(label)
    return cond


def fabricate(powers: List[float], resistances: List[Optional[float]],
              step: float = 1.0) -> Recorder:
    """直接塞进去一段采样序列，绕开真实硬件。"""
    rec = Recorder()
    t0 = time.time() - len(powers) * step
    for i, p in enumerate(powers):
        res = resistances[i] if i < len(resistances) else None
        rec.samples.append((t0 + i * step, p, 85.0, res))
    return rec


def verdict(target: float, a: Recorder, b: Recorder, up: Recorder, down: Recorder) -> str:
    return "\n".join(analyze(target, a, b, up, down, 20.0))


# ======================================================================
# 1. 判读规则
# ======================================================================


def test_resistance_never_moves() -> None:
    """阻力一动不动 + 功率一直偏 → 固件的 ERG 是开环的。"""
    print("\n[1] 阻力档位纹丝不动 → 判成“开环查表”")
    flat = [114.0] * 30
    no_res = [17.5] * 30
    text = verdict(130.0, fabricate(flat, no_res), fabricate(flat, no_res),
                   fabricate(flat, no_res), fabricate(flat, no_res))
    check("不是闭环" in text, "判成固件根本没做功率闭环", text.strip().splitlines()[-1])
    check("闭环阻力" in text, "给出了改用闭环阻力的建议")


def test_resistance_moves_but_power_off() -> None:
    """阻力在动、功率却稳在偏差处 → 固件有闭环，但和上报值不是一个量。"""
    print("\n[2] 阻力在动但功率收敛不到 → 判成“刻度和上报对不上”")
    powers = [120.0] * 30
    moving = [15.0 + i * 0.5 for i in range(30)]      # 阻力一直在抬，但功率纹丝不动
    text = verdict(160.0, fabricate(powers, moving), fabricate(powers, moving),
                   fabricate(powers, moving), fabricate(powers, moving))
    check("刻度" in text or "死区" in text, "判成固件自己的闭环有偏差")
    check("闭环阻力" in text, "建议改用闭环阻力")


def test_keepalive_breaks_integrator() -> None:
    """只发一次的收敛明显更好 → 是每 2 秒重发打断了固件积分。"""
    print("\n[3] 只下发一次明显更好 → 判成“重发打断了积分器”")
    # 两段的阻力都要在动（否则会先命中"阻力一动不动"那条判据），
    # 区别只在重发与否：A 停在 118W，B 收敛到 100W
    a = fabricate([118.0] * 30, [15.0 + i * 0.2 for i in range(30)])
    b = fabricate([100.5] * 30, [15.0 + i * 0.05 for i in range(30)])
    text = verdict(100.0, a, b, b, b)
    check("重发" in text and "打断" in text, "指出重发是元凶")
    check("KEEPALIVE" in text or "重发间隔" in text, "给出了放宽重发间隔的建议")


def test_healthy_erg() -> None:
    """功率压得住 → 不要冤枉固件。"""
    print("\n[4] 功率压得住 → 判成“原生 ERG 正常”")
    # A 段和 B 段必须给**不同**的数据。以前两边用同一个 Recorder，"B 比 A 更稳"
    # 这种情形根本构造不出来，而"只看波动、不看偏差"的判据恰恰会因此把一台健康
    # 设备误判成"重发打断了积分器"（随机数据里这种误判占了一多半）。
    a = fabricate([130.0, 129.0, 131.0] * 10, [17.8] * 30)
    b = fabricate([130.0] * 30, [17.8] * 30)          # B 恰好比 A 稳
    text = verdict(130.0, a, b, a, b)
    check("原生 ERG 是好的" in text, "没有冤枉固件", text.strip().splitlines()[-1])
    check("打断" not in text, "不会仅仅因为 B 更稳就判成「重发打断了积分器」")


def test_missing_resistance_column() -> None:
    """骑行台不回报阻力档位时要主动说明，而不是默默按 0 处理。"""
    print("\n[5] 骑行台不回报阻力档位 → 明确说明只能靠功率推断")
    powers = [114.0] * 30
    text = verdict(130.0, fabricate(powers, [None] * 30), fabricate(powers, [None] * 30),
                   fabricate(powers, [None] * 30), fabricate(powers, [None] * 30))
    check("没有" in text and "回报阻力档位" in text, "指出缺了这一列")


# ======================================================================
# 2. 端到端：真的跑一遍四个阶段
# ======================================================================


async def run_diag(trainer: SimulatedTrainer, target: float,
                   seconds: float) -> List[str]:
    """跑一遍完整诊断并把输出捕获下来（阶段压到几秒，测试才跑得动）。"""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        await run(trainer, target=target, seconds=seconds, quick=True)
    return buf.getvalue().splitlines()


async def test_end_to_end_dumb() -> None:
    """对着"装死"的模拟台跑完整流程，结论必须是固件没在做闭环。"""
    print("\n[6] 端到端：装死的模拟台应被判成“不是闭环”")
    trainer = SimulatedTrainer(responds_to_target_power=False,
                               advertise_power_target=True, cadence=85.0)
    await trainer.connect()
    # 直接把它放进稳态（阻力 40 → 约 85W），否则短阶段里还在爬坡，
    # 判读会被"功率波动大"这条限定挡住
    trainer.power = 0.025 * 40.0 * 85.0
    try:
        lines = await run_diag(trainer, 100.0, 8.0)
    finally:
        await trainer.disconnect()
    text = "\n".join(lines)
    check("判读" in text, "确实输出了判读", "{} 行输出".format(len(lines)))
    check("不是闭环" in text, "判成固件没做功率闭环",
          next((l.strip() for l in lines if "结论" in l), "无"))
    check("闭环阻力" in text, "建议改用闭环阻力")


async def test_end_to_end_good() -> None:
    """对着正常的模拟台跑完整流程，结论必须是原生 ERG 可用。"""
    print("\n[7] 端到端：正常的模拟台应被判成“原生 ERG 是好的”")
    trainer = SimulatedTrainer(responds_to_target_power=True,
                               advertise_power_target=True, cadence=85.0)
    await trainer.connect()
    # 直接放进"已经整定到 100W"的稳态，10 秒的阶段才有意义
    trainer.erg_raw = 100.0 / (0.025 * 85.0)
    trainer.power = 100.0
    try:
        lines = await run_diag(trainer, 100.0, 8.0)
    finally:
        await trainer.disconnect()
    text = "\n".join(lines)
    check("原生 ERG 是好的" in text, "没有冤枉正常的固件",
          next((l.strip() for l in lines if "结论" in l), "无"))


async def main() -> int:
    print("=" * 70)
    print("原生 ERG 诊断脚本测试")
    print("=" * 70)
    test_resistance_never_moves()
    test_resistance_moves_but_power_off()
    test_keepalive_breaks_integrator()
    test_healthy_erg()
    test_missing_resistance_column()
    await test_end_to_end_dumb()
    await test_end_to_end_good()

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
