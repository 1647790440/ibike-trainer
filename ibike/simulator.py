"""模拟骑行台：没有硬件时用来把整套程序跑通、也用来做自动化回归测试。

它刻意复刻了真实设备的两类"毛病"，因为这才是兼容性测试的价值所在：

* ``responds_to_target_power=False`` —— 固件收下 Set Target Power 并回 Success，
  但压根不执行。现实中不少廉价固件就是这样，程序必须能识别并自动降级到闭环阻力。

* ``erg_offset_w`` —— 固件确实在跟随目标功率，但内部整定有固定偏差（永远比目标
  低这么多瓦）。它和上面那种"装死"的台子在判据上长得几乎一样（功率很稳、稳定
  偏离目标），区别只在于给它一个新目标时它**会不会跟过去**。

  用来自动判定"降不降级"的阶跃探测问的是"它能不能把功率压到设定值"，所以带固定
  偏差的台子会被判成不达标、改用闭环阻力——这是**有意**的：闭环按你实际读到的
  功率伺服，正好把这个偏差补掉；留着原生 ERG 的话，你骑 60 分钟、表上永远差那
  十几瓦。这个模拟档位就是用来验证"这种情况确实会切、而且切得干净"。

* 阻力-功率关系带一阶惯性（飞轮）和噪声，且强依赖踏频，这样闭环控制器必须真的
  把踏频补偿做对才能收敛，而不是靠作弊。
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from typing import Any, Callable, Dict, Optional

from .trainer import TrainerError

log = logging.getLogger("ibike.simulator")

# 模拟骑行台的物理参数：P ≈ K_MODEL · 阻力原始值 · 踏频
K_MODEL = 0.025          # raw=100（10.0 档）、踏频 85 → 约 212W
GEAR_RATIO = 2.94        # 约等于 50/17 齿比
WHEEL_CIRCUMFERENCE_M = 2.105   # 700×25c
FLYWHEEL_TAU = 2.5       # 功率响应的一阶时间常数（秒）
POWER_NOISE = 0.015      # 功率噪声幅度
ERG_TAU = 1.8            # 原生 ERG 下固件内部整定的时间常数


class SimulatedTrainer:
    """一个假的智能骑行台，接口与 TrainerClient 保持一致。"""

    def __init__(self,
                 on_data: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_event: Optional[Callable[[str, str], None]] = None,
                 responds_to_target_power: bool = True,
                 advertise_power_target: Optional[bool] = None,
                 cadence: float = 85.0,
                 erg_offset_w: float = 0.0,
                 name: str = "模拟骑行台") -> None:
        self.on_data = on_data
        self.on_event = on_event
        self.responds_to_target_power = responds_to_target_power
        self.erg_offset_w = float(erg_offset_w)
        if advertise_power_target is None:
            advertise_power_target = responds_to_target_power
        self.advertise_power_target = advertise_power_target
        self.base_cadence = cadence

        self.is_simulator = True
        self.name = name
        self.connected = False
        self.has_control = False
        self.latest: Dict[str, Any] = {}
        self.last_data_time = 0.0

        # 状态
        self.resistance_raw = 40.0     # 当前阻力
        self.erg_raw = 40.0            # 原生 ERG 内部整定出的阻力
        self.target_power: Optional[float] = None
        self.power = 0.0
        self.running = False
        self.paused = False
        self.t0 = time.time()

        self.capabilities: Dict[str, Any] = {
            "has_ftms": True,
            "has_control_point": True,
            "supports_power_target": advertise_power_target,
            "supports_resistance_target": True,
            "supports_inclination_target": True,
            "has_indoor_bike_data": True,
            "has_cycling_power": False,
            "power_range": {"min": 0, "max": 1500, "increment": 1},
            "resistance_range": {"min": 0, "max": 255, "increment": 1,
                                 "min_raw": 0, "max_raw": 255, "increment_raw": 1},
            "declared_targets": ["目标阻力", "目标功率", "目标坡度"],
            "declared_measurements": ["踏频", "功率", "速度", "阻力等级"],
        }
        self.machine_info = {
            "manufacturer": "iBike 模拟器",
            "model": "SIM-1",
            "firmware": "1.0.0",
            "battery": 99,
        }

        self._task: Optional[asyncio.Task] = None
        self._last_tick: Optional[float] = None
        self._commands: list = []
        self._publishing = True

    # ------------------------------------------------------------------
    # 与 TrainerClient 对齐的接口
    # ------------------------------------------------------------------

    async def connect(self, address: str = "simulator", timeout: float = 0.0) -> None:
        self.connected = True
        self.t0 = time.time()
        self._last_tick = asyncio.get_event_loop().time()
        self._task = asyncio.ensure_future(self._loop())
        log.info("模拟骑行台已连接（%s）", self.name)
        self._emit_event("connected", "已连接 {}".format(self.name))
        await asyncio.sleep(0.05)

    async def disconnect(self) -> None:
        await self.stop()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.connected = False

    async def start(self) -> None:
        if not self.connected:
            raise TrainerError("模拟骑行台未连接")
        self.has_control = True
        self.running = True
        self.paused = False
        self._commands.append(("start", None))

    async def pause(self) -> None:
        self.paused = True
        self.running = False
        self._commands.append(("pause", None))

    async def stop(self) -> None:
        self.running = False
        self.paused = False
        self.has_control = False
        self.target_power = None
        self.resistance_raw = 40.0
        self.erg_raw = 40.0
        self._commands.append(("stop", None))

    async def set_target_power(self, watts: int) -> None:
        if not self.connected:
            raise TrainerError("模拟骑行台未连接")
        self.target_power = float(watts)
        self._commands.append(("target_power", watts))
        if not self.responds_to_target_power:
            # 关键：假装成功，实际什么也不做，用来验证上层的自动降级
            return

    async def set_resistance_raw(self, raw: int) -> None:
        if not self.connected:
            raise TrainerError("模拟骑行台未连接")
        raw = max(0, min(255, int(round(raw))))
        self.resistance_raw = float(raw)
        self._commands.append(("resistance", raw))

    async def set_resistance_level(self, level: float) -> None:
        await self.set_resistance_raw(int(round(level * 10)))

    # ------------------------------------------------------------------
    # 物理仿真
    # ------------------------------------------------------------------

    def _cadence_at(self, t: float) -> float:
        """模拟一个不太稳的骑手：基础踏频上叠加慢漂移和快抖动。"""
        drift = 3.0 * math.sin(t / 23.0) + 1.8 * math.sin(t / 7.0)
        jitter = random.uniform(-1.2, 1.2)
        return max(0.0, self.base_cadence + drift + jitter)

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.05)
                now = asyncio.get_event_loop().time()
                dt = 0.05 if self._last_tick is None else now - self._last_tick
                self._last_tick = now
                self._tick(dt)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("模拟器循环异常")

    def set_publishing(self, enabled: bool) -> None:
        """停止 / 恢复上报数据。

        用来模拟"蓝牙链路还在、但设备不再推送 Indoor Bike Data"这种真实故障——
        上层必须能识别出数据已经陈旧，而不是继续拿最后一帧的功率值算下去。
        """
        self._publishing = enabled

    def _tick(self, dt: float) -> None:
        t = time.time() - self.t0
        cadence = self._cadence_at(t)

        # 原生 ERG：固件内部把阻力整定到能维持目标功率的位置（带惯性滞后）
        if (self.running and self.responds_to_target_power
                and self.target_power is not None and cadence > 25.0):
            # 固件内部整定的目标：正常是目标功率本身，带偏置的台子永远差一截
            erg_goal = max(0.0, self.target_power - self.erg_offset_w)
            ideal = erg_goal / (K_MODEL * cadence)
            ideal = max(10.0, min(255.0, ideal))
            self.erg_raw += (ideal - self.erg_raw) * min(1.0, dt / ERG_TAU)
            self._effective_resistance = self.erg_raw
        else:
            self._effective_resistance = self.resistance_raw

        # 实际功率：一阶惯性趋近准静态模型值
        target_power_physical = K_MODEL * self._effective_resistance * cadence
        self.power += (target_power_physical - self.power) * min(1.0, dt / FLYWHEEL_TAU)
        power_noisy = max(0.0, self.power * (1.0 + random.uniform(-POWER_NOISE, POWER_NOISE)))

        speed = 0.0
        if cadence > 0:
            # 粗略折算：700c 轮周长 2.105m，中等齿比约 50/17 ≈ 2.94。
            # 踏频 87 时约 32km/h，这才是室内骑行的合理量级。
            speed = cadence / 60.0 * GEAR_RATIO * WHEEL_CIRCUMFERENCE_M * 3.6

        self.latest = {
            "power_w": int(round(power_noisy)),
            "cadence_rpm": round(cadence, 1),
            "speed_kmh": round(speed, 2),
            "resistance_level": round(self._effective_resistance / 10.0, 1),
            "trainer_elapsed_s": int(t),
        }
        if not self._publishing:
            return
        # 和 TrainerClient 保持一致，用单调时钟（见 trainer.py 的说明）
        self.last_data_time = time.monotonic()
        if self.on_data is not None:
            try:
                self.on_data(dict(self.latest))
            except Exception:
                log.exception("模拟器数据回调出错")

    def _emit_event(self, kind: str, message: str) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, message)
            except Exception:
                log.exception("模拟器事件回调出错")

    # ------------------------------------------------------------------

    def state(self) -> Dict[str, Any]:
        return {
            "kind": "simulator",
            "connected": self.connected,
            "name": self.name,
            "capabilities": self.capabilities,
            "machine_info": self.machine_info,
            "has_control": self.has_control,
            "commands_sent": len(self._commands),
        }
