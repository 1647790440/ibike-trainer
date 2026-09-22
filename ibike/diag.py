"""原生 ERG 诊断：回答"我设了 130W，骑行台为什么没把功率拉到 130W"。

背景（真机复盘）：一次 60 分钟 130W 的骑行里，功率在 t≈912~930 秒整整一段
**每一个采样点都精确等于 114W**，而目标是 130W。人的腿不可能这么稳，这说明
那几秒里骑行台报的功率是它自己用「阻力 × 踏频」算出来的固定值——也就是说
**它的阻力档位没动**，所以功率也没动。

但"阻力没动"到底是因为：
  A. 固件把 Set Target Power 实现成开环查表（换算成一次阻力就完事）；
  B. 固件有自己的闭环，只是死区太大 / 太慢；
  C. 固件闭环是好的，只是它上报的功率和它内部伺服的量有刻度偏差；
  D. 程序每 2 秒重发一次目标功率，把固件自己的积分器打断了；
从一小时的功率曲线是分不出来的——缺的那一列是**骑行台回报的阻力档位**。

所以这个脚本把它补上：一边下发指令，一边把 功率 / 踏频 / 骑行台回报的阻力档位
逐秒打出来，并跑三个对照阶段：

  A 原生 ERG + 每 2 秒重发（程序现在的做法）
  B 原生 ERG + 只在一开始发一次（看重发会不会打断固件）
  C 目标功率阶跃 +40W 再 -40W（看固件对"设定值"到底有没有伺服能力）

    python3 main.py --diag                 # 连接后跑完整诊断（约 4 分钟）
    python3 main.py --diag --sim           # 用模拟骑行台先看一眼输出长什么样
    python3 main.py --diag --address UUID  # 直接连指定设备
    python3 main.py --diag --quick         # 每个阶段短一点（约 2 分钟）

判读规则写在 analyze() 里，跑完会直接给结论。
"""

from __future__ import annotations

import asyncio
import statistics
import time
from typing import Any, Dict, List, Optional, Tuple

# 一个采样点：t 相对阶段起点的秒数，以及当时骑行台报的三个量
Sample = Dict[str, Any]


def _fmt(v: Optional[float], width: int = 6, digits: int = 1) -> str:
    if v is None:
        return " " * (width - 2) + "--"
    return ("{:>" + str(width) + "." + str(digits) + "f}").format(v)


class Recorder:
    """按秒采样骑行台回报的功率 / 踏频 / 阻力档位。"""

    def __init__(self) -> None:
        self.samples: List[Tuple[float, Optional[float], Optional[float], Optional[float]]] = []
        self._last: Optional[float] = None
        self.events: List[Tuple[float, str]] = []

    def tick(self, trainer: Any, note: str = "") -> None:
        now = time.time()
        if self._last is not None and now - self._last < 1.0:
            return
        self._last = now
        latest = getattr(trainer, "latest", None) or {}
        self.samples.append((
            now,
            latest.get("power_w"),
            latest.get("cadence_rpm"),
            # 关键的一列：骑行台**自己回报**的阻力档位。
            # 原生 ERG 模式下程序一个阻力指令都没发过，所以这一列完全是固件的行为。
            latest.get("resistance_level"),
        ))
        if note:
            self.events.append((now, note))

    # -- 统计 -----------------------------------------------------------

    def tail(self, seconds: float) -> List[Tuple[float, Optional[float], Optional[float], Optional[float]]]:
        if not self.samples:
            return []
        end = self.samples[-1][0]
        return [s for s in self.samples if s[0] >= end - seconds]

    def power_stats(self, seconds: float) -> Dict[str, Any]:
        vals = [p for _, p, _, _ in self.tail(seconds) if p is not None]
        if not vals:
            return {"n": 0}
        return {
            "n": len(vals),
            "mean": statistics.mean(vals),
            "stdev": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
            "min": min(vals),
            "max": max(vals),
        }

    def resistance_motion(self, seconds: float) -> Optional[float]:
        """回报的阻力档位在这段时间里动了多少（极差，单位 0.1 档）。"""
        vals = [r for _, _, _, r in self.tail(seconds) if r is not None]
        if len(vals) < 2:
            return None
        return max(vals) - min(vals)


def print_table(title: str, samples: List[Tuple[float, Optional[float], Optional[float], Optional[float]]],
                every: int = 5) -> None:
    print("\n  " + title)
    print("    {:>6}  {:>7}  {:>7}  {:>9}".format("时刻", "功率W", "踏频", "回报阻力"))
    for i, (_, p, c, r) in enumerate(samples):
        if i % every and i != len(samples) - 1:
            continue
        print("    {:>6.0f}  {}  {}  {}".format(i, _fmt(p, 7, 0), _fmt(c, 7, 1), _fmt(r, 9, 1)))


async def _set_target(trainer: Any, watts: float) -> None:
    try:
        await trainer.set_target_power(int(round(watts)))
    except Exception as exc:  # noqa: BLE001 - 诊断脚本，出错要能看到而不是崩掉
        print("    ！下发 {}W 失败：{}".format(int(watts), exc))


async def phase_a(tester: Any, target: float, seconds: float) -> Recorder:
    """A：原生 ERG，每 2 秒重发一次目标功率——程序现在的做法。"""
    print("\n[A] 原生 ERG + 每 2 秒重发目标功率（当前程序的做法）")
    rec = Recorder()
    await _set_target(tester, target)
    started = time.time()
    last_keepalive = started
    while time.time() - started < seconds:
        await asyncio.sleep(0.2)
        # 完全复刻 WorkoutSession 的行为：POWER_KEEPALIVE_S = 2.0
        now = time.time()
        if now - last_keepalive >= 2.0:
            last_keepalive = now
            await _set_target(tester, target)
        rec.tick(tester)
    print_table("阶段 A（目标 {}W，全程重发）".format(int(target)), rec.tail(seconds))
    return rec


async def phase_b(tester: Any, target: float, seconds: float) -> Recorder:
    """B：原生 ERG，只在开头发一次，之后再不下发——看重发会不会打断固件。"""
    print("\n[B] 原生 ERG + 只下发一次（之后一条指令都不发）")
    rec = Recorder()
    # 先用一个明显不同的值把固件"踢"一下，否则它可能还保持着上一阶段的状态
    await _set_target(tester, target + 20)
    await asyncio.sleep(2.0)
    await _set_target(tester, target)
    started = time.time()
    while time.time() - started < seconds:
        await asyncio.sleep(0.2)
        rec.tick(tester)
    print_table("阶段 B（目标 {}W，只发一次）".format(int(target)), rec.tail(seconds))
    return rec


async def phase_c(tester: Any, target: float, seconds: float) -> Tuple[Recorder, Recorder]:
    """C：目标阶跃 ±40W，看固件对设定值到底有没有伺服能力。"""
    print("\n[C] 目标功率阶跃 {}W → {}W（每个台阶只看最后 {:.0f} 秒）".format(
        int(target), int(target + 40), seconds / 2))
    up = Recorder()
    await _set_target(tester, target + 40)
    started = time.time()
    while time.time() - started < seconds:
        await asyncio.sleep(0.2)
        up.tick(tester)
    print_table("  台阶 1：目标 {}W".format(int(target + 40)), up.tail(seconds))

    down = Recorder()
    await _set_target(tester, target)
    started = time.time()
    while time.time() - started < seconds:
        await asyncio.sleep(0.2)
        down.tick(tester)
    print_table("  台阶 2：目标 {}W".format(int(target)), down.tail(seconds))
    return up, down


def analyze(target: float, a: Recorder, b: Recorder, up: Recorder, down: Recorder,
            window: float) -> List[str]:
    """把三个对照阶段翻译成人话结论。"""
    lines: List[str] = []

    st_a = a.power_stats(window)
    st_b = b.power_stats(window)
    if st_a.get("n", 0) < 3 or st_b.get("n", 0) < 3:
        return ["没采到足够的数据（骑行台没在报功率？）。先把数据链路的问题解决掉。"]

    tol = max(8.0, 0.06 * target)
    err_a = st_a["mean"] - target
    err_b = st_b["mean"] - target
    move_a = a.resistance_motion(window)
    move_b = b.resistance_motion(window)

    have_res = move_a is not None or move_b is not None
    if not have_res:
        lines.append("· 骑行台**没有**在 Indoor Bike Data 里回报阻力档位，"
                     "只能靠功率和踏频推断。")

    lines.append("阶段 A（重发）：功率 {}W（目标 {}W，偏差 {:+.0f}W），波动 ±{:.0f}W".format(
        _fmt(st_a["mean"], 0, 0).strip() or "?", int(target), err_a, st_a["stdev"]))
    lines.append("阶段 B（只发一次）：功率 {}W（偏差 {:+.0f}W），波动 ±{:.0f}W".format(
        _fmt(st_b["mean"], 0, 0).strip() or "?", err_b, st_b["stdev"]))
    if have_res:
        lines.append("骑行台自己回报的阻力档位：阶段 A 变动 {:.1f} 档，阶段 B 变动 {:.1f} 档"
                     "（0 表示它一动没动）".format(move_a or 0.0, move_b or 0.0))
    lines.append("")

    st_up = up.power_stats(window)
    st_down = down.power_stats(window)

    # 判据 1：报了阻力档位、整段时间一动不动，而功率**又稳又偏** → 根本没在做闭环。
    # "又稳又偏"这个限定不能省：如果功率还在爬坡（波动大），那只是固件反应慢，
    # 不能据此判它是开环。
    stable_a = st_a["stdev"] <= max(8.0, 0.08 * target)
    if (have_res and max(move_a or 0.0, move_b or 0.0) < 1.0
            and abs(err_a) > tol and stable_a):
        lines.append("【结论】固件的“原生 ERG”不是闭环：它回报的阻力档位在整段时间里"
                     "一动不动，而功率又稳又偏（{}W ± {:.0f}W，目标 {}W）。".format(
                         _fmt(st_a["mean"], 0, 0).strip(), st_a["stdev"], int(target)))
        lines.append("       最可能的情况是：Set Target Power 被实现成“把瓦数换算成一次"
                     "阻力档位就完事”，之后不再回头看。")
        lines.append("       功率实际等于「那个固定阻力 × 你当时的踏频」——踏频一变，"
                     "功率就跟着漂，踩到 114W 是因为你的踏频刚好把功率踩到 114W，")
        lines.append("       并不是固件认为 114W 是对的。")
        lines.append("       → 建议：这台台子请固定用**闭环阻力**模式。")
        return lines

    # 判据 2：只发一次明显更好 → 是重发打断了固件的积分器。
    # 这条是 A/B 对照得出的，证据比单看一条曲线强，所以排在"阻力在动"前面。
    # 但必须先确认阶段 A 本身是**不合格**的：否则一台本来就很稳的设备，只要
    # B 的波动恰好小一点就会被判成"重发打断了积分器"，而"原生 ERG 是好的"那条
    # 结论就永远轮不到（实测随机数据里它会占掉一多半）。
    a_bad = abs(err_a) > tol or st_a["stdev"] > max(8.0, 0.08 * target)
    if a_bad and (abs(err_b) + 3.0 < abs(err_a)
                  or st_b["stdev"] + 3.0 < st_a["stdev"]):
        lines.append("【结论】阶段 B（只下发一次）明显收敛得比阶段 A（每 2 秒重发）好："
                     "偏差 {:+.0f}W → {:+.0f}W。".format(err_a, err_b))
        lines.append("       说明每 2 秒重发一次目标功率会打断固件自己的闭环积分——"
                     "很多廉价固件把“收到 Set Target Power”当成“重新开始整定”。")
        lines.append("       → 建议：把 POWER_KEEPALIVE_S 从 2 秒放宽到 30~60 秒，"
                     "只在目标真正变化时立即下发。")
        return lines

    # 判据 3：阻力在动，但功率收敛不到目标。
    # 同样要加"又稳又偏"这个限定，不能拿还在过渡中的数据下结论。
    if (have_res and max(move_a or 0.0, move_b or 0.0) >= 1.0
            and abs(err_a) > tol and stable_a):
        lines.append("【结论】固件确实在调阻力，也停不下来——说明它自己有闭环，"
                     "只是它内部认为对的功率和它上报给你的功率不是同一个数（刻度/零点偏差），"
                     "或者死区太大。")
        lines.append("       证据：阻力在动（阶段 A 动了 {:.1f} 档），但功率稳在偏差 {:+.0f}W 处。"
                     .format(move_a or 0.0, err_a))
        lines.append("       → 建议：用**闭环阻力**模式，程序按你读到的功率来伺服，"
                     "正好把这个偏差补掉。")
        return lines

    # 判据 4：功率能压住目标
    if abs(err_a) <= tol and st_a["stdev"] <= max(8.0, 0.08 * target):
        lines.append("【结论】原生 ERG 是好的：功率稳定在 {}W（目标 {}W，偏差 {:+.0f}W），"
                     "波动 ±{:.0f}W。".format(
                         _fmt(st_a["mean"], 0, 0).strip(), int(target), err_a, st_a["stdev"]))
        lines.append("       → 训练中被降级，多半是因为骑手自己平稳地骑低了、"
                     "或者数据链路卡了一下，不是固件的问题。")
        return lines

    # 判据 5：阶跃响应
    if st_up.get("n", 0) >= 3 and st_down.get("n", 0) >= 3:
        d_up = st_up["mean"] - (target + 40)
        d_down = st_down["mean"] - target
        lines.append("【结论】没命中上面任何一条明确判据，看阶跃响应：")
        lines.append("       目标 {}W → 实测 {}W（差 {:+.0f}W）；目标 {}W → 实测 {}W（差 {:+.0f}W）"
                     .format(int(target + 40), _fmt(st_up["mean"], 0, 0).strip(), d_up,
                             int(target), _fmt(st_down["mean"], 0, 0).strip(), d_down))
        if abs(d_up) > tol and abs(d_down) > tol:
            lines.append("       两个台阶都压不到位 → 这台固件压不住设定值，用闭环阻力。")
        else:
            lines.append("       台阶基本压到位 → 原生 ERG 可用，训练中那次偏离更可能是"
                         "骑手掉功率或数据陈旧造成的。")
    return lines


async def run(tester: Any, target: float, seconds: float, quick: bool = False) -> int:
    """跑完整诊断。``tester`` 可以是 TrainerClient，也可以是模拟骑行台。"""
    caps = getattr(tester, "capabilities", {}) or {}
    print("\n骑行台能力：")
    print("  名称              : {}".format(getattr(tester, "name", "?")))
    print("  声明支持目标功率  : {}".format(caps.get("supports_power_target")))
    print("  声明支持目标阻力  : {}".format(caps.get("supports_resistance_target")))
    print("  声明的目标类型    : {}".format(caps.get("declared_targets")))
    print("  阻力量程(原始值)  : {}".format(caps.get("resistance_range")))
    print("  机器信息          : {}".format(getattr(tester, "machine_info", None)))
    if not caps.get("has_control_point"):
        print("\n！没有控制点（Fitness Machine Control Point），这台设备没法被控制。")
        return 1

    try:
        await tester.start()
    except Exception as exc:  # noqa: BLE001
        print("\n！申请控制权 / 启动失败：{}".format(exc))
        print("  可能是被手机 App（Zwift / 官方 App）占着，先把它退掉再试。")
        return 1

    span = seconds
    if span < 30.0:
        print("\n！每个阶段只有 {:.0f} 秒，太短了：固件自己的整定还没走完，"
              "判读会把\"还在爬坡\"误当成\"压不住\"。".format(span))
        print("  正式诊断请用 --diag-seconds 60（或 --quick 的 30 秒）。"
              "下面的结论只当流程演示看。")
    print("\n" + "=" * 66)
    print("开始诊断：目标 {}W，每个阶段 {} 秒。请**保持踩踏**并在三个阶段里"
          "尽量用同一个踏频。".format(int(target), int(span)))
    print("=" * 66)

    a = await phase_a(tester, target, span)
    b = await phase_b(tester, target, span)
    up, down = await phase_c(tester, target, span)

    # 用每个阶段的最后 20 秒（quick 模式 10 秒）做判读，避开过渡过程
    window = min(10.0 if quick else 20.0, span / 2)

    print("\n" + "=" * 66)
    print("判读")
    print("=" * 66)
    for line in analyze(target, a, b, up, down, window):
        print("  " + line)

    try:
        await tester.stop()
    except Exception:  # noqa: BLE001
        pass
    return 0
