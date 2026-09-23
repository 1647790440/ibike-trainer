"""训练会话引擎：倒计时、暂停、统计，以及把功率压到目标值的两种 ERG 实现。

两条控制路径：

1. ``ftms``  —— 原生 ERG。直接下发 Set Target Power (0x05)，由骑行台固件内部
   闭环控制阻力。Zwift 走的就是这条路。前提是骑行台声明并真正支持"目标功率"。

2. ``resistance`` —— 上位机闭环。只下发 Set Target Resistance Level (0x04)，
   由本程序读回实际功率，用自适应控制器不断修正阻力档位。适用于只支持设阻力
   的固件（不少国产动感单车就是这种）。

``auto`` 模式先按原生 ERG 走，如果**实证确认**骑行台压根不理会目标功率，才会
降级到闭环阻力控制。这一点对兼容性很重要——光看 Feature 特征值并不可靠，有些
固件声明支持目标功率，实际却是空操作。

但"功率很稳却稳定偏离目标"这一条只是**嫌疑**，不是证据：动感单车的功率往往是
固件用「阻力 × 踏频」算出来的，骑手踏频一稳，功率就能稳得毫无波动，看上去像
固件在装死，实际上它可能正在慢慢加阻力。所以怀疑之后还要做一次**目标功率阶跃
探测**——把目标往下压一档，看功率跟不跟——跟了就留着原生 ERG，确实不跟才降级。
"""

from __future__ import annotations

import asyncio
import logging
import time
from bisect import bisect_right
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .ftptest import KIND_TEST, RESULT_BEST_SEGMENT, RESULT_RAMP
from .heartrate import (HR_MAX_PLAUSIBLE, HR_MIN_PLAUSIBLE, hr_zone_of,
                        hr_zones)
from .trainer import TrainerError

log = logging.getLogger("ibike.session")

ERG_AUTO = "auto"
ERG_FTMS = "ftms"
ERG_RESISTANCE = "resistance"
# 自由骑行：设一次阻力就不管了，功率完全由骑手自己配速。
# FTP 测试里的 20 分钟 / 8 分钟计时段必须用这个模式——ERG 会把功率锁死在
# 设定值上，那样测出来的是"你设定的数"，不是你的能力。
ERG_FREE = "free"

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_FINISHED = "finished"
STATE_ERROR = "error"

# 原生 ERG 模式下，判定"骑行台没在理会目标功率"的判据：
# 功率**很稳**但**稳在错的数值上**——这是固件收下指令却不执行的典型特征。
# 反过来，如果功率在上下波动，说明固件正在努力收敛，不该贸然降级。
# 注意这只是"立案"，不是"定罪"：定罪要靠下面那次阶跃探测。
_NATIVE_FALLBACK_MIN_ERROR_W = 8.0    # 容差下限，实际取 max(8W, 目标的 6%)
_NATIVE_FALLBACK_TOL_RATIO = 0.06
_NATIVE_FALLBACK_STABLE_W = 8.0       # 标准差小于这个值才算"很稳"
_NATIVE_FALLBACK_HOLD_S = 18.0        # 需要连续观察这么久
_NATIVE_FALLBACK_GRACE_S = 12.0       # 起步后先给固件这么多时间

# 降级前的目标功率阶跃探测。
# 为什么不直接降级：功率稳、但偏离目标，也可能是骑手自己平稳地骑低了一点，
# 或者固件正在慢慢把阻力加回去。切换模式会把控制权整个夺走，代价太大，
# 所以先做一次只有几瓦代价的探测，拿到证据再决定。
_NATIVE_PROBE_DELTA_W = 25.0          # 把目标往下压这么多瓦
_NATIVE_PROBE_WAIT_S = 20.0           # 观察这么久，看功率能不能收敛到新目标
_NATIVE_PROBE_COOLDOWN_S = 600.0      # 确认固件能收敛后，这么久内不再打扰它
                                     # （探测本身会故意把目标压低 25W 持续 20 秒，
                                     #  过于频繁地重复探测对骑手是实打实的干扰）

# 超过这么久没收到骑行台数据就判定为"数据陈旧"，停止积分与控制并告警
STALE_AFTER_S = 3.0

# 心率上限保护（安全刹车，不是控制方式——心率滞后几十秒，做闭环必然振荡）
HR_LIMIT_HOLD_S = 10.0          # 超过上限持续这么久才动手
HR_LIMIT_COOLDOWN_S = 20.0      # 两次下调之间至少间隔这么久
HR_LIMIT_STEP_RATIO = 0.95      # 每次下调 5%
HR_LIMIT_FLOOR_RATIO = 0.80     # 最多降到本段原目标的 80%，不再往下
HR_LIMIT_RECOVER_S = 60.0       # 回落到上限以下并持续这么久，才回到原计划

# 坡道测试的力竭判据。换级之后功率要几秒才爬上来，所以先给一段宽限期，
# 再判断"踩不动了"。宁可多给几秒，也别在人家还能撑的时候把测试掐掉。
RAMP_STEP_GRACE_S = 6.0
RAMP_FAIL_POWER_RATIO = 0.80    # 5 秒平均掉到目标的 80% 以下
RAMP_FAIL_HOLD_S = 5.0
RAMP_FAIL_CADENCE = 50.0        # 或者踏频掉到 50 以下
RAMP_FAIL_CADENCE_HOLD_S = 3.0
RAMP_MIN_STEPS = 3              # 至少要踩过这么多级才算一次有效测试
                                # （1 分钟一级的话就是 3 分钟；再少说明起步设太高了）

# 自由骑行的默认阻力档位（原始 0-255）。不同骑行台刻度差别很大，
# 界面上可以随时微调。
DEFAULT_FREE_RESISTANCE = 90


class ResistancePowerController:
    """在只能设阻力的骑行台上，用自适应闭环把功率压到目标值。

    物理近似：功率正比于 阻力 × 踏频，即 ``P ≈ k · R · C``（R 为原始阻力值，
    C 为踏频 rpm）。要维持目标功率，理想阻力是 ``R = P_target / (k · C)``。

    ``k`` 事先未知，而且会随温度、皮带张力、骑行台个体差异漂移，所以在骑行过程
    中在线估计：

        k_hat ← (1-α)·k_hat + α·(P / (R·C))

    再按 ``dR = β · (P_target - P) / (k_hat · C)`` 求本步修正量。用估计出的 k
    来归一化，意味着踏频变化会被自动补偿——这正是 ERG 模式该有的手感。

    两个关键的抗振荡设计（少了它们功率会来回冲）：

    * **β < 1 的松弛因子**：每步只修正一部分误差，而不是一次修正到位。功率测量
      天生带滞后（飞轮惯性 + 平均窗口），一次修正到位必然过冲，然后反向过冲，
      形成振荡。β 取 0.55 时误差每步衰减一半，既快又不冲。

    * **只在系统稳定时辨识 k**：改变阻力后的一两个控制周期内，测到的功率还是
      旧阻力的余温，拿它去更新 k 会把模型带偏。所以只在阻力保持不变的稳定期
      才更新 k，而且用很小的 α 慢慢平滑。
    """

    def __init__(self, target_power: float, r_min: int = 0, r_max: int = 255,
                 alpha: float = 0.12, beta: float = 0.55, deadband_w: float = 3.0,
                 max_step_raw: float = 10.0, settle_s: float = 3.0) -> None:
        self.target_power = float(target_power)
        self.r_min = float(r_min)
        self.r_max = float(r_max)
        self.alpha = alpha
        self.beta = beta
        self.deadband_w = deadband_w
        self.max_step_raw = max_step_raw
        self.settle_s = settle_s

        self.k_hat: Optional[float] = None
        self.level_raw: Optional[float] = None
        self.last_reason = ""
        self._last_change: Optional[float] = None

    def seed(self, level_raw: float) -> None:
        """设定初始阻力档位（一般取量程的 1/4 左右，给上下调整都留余量）。"""
        self.level_raw = max(self.r_min, min(self.r_max, level_raw))

    def _estimate_k(self, power: float, level_raw: float, cadence: float) -> None:
        if level_raw <= 1.0 or cadence <= 25.0 or power <= 0.0:
            return
        sample = power / (level_raw * cadence)
        if not (0.0 < sample < 100.0):
            return
        if self.k_hat is None:
            self.k_hat = sample
        else:
            self.k_hat = (1.0 - self.alpha) * self.k_hat + self.alpha * sample

    def update(self, power_avg: Optional[float], cadence: Optional[float], now: float
               ) -> Tuple[Optional[float], str]:
        """返回 (新的阻力原始值, 说明)。返回 None 表示本步不需要调整。"""
        if self.level_raw is None:
            self.seed((self.r_min + self.r_max) / 4.0)

        if power_avg is None:
            self.last_reason = "还没有功率数据"
            return None, self.last_reason
        if cadence is None or cadence < 25.0:
            # 没在踩踏时功率必然掉到 0，此时调阻力毫无意义，反而会导致
            # 重新开始踩踏时阻力大得踩不动。
            self.last_reason = "踏频过低，暂不调整阻力"
            return None, self.last_reason

        # 只在阻力已经稳定下来的阶段辨识模型，避免把过渡过程当成稳态特性
        if self._last_change is None or (now - self._last_change) >= self.settle_s:
            self._estimate_k(power_avg, self.level_raw, cadence)

        error = self.target_power - power_avg
        if abs(error) < self.deadband_w:
            self.last_reason = "误差 {:+.1f}W 在死区内".format(error)
            return None, self.last_reason

        if self.k_hat is None:
            # 还没建立模型，先按固定步长朝正确方向摸索
            delta = 6.0 if error > 0 else -6.0
            self.last_reason = "正在建立阻力-功率模型"
        else:
            delta = self.beta * error / (self.k_hat * cadence)
            self.last_reason = "k={:.4f} 误差{:+.1f}W 步进{:+.1f}".format(
                self.k_hat, error, delta)

        delta = max(-self.max_step_raw, min(self.max_step_raw, delta))
        new_level = max(self.r_min, min(self.r_max, self.level_raw + delta))
        if abs(new_level - self.level_raw) < 0.5:
            self.last_reason += "（已到量程边界）"
            return None, self.last_reason

        self.level_raw = new_level
        self._last_change = now
        return new_level, self.last_reason


class WorkoutSession:
    """一次"设定时长 + 设定功率"的骑行训练。"""

    TICK = 0.1                 # 主循环 10Hz
    POWER_KEEPALIVE_S = 2.0    # 原生 ERG 下重发目标功率的间隔
    # 闭环控制的节奏：4 秒一次。飞轮惯性让功率有两三秒的滞后，调得太勤必然振荡。
    RESISTANCE_INTERVAL_S = 4.0
    POWER_WINDOW_S = 5.0       # 界面上显示的平滑功率窗口
    CONTROL_WINDOW_S = 2.0     # 闭环控制实际使用的窗口，取调整周期靠后的一段，
                               # 避开刚改完阻力那段还没稳定的数据

    # 自动降级的判据与探测参数，做成类属性是为了让测试能压缩时间轴，
    # 不必真的等 18 秒 + 20 秒。
    FALLBACK_GRACE_S = _NATIVE_FALLBACK_GRACE_S
    FALLBACK_HOLD_S = _NATIVE_FALLBACK_HOLD_S
    FALLBACK_STABLE_W = _NATIVE_FALLBACK_STABLE_W
    FALLBACK_MIN_ERROR_W = _NATIVE_FALLBACK_MIN_ERROR_W
    FALLBACK_TOL_RATIO = _NATIVE_FALLBACK_TOL_RATIO
    PROBE_DELTA_W = _NATIVE_PROBE_DELTA_W
    PROBE_WAIT_S = _NATIVE_PROBE_WAIT_S
    PROBE_COOLDOWN_S = _NATIVE_PROBE_COOLDOWN_S

    def __init__(self, trainer: Any,
                 on_update: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_event: Optional[Callable[[str, str], None]] = None,
                 heart_rate: Optional[Any] = None) -> None:
        self.trainer = trainer
        # 独立心率带（ibike.heartrate.HeartRateClient）。它是**第二个外设**，和骑行台
        # 各连各的：可以为 None（没接带子），也可以中途接上/断开。
        # 心率还有第二个来源——骑行台自己的 Indoor Bike Data 里那个字段，
        # 取数时"独立心率带优先"，见 _ingest_trainer_data。
        self.heart_rate = heart_rate
        # 这次训练用的心率设置（最大心率/静息心率/区间算法/上限保护），
        # 开训时从 config 里冻结下来：训练中途改设置不该影响正在进行的这一场。
        self.hr_config: Dict[str, Any] = {}
        self.on_update = on_update
        self.on_event = on_event

        self.state = STATE_IDLE
        self.target_power = 100.0
        self.duration_s = 3600.0
        self.erg_mode = ERG_AUTO
        self.active_erg_mode: Optional[str] = None

        # 训练计划。恒定功率就是"只有一个步骤的计划"，两条路径共用同一套推进逻辑。
        self.plan: List[Dict[str, Any]] = []
        self.plan_name = "恒定功率"
        self.step_index = 0
        self._step_starts: List[float] = []
        self._step_bounds: List[float] = []
        self._work_indices: List[int] = []
        self._work_position: Dict[int, int] = {}
        self._test_indices: List[int] = []

        self.elapsed_s = 0.0
        # elapsed_s 是"计划位置"（用于定位当前段、算剩余时间），跳到下一段会直接
        # 跳前一段时间；active_s 才是真正累计的骑行时间。两者都保留：总结里的
        # "实际时长"和平均功率必须用 active_s，否则跳过的时间会被当成骑过的。
        self.active_s = 0.0
        self.started_at: Optional[float] = None
        # 与 started_at 同时刻，但走单调时钟：只用于「还没收到过任何数据」时的
        # 陈旧判定（见 _ingest_trainer_data）
        self._mono_started_at: Optional[float] = None
        self.finished_at: Optional[float] = None

        self.current_power: Optional[float] = None
        self.current_cadence: Optional[float] = None
        self.current_speed: Optional[float] = None
        self.current_hr: Optional[float] = None
        # 这一帧心率是从哪来的："strap"（独立心率带）/ "trainer"（骑行台自带）/ None
        self.hr_source: Optional[str] = None
        self.rr_intervals = 0          # 最近一帧带回来几个 RR 间期（有无 RR 靠它判断）
        self.hr_contact: Optional[str] = None
        # 平均/最大心率用独立累加器（10Hz），和踏频同一个道理：
        # 只靠 1Hz 的 trace 会漏掉"停止前最后不到一秒"里那几次采样
        self._hr_integral = 0.0
        self._hr_time = 0.0
        self._hr_max = 0.0
        # 心率上限保护（F4）的状态
        self._hr_over_since: Optional[float] = None
        self._hr_ok_since: Optional[float] = None
        self._hr_last_action = 0.0
        self._hr_step_locked = False
        self._hr_cap_note = ""
        self.resistance_raw: Optional[float] = None
        self.controller: Optional[ResistancePowerController] = None

        self.max_power = 0.0
        self.energy_kj = 0.0
        self.distance_m = 0.0
        self._power_integral = 0.0
        # 真正收到有效功率数据的秒数。它和 active_s 的区别就是"数据断链"那一段：
        # 断链时功率积分停了，但 active_s 还在走，用 active_s 当分母会把平均功率
        # 越摊越低（实测 20 秒断链能把 100W 的骑行显示成 45W）。
        self._sampled_s = 0.0
        self._np_sum = 0.0
        self._np_count = 0
        self._np_window: Deque[Tuple[float, float]] = deque()
        self._power_window: Deque[Tuple[float, float]] = deque()
        self._control_window: Deque[Tuple[float, float]] = deque()

        self.error_message: Optional[str] = None
        self.last_command_note = ""
        self.no_data_since: Optional[float] = None
        self._stale = False

        # 训练总结用的数据
        self._trace: List[Dict[str, Any]] = []       # 逐秒轨迹，用于曲线和分布
        self._last_trace: Optional[float] = None
        self._cadence_integral = 0.0
        self._cadence_time = 0.0
        self._cadence_max = 0.0
        self.summary: Optional[Dict[str, Any]] = None
        self.stop_reason: Optional[str] = None
        self.workout_config: Dict[str, Any] = {}
        # FTP 测试相关
        self._test_meta: Optional[Dict[str, Any]] = None
        self._test_result: Optional[Dict[str, Any]] = None
        self._ramp_low_since: Optional[float] = None
        self.free_resistance = float(DEFAULT_FREE_RESISTANCE)
        # 控功率方式的切换记录。排查"阻力为什么突然变了"这类问题时，
        # 光看最终模式是不够的——必须知道什么时候切的、为什么切的。
        self._mode_log: List[Dict[str, Any]] = []

        self._task: Optional[asyncio.Task] = None
        self._last_dt_time: Optional[float] = None
        self._last_keepalive = 0.0
        self._last_resistance_adj = 0.0
        self._native_watch: Deque[Tuple[float, float]] = deque()
        # 闭环阻力连续下发失败的计数与熔断标志（见 _tick_closed_loop）
        self._resistance_failures = 0
        self._resistance_blocked = False
        # 降级前的目标功率阶跃探测。None 表示当前没在探测。
        self._native_probe: Optional[Dict[str, Any]] = None
        self._probe_cooldown_until = 0.0
        self._distance_accum = 0.0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, target_power: float = 100.0, duration_min: float = 60.0,
                    erg_mode: str = ERG_AUTO,
                    plan: Optional[List[Dict[str, Any]]] = None,
                    plan_name: str = "",
                    config: Optional[Dict[str, Any]] = None) -> None:
        """开始一次训练。

        传 ``plan`` 就是间歇训练，不传就是恒定功率——后者在内部同样被展开成
        "只有一个步骤的计划"，这样推进、暂停、跳过、统计都只有一套代码。
        """
        if self.state == STATE_RUNNING:
            raise RuntimeError("训练已经在进行中")

        # 从"暂停"状态再开一次的话，上一个主循环还在跑。不把它清掉就会留下一个
        # 永不放手的孤儿循环：它会一直空转，并在下次训练时和新循环一起向骑行台
        # 下发指令。（正常界面走不到这里，但两个标签页、或者直接调接口都能触发。）
        await self._cancel_task()

        # 原样记下这次是怎么配的，总结里带回给前端，"再来一次"才能精确复现
        self.workout_config = dict(config or {})
        # 心率相关设置也一起冻结（见 __init__ 里的说明）
        self.hr_config = dict(self.workout_config.get("heart_rate") or {})
        self._reset_hr_cap()
        # FTP 测试：从配置里取出结果计算方式，测试期间用来自动判力竭
        self._test_meta = self.workout_config.get("test") or None
        self._test_result = None
        self._ramp_low_since = None
        self._mode_log = []
        self.free_resistance = float(
            self.workout_config.get("free_resistance") or DEFAULT_FREE_RESISTANCE)

        if plan:
            self.plan = [dict(s) for s in plan]
            self.plan_name = plan_name or "间歇训练"
        else:
            self.plan = [{
                "name": "恒定功率",
                "kind": "work",
                "duration_s": max(1.0, float(duration_min) * 60.0),
                "pct_ftp": None,
                "target_power": int(round(float(target_power))),
            }]
            self.plan_name = "恒定功率"

        # 预先算好每一步的起止时刻，主循环里就能用二分查找定位当前步骤
        self._step_starts = []
        self._step_bounds = []
        acc = 0.0
        for step in self.plan:
            self._step_starts.append(acc)
            acc += max(0.1, float(step["duration_s"]))
            self._step_bounds.append(acc)
        self.duration_s = acc
        self._work_indices = [i for i, s in enumerate(self.plan)
                              if s.get("kind") == "work"]
        self._test_indices = [i for i, s in enumerate(self.plan)
                              if s.get("kind") == KIND_TEST]
        self._work_position = {idx: n + 1 for n, idx in enumerate(self._work_indices)}
        self.step_index = 0
        self.target_power = float(self.plan[0]["target_power"])

        # ERG_FREE 也要留在白名单里：漏掉它的话，自由骑行的测试会在快照和报告里
        # 把自己的"请求模式"写成 auto，看起来像是程序自己改了主意。
        self.erg_mode = (erg_mode if erg_mode in (ERG_AUTO, ERG_FTMS, ERG_RESISTANCE,
                                                 ERG_FREE)
                         else ERG_AUTO)

        self.elapsed_s = 0.0
        self.active_s = 0.0
        self.started_at = time.time()
        self._mono_started_at = time.monotonic()
        self.finished_at = None
        self.max_power = 0.0
        self.energy_kj = 0.0
        self.distance_m = 0.0
        self._power_integral = 0.0
        self._np_sum = 0.0
        self._np_count = 0
        self._np_window.clear()
        self._power_window.clear()
        self._control_window.clear()
        self._distance_accum = 0.0
        self.error_message = None
        self.no_data_since = None
        self._stale = False
        self._native_watch.clear()
        self._native_probe = None
        self._probe_cooldown_until = 0.0

        self._trace = []
        self._last_trace = None
        self._cadence_integral = 0.0
        self._cadence_time = 0.0
        self._hr_integral = 0.0
        self._hr_time = 0.0
        self._hr_max = 0.0
        self._cadence_max = 0.0
        self._sampled_s = 0.0
        self.summary = None
        self.stop_reason = None

        self.active_erg_mode = self._resolve_mode(erg_mode)
        self._mode_log = []
        self.controller = None

        # 整个准备过程都要兜住：只包住 trainer.start() 的话，后面下发目标功率失败时
        # 会留下"状态还是 idle、但骑行台已经被占住并 Start/Resume"的不一致状态。
        try:
            await self.trainer.start()
            if self.active_erg_mode == ERG_FREE:
                await self._enter_free_mode()
            elif self.active_erg_mode == ERG_RESISTANCE:
                await self._enter_resistance_mode(initial=True)
            else:
                try:
                    await self._send_target_power(self.target_power)
                except TrainerError as exc:
                    # 有些固件在 Feature 里声明支持目标功率，真下发时却回一句
                    # "不支持这个操作码"。用户选的是 auto、而这台台子又能设阻力，
                    # 那就该退回闭环阻力把训练开起来，而不是直接报错收场——
                    # 以前这里会让 auto 模式在一台"只能设阻力"的台子上直接失败。
                    caps = getattr(self.trainer, "capabilities", {}) or {}
                    if (self.erg_mode == ERG_AUTO and caps.get("has_control_point")
                            and caps.get("supports_resistance_target")):
                        log.warning("目标功率不被支持，自动改用闭环阻力：%s", exc)
                        self._notify(
                            "fallback",
                            "骑行台不支持目标功率（{}），已改用闭环阻力控制".format(exc))
                        await self._enter_resistance_mode(
                            initial=True, reason="自动回退：固件不支持目标功率")
                    else:
                        raise
        except TrainerError as exc:
            self.state = STATE_ERROR
            self.error_message = str(exc)
            try:
                await self.trainer.stop()      # 尽量把骑行台放开
            except Exception:
                log.warning("回滚时释放骑行台失败", exc_info=True)
            await self._emit()
            raise

        self.state = STATE_RUNNING
        self._last_dt_time = asyncio.get_event_loop().time()
        self._last_keepalive = self._last_dt_time
        self._last_resistance_adj = self._last_dt_time
        self._last_trace = self._last_dt_time
        self._task = asyncio.ensure_future(self._run())
        if len(self.plan) > 1:
            self._notify("started", "开始间歇训练「{}」：{} 分钟 / {} 段，峰值 {}W（{}）".format(
                self.plan_name, int(self.duration_s // 60), len(self.plan),
                max(s["target_power"] for s in self.plan), self._mode_label()))
        else:
            self._notify("started", "开始训练：{} 分钟 @ {}W（{}）".format(
                int(self.duration_s // 60), int(self.target_power), self._mode_label()))
        await self._emit()

    def _resolve_mode(self, requested: str) -> str:
        caps = getattr(self.trainer, "capabilities", {}) or {}
        # 能力未知时不猜：宁可报错也不下发一条设备看不懂的指令
        has_control_point = bool(caps.get("has_control_point"))
        supports_native = bool(caps.get("supports_power_target")) and has_control_point
        supports_resistance = bool(caps.get("supports_resistance_target")) and has_control_point

        if requested == ERG_FTMS:
            if not supports_native:
                raise TrainerError("该骑行台不支持设定目标功率，请改用闭环阻力模式")
            return ERG_FTMS
        if requested == ERG_RESISTANCE:
            if not supports_resistance:
                raise TrainerError("该骑行台不支持设定阻力，无法用闭环模式")
            return ERG_RESISTANCE
        if requested == ERG_FREE:
            # 自由骑行至少要能设一次阻力，否则骑手没法控制负荷
            if not supports_resistance:
                raise TrainerError("该骑行台不支持设定阻力，无法做自由骑行")
            return ERG_FREE
        # auto
        if supports_native:
            return ERG_FTMS
        if supports_resistance:
            return ERG_RESISTANCE
        raise TrainerError("该骑行台既不能设目标功率也不能设阻力，无法控制")

    def _set_mode(self, mode: str, reason: str) -> None:
        """切换控功率方式，并记下时间点和原因。"""
        if self.active_erg_mode == mode:
            return
        self._mode_log.append({
            "t": round(self.elapsed_s, 1),
            "from": self.active_erg_mode,
            "to": mode,
            "reason": reason,
        })
        log.info("控功率方式切换：%s → %s（%s）", self.active_erg_mode, mode, reason)
        self.active_erg_mode = mode

    def _log_mode_event(self, kind: str, reason: str) -> None:
        """记一条与控功率方式有关、但并不是切换的事件（如降级前的探测）。

        统一放进 mode_changes 里，是因为用户复盘时问的是同一个问题：
        "这一分钟到底发生了什么"。只记录切换的话，探测期间那 20 秒的
        功率凹坑就成了无源之水。
        """
        self._mode_log.append({
            "t": round(self.elapsed_s, 1),
            "kind": kind,
            "from": self.active_erg_mode,
            "to": self.active_erg_mode,
            "reason": reason,
        })

    def _mode_label(self) -> str:
        if self.active_erg_mode == ERG_FTMS:
            return "原生 ERG"
        if self.active_erg_mode == ERG_RESISTANCE:
            return "闭环阻力"
        if self.active_erg_mode == ERG_FREE:
            return "自由骑行"
        return "未知"

    async def pause(self) -> None:
        if self.state != STATE_RUNNING:
            return
        # 暂停会打断探测的连续性（功率必然掉到 0），结论不可信，作废
        self._native_probe = None
        self.state = STATE_PAUSED
        try:
            await self.trainer.pause()
        except Exception as exc:
            log.warning("暂停指令下发失败：%s", exc)
        self._notify("paused", "已暂停")
        await self._emit()

    async def resume(self) -> None:
        if self.state != STATE_PAUSED:
            return
        try:
            await self.trainer.start()
            if self.active_erg_mode == ERG_RESISTANCE:
                await self._enter_resistance_mode(initial=False)
            elif self.active_erg_mode == ERG_FREE:
                # 自由骑行绝不能下发目标功率：那会让固件进入 ERG、把功率锁死在
                # 设定值上，20/8 分钟 FTP 测试测出来的就变成"你设的那个数"了。
                # 重新确认一次阻力档位即可（和 _enter_free_mode 做的事一样）。
                await self._enter_free_mode()
            else:
                await self._send_target_power(self.target_power)
        except TrainerError as exc:
            # 起不来就别假装在跑：状态留在暂停，骑手再点一次"继续"就行。
            # 以前这里也把状态改成 running，结果是计时在走、骑行台还停着。
            self.error_message = str(exc)
            self._notify("resume-failed", "继续失败：{}".format(exc))
            await self._emit()
            return

        # 上面几个 await 期间，可能已经被 /api/stop 收尾了（暂停/继续没有和停止
        # 串行化）。这时候再把自己置成 running，就会留下一个"状态在跑、但主循环
        # 已经被取消"的僵尸会话：计时不动、指令也不发。
        if self.state != STATE_PAUSED:
            return

        self.state = STATE_RUNNING
        now = asyncio.get_event_loop().time()
        self._last_dt_time = now
        # 暂停这段时间不能被算进"这一小节骑了多久"——trace 的 dt 是两个采样点
        # 之间的间隔，不重置的话恢复后的第一个点会带上整段暂停时间，于是分段
        # 时长、区间分布、甚至 FTP 测试的"计时段完成度"全都会虚高。
        self._last_trace = now
        self._last_resistance_adj = now
        self._notify("resumed", "继续训练")
        await self._emit()

    async def stop(self, reason: str = "用户停止") -> None:
        # 先落状态再做别的。stop() 里要等骑行台应答（可能几百毫秒到 2.5 秒），
        # 这个窗口里如果再来一次 stop，会看到 state 还是 running 而重复下发 STOP；
        # 如果来的是 start，旧的本次收尾还会把新训练的状态覆盖掉。
        if self.state in (STATE_IDLE, STATE_FINISHED):
            return
        self.state = STATE_FINISHED
        self.finished_at = time.time()

        await self._cancel_task()
        try:
            await self.trainer.stop()
        except Exception as exc:
            log.warning("停止指令下发失败：%s", exc)

        # 收尾期间可能已经开始了新训练，那就别去动它的状态和总结
        if self.state != STATE_FINISHED:
            return
        self.stop_reason = reason
        self.summary = self._build_summary(reason)
        self._notify("stopped", "训练结束（{}）".format(reason))
        await self._emit()

    async def dismiss(self) -> None:
        """关掉总结面板，回到可以重新设定的状态（骑行台保持连接）。"""
        if self.state != STATE_FINISHED:
            return
        self.summary = None
        self.stop_reason = None
        self.state = STATE_IDLE
        self.plan = []
        self.plan_name = "恒定功率"
        self._step_starts = []
        self._step_bounds = []
        self._work_indices = []
        self._work_position = {}
        self.step_index = 0
        self.active_erg_mode = None
        self.controller = None
        self.target_power = 100.0
        self.duration_s = 3600.0
        self.elapsed_s = 0.0
        self.active_s = 0.0
        self.started_at = None
        self._mono_started_at = None
        self.finished_at = None
        self.current_power = None
        self.current_cadence = None
        self.current_speed = None
        self.current_hr = None
        self.resistance_raw = None
        self.max_power = 0.0
        self.energy_kj = 0.0
        self.distance_m = 0.0
        self._power_integral = 0.0
        self._sampled_s = 0.0
        self._hr_integral = 0.0
        self._hr_time = 0.0
        self._hr_max = 0.0
        self._cadence_integral = 0.0
        self._cadence_time = 0.0
        self._cadence_max = 0.0
        self._distance_accum = 0.0
        self._trace = []
        self._last_trace = None
        self._power_window.clear()
        self._control_window.clear()
        self._np_window.clear()
        # 这两个也必须清：只清窗口的话，idle 状态还会继续报上一场的标准化功率
        self._np_sum = 0.0
        self._np_count = 0
        # 测试相关的状态同样要清。漏掉的话，刚测完 FTP、关掉总结回到设定页，
        # 界面上还挂着"第 N 级"的测试条，因为快照里的 is_test/test_id 还是真的。
        self._test_meta = None
        self._test_indices = []
        self._test_result = None
        self.workout_config = {}
        self.hr_config = {}
        self.current_hr = None
        self.hr_source = None
        self.rr_intervals = 0
        self.hr_contact = None
        self._reset_hr_cap()
        self.free_resistance = float(DEFAULT_FREE_RESISTANCE)
        self._native_watch.clear()
        self._native_probe = None
        self._probe_cooldown_until = 0.0
        self.last_command_note = ""
        self.no_data_since = None
        self._stale = False
        self.error_message = None
        await self._emit()

    # ------------------------------------------------------------------
    # 训练总结
    # ------------------------------------------------------------------

    def _build_summary(self, reason: str) -> Dict[str, Any]:
        """把这次训练整理成一份总结。

        核心指标刻意选了"控功率达标率"和"平均偏差"：这套程序的价值就在于把功率
        压在目标上，光看平均功率是看不出来控得稳不稳的——平均 100W 可能是一路
        精确的 100W，也可能是 80 和 120 各占一半。
        """
        trace = self._trace
        target = self.target_power
        tolerance = max(5.0, 0.05 * target) if target > 0 else 5.0

        in_zone_t = 0.0
        dev_sum = 0.0
        dev_t = 0.0
        valid_t = 0.0
        res_sum = 0.0
        res_t = 0.0

        for s in trace:
            dt = float(s.get("dt") or 0.0)
            power = s.get("p")
            tgt = s.get("target") or target
            if power is None or not tgt:
                continue
            valid_t += dt
            dev = abs(power - tgt)
            dev_sum += dev * dt
            dev_t += dt
            if dev <= tolerance:
                in_zone_t += dt
            res = s.get("res")
            if res is not None:
                res_sum += res * dt
                res_t += dt

        # 同样用"有数据的秒数"当分母。否则一次蓝牙掉线就会让平均功率和
        # 同一份报告里的做功、区间占比对不上——它们都是用有效样本算的。
        avg_power = (round(self._power_integral / self._sampled_s, 1)
                     if self._sampled_s > 1.0 else None)
        # 标准化功率建立在 30 秒滑动窗口上。训练时长还不到两个窗口时，窗口只增不减，
        # 早期的爬升段会被严重放大，算出来的 NP 甚至可能低于平均功率——那是个
        # 误导人的数字，不如不显示。
        np_value = None
        if self._np_count > 0 and self.active_s >= 60.0:
            np_value = round((self._np_sum / self._np_count) ** 0.25, 1)
        avg_cadence = (round(self._cadence_integral / self._cadence_time, 1)
                       if self._cadence_time > 0 else None)

        # 心率：平均/最大从 trace 里算（心率可能中途才有带子，用实际采到的样本）
        # 再筛一道：老报告里可能已经存着 0（那是修复前写进去的），
        # 不能让一条 0 就撑起一个"心率"区块
        hr_samples = [float(s["hr"]) for s in trace if s.get("hr") is not None
                      and HR_MIN_PLAUSIBLE <= float(s["hr"]) <= HR_MAX_PLAUSIBLE]
        hr_stats = None
        if hr_samples:
            band = self._hr_settings()
            zones = hr_zones(band.get("max_hr"), band.get("rest_hr"),
                             band.get("zone_mode") or "max")
            counts = [0.0] * len(zones) if zones else []
            for s in trace:
                bpm = s.get("hr")
                if bpm is None or not zones:
                    continue
                dt = float(s.get("dt") or 0.0)
                for i, z in enumerate(zones):
                    top = z["max_bpm"] if z["max_bpm"] is not None else float("inf")
                    if z["min_bpm"] <= bpm < top:
                        counts[i] += dt
                        break
            total = sum(counts)
            avg_hr = (round(self._hr_integral / self._hr_time, 1)
                      if self._hr_time > 0 else None)
            hr_stats = {
                "avg_bpm": avg_hr,
                "max_bpm": round(self._hr_max, 0) if self._hr_max else None,
                "sample_count": len(hr_samples),
                "sampled_s": round(self._hr_time, 1),
                "source": self.hr_source or "",
                "rr_seen": bool(self.rr_intervals),
                "zones": ([{"code": z["code"], "name": z["name"], "label": z["label"],
                            "min_bpm": z["min_bpm"], "max_bpm": z["max_bpm"],
                            "seconds": round(c, 1),
                            "pct": (round(100.0 * c / total, 1) if total > 0 else 0.0)}
                           for z, c in zip(zones, counts)] if zones else []),
                "zone_mode": (band.get("zone_mode") or "max") if zones else None,
                "max_hr": band.get("max_hr"),
                "rest_hr": band.get("rest_hr"),
                "cap_active": bool(band.get("hr_limit_enabled")),
                "cap_bpm": band.get("hr_limit_bpm"),
                "cap_note": self._hr_cap_note,
            }
        mad = round(dev_sum / dev_t, 1) if dev_t > 0 else None
        in_zone_pct = round(100.0 * in_zone_t / valid_t, 1) if valid_t > 0 else None
        dev_from_target = round(avg_power - target, 1) if avg_power is not None else None

        # 机械做功换算成消耗：按人体做功效率约 23% 折算。常说的"kJ 约等于 kcal"
        # 就是这个换算的近似结果，这里把假设写清楚。
        efficiency = 0.23
        kcal = round(self.energy_kj / efficiency / 4.184, 1) if self.energy_kj else 0.0

        # FTP 测试结果。测试的"完成"含义和普通训练不同：坡道测试踩到力竭本身就是
        # 正常结束，用 active_s >= duration_s 去判断会永远显示"提前结束"。
        test_result = self._compute_test_result(trace)
        if test_result is not None:
            completed = bool(test_result["valid"])
        else:
            # 两头都要满足：计时走满，而且**确实采到了**这段时长。
            # 只看 active_s 的话，中途蓝牙掉线、后面的时间全是空的，报告照样写
            # "完成目标时长"。留 2%（最少 5 秒）的余量，免得几次短暂掉数据就把
            # 一次完整训练判成提前结束。
            slack = max(5.0, 0.02 * self.duration_s)
            completed = (self.active_s >= self.duration_s - 2.0
                         and self._sampled_s >= self.duration_s - slack)

        intervals = self._interval_breakdown(trace)
        # 间歇训练单独给出"高强度段达标率"。把恢复段混进去算整体达标率是没有意义的
        # ——恢复段的目标是降下来，而功率物理上降不了那么快——所以那个数字会很惨，
        # 却完全反映不出你高强度段踩得好不好。
        work_valid = sum(r["valid_s"] for r in intervals if r["kind"] == "work")
        work_in_zone = sum(r["in_zone_s"] for r in intervals if r["kind"] == "work")
        work_in_zone_pct = (round(100.0 * work_in_zone / work_valid, 1)
                            if work_valid > 0 else None)

        return {
            "reason": reason,
            "completed": completed,
            "test_result": test_result,
            "plan_name": self.plan_name,
            "is_interval": len(self.plan) > 1,
            # 历史报告里只需要"是不是测试"，结果本身在 test_result 里；
            # 实时读数（live_test）和档位进度属于实时界面，不该进历史记录
            "is_test": bool(self._test_meta),
            "test_id": (self._test_meta or {}).get("test_id"),
            "step_count": len(self.plan),
            # 控功率方式在什么时候、因为什么切换过。排查"阻力为什么突然变了"
            # 这类问题必须靠它——只看最终模式是查不出来的。
            "mode_changes": list(self._mode_log),
            "config": dict(self.workout_config),
            "trainer_name": getattr(self.trainer, "name", "") or "",
            "erg_mode": self.active_erg_mode,
            "erg_mode_label": self._mode_label(),
            "requested_erg_mode": self.erg_mode,
            "target_power": round(target, 1),
            "planned_s": round(self.duration_s, 1),
            "actual_s": round(self.active_s, 1),
            "sampled_s": round(self._sampled_s, 1),
            "skipped_s": round(max(0.0, self.elapsed_s - self.active_s), 1),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "avg_power": avg_power,
            "max_power": round(self.max_power, 1),
            "normalized_power": np_value,
            "avg_cadence": avg_cadence,
            "max_cadence": round(self._cadence_max, 1) if self._cadence_max else None,
            "heart_rate": hr_stats,
            "energy_kj": round(self.energy_kj, 1),
            "energy_kcal_est": kcal,
            "energy_kcal_basis": "按 {}% 做功效率折算".format(int(efficiency * 100)),
            "distance_m": round(self.distance_m, 1),
            "in_zone_pct": in_zone_pct,
            "work_in_zone_pct": work_in_zone_pct,
            "in_zone_tolerance_w": round(tolerance, 1),
            "mean_abs_deviation_w": mad,
            "target_deviation_w": dev_from_target,
            "avg_resistance_raw": round(res_sum / res_t, 1) if res_t > 0 else None,
            "controller_k": (round(self.controller.k_hat, 5)
                             if (self.controller and self.controller.k_hat) else None),
            "distribution": self._power_distribution(trace),
            "intervals": intervals,
            "trace": self._downsample(trace),
        }

    def _interval_breakdown(self, trace: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按段统计，让用户看清楚每一组到底踩成什么样。

        间歇训练最有价值的反馈粒度就是"每组"：整体平均功率会把一个踩崩的第 3 组
        和踩得漂亮的第 5 组平均掉，看不出问题在哪。

        每段开头会跳过一小段"过渡期"再统计。原因是物理上的：飞轮有惯性，目标功率
        从 262W 切到 125W 时，实际功率需要几秒才能降下来；短恢复段（比如 Tabata 的
        10 秒）如果把这头几秒算进去，"达标率"会低得毫无参考价值。控功率的闭环本来
        就按同样的道理留了 settle 时间，这里保持一致。
        """
        if len(self.plan) <= 1:
            return []

        by_step: Dict[int, List[Dict[str, Any]]] = {}
        for point in trace:
            by_step.setdefault(int(point.get("step", 0)), []).append(point)

        rows: List[Dict[str, Any]] = []
        for index, step in enumerate(self.plan):
            points = by_step.get(index)
            if not points:
                continue

            duration = float(step.get("duration_s", 0))
            # 过渡期：最多 5 秒，且不超过该段时长的四分之一
            settle = min(5.0, 0.25 * duration)
            step_start = (self._step_starts[index]
                          if index < len(self._step_starts) else 0.0)

            actual = 0.0
            valid = 0.0
            in_zone = 0.0
            powers: List[float] = []
            for point in points:
                dt = float(point.get("dt") or 0.0)
                actual += dt
                power = point.get("p")
                if power is None:
                    continue
                if float(point.get("t", 0.0)) - step_start < settle:
                    continue        # 还在过渡期，不参与判定
                # 用逐点的目标值而不是该步的最终目标：中途手动调过功率时，
                # 前后两段本来就该按各自当时的目标来判定是否达标
                target = float(point.get("target") or step.get("target_power") or 0)
                if target <= 0:
                    continue
                tolerance = max(5.0, 0.05 * target)
                valid += dt
                powers.append(power)
                if abs(power - target) <= tolerance:
                    in_zone += dt

            # 达标率只对高强度段有意义。恢复段的目标是"降下来"，而飞轮有惯性，
            # 几秒的恢复期内功率物理上就降不到目标值；拿"有没有踩到 125W"去评判
            # 恢复段只会得到一片 0%，既不公平也会把整体达标率整个拖垮。
            is_work = step.get("kind") == "work"
            rows.append({
                "index": index,
                "name": step.get("name"),
                "kind": step.get("kind"),
                "zone": step.get("zone"),
                "pct_ftp": step.get("pct_ftp"),
                "planned_s": round(duration, 1),
                "actual_s": round(actual, 1),
                "settle_s": round(settle, 1),
                "valid_s": round(valid, 1),
                "in_zone_s": round(in_zone, 1),
                "target_power": round(float(step.get("target_power", 0)), 1),
                "avg_power": round(sum(powers) / len(powers), 1) if powers else None,
                "in_zone_pct": (round(100.0 * in_zone / valid, 1)
                                if (valid > 0 and is_work) else None),
                "work_position": self._work_position.get(index),
            })
        return rows

    @staticmethod
    def _power_distribution(trace: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按目标功率的百分比分档统计停留时间。"""
        bands = [
            ("低于 90%", 0.0, 0.90),
            ("90–97%", 0.90, 0.97),
            ("97–103%", 0.97, 1.03),
            ("103–110%", 1.03, 1.10),
            ("高于 110%", 1.10, float("inf")),
        ]
        seconds = [0.0] * len(bands)
        for s in trace:
            power = s.get("p")
            tgt = s.get("target")
            dt = float(s.get("dt") or 0.0)
            if power is None or not tgt:
                continue
            ratio = power / tgt
            for i, (_, lo, hi) in enumerate(bands):
                if lo <= ratio < hi:
                    seconds[i] += dt
                    break
        total = sum(seconds) or 1.0
        return [
            {"label": bands[i][0],
             "seconds": round(seconds[i], 1),
             "pct": round(100.0 * seconds[i] / total, 1)}
            for i in range(len(bands))
        ]

    @staticmethod
    def _downsample(trace: List[Dict[str, Any]], limit: int = 600
                    ) -> List[Dict[str, Any]]:
        """把轨迹压缩到大约 limit 个点，供前端画曲线。

        用的是每个桶取"最小值和最大值"的做法，而不是简单地每隔几个点抽一个——
        抽点会把短暂的功率尖峰整个丢掉，而尖峰恰恰是看训练质量时最该保留的信息。
        """
        n = len(trace)
        # 保留阻力和踏频：报告是事后复盘问题的唯一依据，把这两列丢掉的话
        # "阻力为什么突然掉了"这类问题就完全无从查起
        def simple(s):
            return {"t": s["t"], "p": s["p"], "target": s["target"],
                    "res": s.get("res"), "cadence": s.get("cadence"),
                    "hr": s.get("hr")}
        if n <= limit:
            return [simple(s) for s in trace]

        buckets = max(1, limit // 2)
        size = n / float(buckets)
        out: List[Dict[str, Any]] = []
        for b in range(buckets):
            lo = int(b * size)
            hi = min(n, int((b + 1) * size))
            if lo >= hi:
                continue
            chunk = trace[lo:hi]
            if len(chunk) == 1:
                out.append(simple(chunk[0]))
                continue
            lowest = min(chunk, key=lambda s: s["p"])
            highest = max(chunk, key=lambda s: s["p"])
            ordered = [lowest, highest] if lowest["t"] <= highest["t"] else [highest, lowest]
            out.extend(simple(s) for s in ordered)
        return out

    async def _cancel_task(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        # 时长跑满时是主循环自己调用 stop()，此时不能取消自己——否则后面的状态
        # 更新会被 CancelledError 打断，"已完成"永远写不进去。主循环在 stop()
        # 返回后会自行 return。
        if task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("结束训练任务时出错")

    async def aclose(self) -> None:
        await self._cancel_task()

    # ------------------------------------------------------------------
    # 运行中调整
    # ------------------------------------------------------------------

    async def set_target_power(self, watts: float) -> None:
        """调整当前这一步的目标功率。

        间歇训练里只改当前这一段，不会动整个计划——正在踩阈值组的时候按下
        +10W，意思显然是"这一组再狠一点"，而不是"后面所有组都改"。
        """
        self.target_power = float(max(0.0, watts))
        if 0 <= self.step_index < len(self.plan):
            step = self.plan[self.step_index]
            step["target_power"] = int(round(self.target_power))
            # 手动改过之后就不再是原方案的比例了，标记清楚免得总结里对不上
            step["pct_ftp"] = None
        if self.controller is not None:
            self.controller.target_power = self.target_power
        # 目标变了，"功率稳但偏"的观察窗口和正在进行/已完成的探测结论都不再成立
        # （否则探测期间那 20 秒还会继续按旧目标下发）。
        self._native_watch.clear()
        self._native_probe = None
        if self.state == STATE_RUNNING and self.active_erg_mode == ERG_FTMS:
            try:
                await self._send_target_power(self.target_power)
            except TrainerError as exc:
                self.error_message = str(exc)
        await self._emit()

    async def set_manual_resistance(self, raw: int) -> None:
        """手动拧阻力（调试/自由骑行用），会脱离闭环控制。"""
        self.controller = None
        self._set_mode(ERG_RESISTANCE, "手动设置阻力")
        self.resistance_raw = float(raw)
        await self.trainer.set_resistance_raw(raw)
        await self._emit()

    # ------------------------------------------------------------------
    # 间歇计划的推进
    # ------------------------------------------------------------------

    def _step_index_at(self, elapsed: float) -> int:
        """用二分查找定位某个时刻属于第几个步骤。"""
        if not self._step_bounds:
            return 0
        # bisect_right：时刻正好等于某步终点时，算作下一步的开始
        idx = bisect_right(self._step_bounds, elapsed)
        return max(0, min(len(self._step_bounds) - 1, idx))

    def _sync_step(self) -> bool:
        """把当前步骤对齐到已用时间。返回 True 表示刚刚发生了切换。"""
        idx = self._step_index_at(self.elapsed_s)
        if idx == self.step_index:
            return False
        self.step_index = idx
        self.target_power = float(self.plan[idx]["target_power"])
        # 目标刚变过，功率必然要几秒才跟上。把降级的观察窗口清零，
        # 重新给固件一次机会——否则频繁换段的课表会被误判成"不响应目标功率"。
        self._native_watch.clear()
        # 探测的结论建立在"目标没变"的前提上，目标一变就得作废重来
        self._native_probe = None
        # 换段了：心率保护的"原目标"和已下调状态都该重新开始
        self._reset_hr_cap()
        return True

    async def _apply_current_target(self, announce: bool = False) -> None:
        """把当前步骤的目标功率下发给骑行台。"""
        if self.active_erg_mode == ERG_FREE:
            # 自由骑行下发的是"档位"而不是"功率"，换段时什么都不用做——
            # 全程保持同一个阻力，骑手自己配速
            pass
        elif self.active_erg_mode == ERG_RESISTANCE:
            if self.controller is not None:
                # 闭环控制器跟踪新目标即可，它会自己把阻力重新收敛过去
                self.controller.target_power = self.target_power
        else:
            try:
                await self._send_target_power(self.target_power)
            except TrainerError as exc:
                self.error_message = str(exc)

        if announce:
            step = self.plan[self.step_index]
            zone = step.get("zone")
            self._notify("interval", "进入「{}」{}W{}（{}/{}）".format(
                step.get("name", "步骤"), int(self.target_power),
                " · " + zone if zone else "",
                self.step_index + 1, len(self.plan)))

    async def skip_step(self, delta: int = 1) -> None:
        """跳到上一步 / 下一步（按组跳过，跳过的时间不计入训练时长）。"""
        if not self.plan or self.state not in (STATE_RUNNING, STATE_PAUSED):
            return
        target = max(0, min(len(self.plan) - 1, self.step_index + int(delta)))
        if target == self.step_index:
            return
        self.elapsed_s = self._step_starts[target]
        self._last_dt_time = asyncio.get_event_loop().time()
        self.step_index = target
        self.target_power = float(self.plan[target]["target_power"])
        self._native_watch.clear()
        await self._apply_current_target()
        self._notify("interval", "跳到「{}」{}W".format(
            self.plan[target].get("name", "步骤"), int(self.target_power)))
        await self._emit()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            while True:
                await asyncio.sleep(self.TICK)
                now = loop.time()
                dt = 0.0 if self._last_dt_time is None else now - self._last_dt_time
                self._last_dt_time = now
                if dt < 0:
                    dt = 0.0

                self._ingest_trainer_data(now, dt)

                if self.state == STATE_PAUSED:
                    await self._emit()
                    continue
                if self.state != STATE_RUNNING:
                    # 已经结束或被丢弃了，循环该退出。留在这儿空转没有意义，
                    # 而且它会在下一次训练时和新循环一起向骑行台发指令。
                    return

                self.elapsed_s += dt
                self.active_s += dt
                # 间歇训练：到点就自动切到下一步
                if self._sync_step():
                    await self._apply_current_target(announce=True)
                if self.current_power is not None:
                    self._power_integral += self.current_power * dt
                    self._sampled_s += dt
                    self.energy_kj = self._power_integral / 1000.0
                    if self.current_power > self.max_power:
                        self.max_power = self.current_power
                if self.current_speed is not None:
                    self._distance_accum += self.current_speed / 3.6 * dt
                    self.distance_m = self._distance_accum
                if self.current_cadence is not None:
                    self._cadence_integral += self.current_cadence * dt
                    self._cadence_time += dt
                    if self.current_cadence > self._cadence_max:
                        self._cadence_max = self.current_cadence
                if self.current_hr is not None:
                    self._hr_integral += self.current_hr * dt
                    self._hr_time += dt
                    if self.current_hr > self._hr_max:
                        self._hr_max = self.current_hr

                # 逐秒记一个点，训练结束后用来画曲线和算区间分布。
                # 1Hz 对总结来说足够，60 分钟也才 3600 个点。
                if self.current_power is not None and self._last_trace is not None \
                        and (now - self._last_trace) >= 1.0:
                    self._trace.append({
                        "t": round(self.elapsed_s, 1),
                        "dt": round(now - self._last_trace, 2),
                        "p": round(self.current_power, 1),
                        "target": round(self.target_power, 1),
                        "step": self.step_index,
                        "cadence": (round(self.current_cadence, 1)
                                    if self.current_cadence is not None else None),
                        "res": (round(self.resistance_raw, 1)
                                if self.resistance_raw is not None else None),
                        # 心率也要逐秒记：报告里的"心率漂移/解耦率"全靠这一列，
                        # 而这两个指标恰恰是最需要事后复盘的东西
                        "hr": (round(self.current_hr, 1)
                               if self.current_hr is not None else None),
                    })
                    self._last_trace = now

                self._update_normalized_power(now)

                # 心率上限保护：安全刹车，独立于控功率方式
                self._check_hr_cap(now)

                if self.active_erg_mode == ERG_FTMS:
                    await self._tick_native(now)
                    # 坡道测试：固件在按 ERG 加载，但我们还要盯着骑手什么时候踩不动了
                    if self._is_ramp_test and await self._check_ramp_failure(now):
                        return
                elif self.active_erg_mode == ERG_RESISTANCE:
                    await self._tick_closed_loop(now)
                # 自由骑行：不控任何东西，功率完全由骑手配速

                if self.elapsed_s >= self.duration_s:
                    self.elapsed_s = self.duration_s
                    await self.stop(reason="完成目标时长")
                    return

                await self._emit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 兜底，避免后台任务静默死掉
            log.exception("训练循环异常")
            self.state = STATE_ERROR
            self.error_message = "训练循环异常：{}".format(exc)
            await self._emit()

    def monitor_tick(self) -> None:
        """空闲（没在训练）时也把设备数据读进来。

        为什么需要：训练主循环只在训练进行时跑，所以"还没开始训练"的时候
        `current_power`/`current_hr` 一直是 None——而设备页恰恰要在**开始训练之前**
        显示"功率/踏频在动""心率在跳"，否则用户没法确认到底连上没有。
        服务器那边每 0.25 秒的广播循环顺手调一次这里。

        除了取数什么都不做：不累计做功、不推进计时、不调阻力、不判心率上限。
        """
        if self.state in (STATE_RUNNING, STATE_PAUSED):
            return                       # 训练中主循环自己会读，别读两遍
        now = asyncio.get_event_loop().time()
        self._ingest_trainer_data(now, 0.0)
        if self.on_update is not None:
            try:
                self.on_update(self.snapshot())
            except Exception:
                log.exception("状态回调出错")

    def _ingest_trainer_data(self, now: float, dt: float) -> None:
        """读取骑行台最新数据，并判断数据是不是还"活着"。

        这里有个容易踩的坑：`trainer.latest` 是**跨帧累积**的字典，设备一旦停推，
        里面的 `power_w` 仍然在，`"power_w" in latest` 永远为真。所以判活必须看
        "最后一次收到数据的时刻"，不能看字段在不在。否则设备静默之后，程序会拿
        一个冻结的功率值一直积分、还继续按它调阻力，而界面上一切正常。
        """
        latest = getattr(self.trainer, "latest", None) or {}
        last_data_time = getattr(self.trainer, "last_data_time", 0.0) or 0.0

        if last_data_time:
            stale_for = time.monotonic() - last_data_time
        else:
            # 还没收到过任何数据：从训练开始算起，给设备一点启动时间
            stale_for = time.monotonic() - (self._mono_started_at or time.monotonic())
        self._stale = stale_for > STALE_AFTER_S

        if self._stale:
            if self.no_data_since is None:
                self.no_data_since = now
            # 数据不可信时不要留着旧值当"当前功率"：否则能量/距离会继续累积、
            # 闭环控制器会按冻结的功率把阻力一路推到量程端点
            self.current_power = None
            self.current_cadence = None
            self.current_speed = None
            self._power_window.clear()
            self._control_window.clear()
            return

        if self.no_data_since is not None:
            # 刚从"数据陈旧"恢复：和暂停一样，断链那段时间不能算成骑行了。
            # 旧值会让恢复后的第一个 trace 点带上一整段空档的 dt。
            self._last_trace = now
        self.no_data_since = None
        if "power_w" in latest:
            self.current_power = float(latest["power_w"])
            self._power_window.append((now, self.current_power))
            self._control_window.append((now, self.current_power))
        if "cadence_rpm" in latest:
            self.current_cadence = float(latest["cadence_rpm"])
        if "speed_kmh" in latest:
            self.current_speed = float(latest["speed_kmh"])
        # 心率：独立心率带优先，没有才退回骑行台自带的那个字段。
        # 心率带单独判活（它是一台独立设备，骑行台断链不代表心率断了）。
        hr_latest, hr_fresh = self._heart_rate_reading()
        if hr_fresh is not None:
            self.current_hr = hr_fresh
            self.hr_contact = (hr_latest or {}).get("contact")
        elif "heart_rate_bpm" in latest:
            bpm = float(latest["heart_rate_bpm"])
            # 骑行台自带的心率字段（FTMS Indoor Bike Data 里的那个）经常是 **0**：
            # 固件没接心率带时也照发这个字段。0 不是"心率很低"，是"没有数据"，
            # 以前直接收下，于是报告里出现"平均心率 0 bpm、100% 在 Z1"的假区块，
            # 实时界面那个心率格子也会显示 0。这里和独立心率带那条路用同一套范围。
            if HR_MIN_PLAUSIBLE <= bpm <= HR_MAX_PLAUSIBLE:
                self.current_hr = bpm
                self.hr_source = "trainer"
            else:
                self.current_hr = None
                self.hr_source = None
            self.rr_intervals = 0
        else:
            self.current_hr = None
            self.hr_source = None
            self.rr_intervals = 0
        if "resistance_level" in latest and self.controller is None:
            self.resistance_raw = float(latest["resistance_level"]) * 10.0

        cutoff = now - self.POWER_WINDOW_S
        while self._power_window and self._power_window[0][0] < cutoff:
            self._power_window.popleft()
        cutoff = now - self.CONTROL_WINDOW_S
        while self._control_window and self._control_window[0][0] < cutoff:
            self._control_window.popleft()

    def _reset_hr_cap(self) -> None:
        self._hr_over_since = None
        self._hr_ok_since = None
        self._hr_last_action = 0.0
        self._hr_step_locked = False
        self._hr_cap_note = ""

    def _heart_rate_reading(self) -> Tuple[Optional[Dict[str, Any]], Optional[float]]:
        """读独立心率带的最新值。返回 ``(latest, bpm)``，无效时为 ``(None, None)``。

        ``latest`` 一起返回，是因为电极接触状态这类信息只有原始帧里有。
        （这里以前返回的是 ``(bpm, bpm)`` 这种没意义的元组，调用方拿第一个
        去当字典用就会炸——正是下面 hr_contact 那行的来历。）

        单独判活是必须的：心率带是第二个外设，它自己会飘出范围、会没电、会被别的
        App 抢走。骑行台那条链路好着的时候，完全可能心率带已经十分钟没数据了——
        这时候界面不能继续显示一个冻结的心率值（和当初功率踩过的坑一模一样）。
        """
        hr = self.heart_rate
        if hr is None or not getattr(hr, "connected", False):
            return None, None
        latest = getattr(hr, "latest", None) or {}
        bpm = latest.get("heart_rate_bpm")
        if bpm is None:
            return None, None
        stale_after = getattr(hr, "HR_STALE_AFTER_S", 10.0)
        last = getattr(hr, "last_data_time", 0.0) or 0.0
        if last and (time.monotonic() - last) > stale_after:
            return None, None
        rrs = latest.get("rr_intervals_ms") or []
        self.rr_intervals = len(rrs)
        self.hr_source = "strap"
        return latest, float(bpm)

    def _hr_settings(self) -> Dict[str, Any]:
        """这次训练用的心率设置（开训时从 config 里冻结下来）。"""
        return self.hr_config or {}

    def _hr_zone(self, bpm: Optional[float]) -> Optional[Dict[str, Any]]:
        from .heartrate import hr_zone_of
        cfg = self._hr_settings()
        if bpm is None or not cfg.get("max_hr"):
            return None
        return hr_zone_of(bpm, cfg.get("max_hr"), cfg.get("rest_hr"),
                          cfg.get("zone_mode") or "max")

    def _check_hr_cap(self, now: float) -> None:
        """心率上限保护：超上限就往下压一点功率。

        这是**安全刹车，不是控制方式**。心率滞后 30~60 秒，拿它做闭环必然振荡，
        所以这里的做法是"高了就分几步往下让，低了就回到原计划"，不是伺服。

        自由骑行模式下不动功率（程序本来就不控制功率），只提示。
        """
        cfg = self._hr_settings()
        limit = cfg.get("hr_limit_bpm")
        if not cfg.get("hr_limit_enabled") or not limit or self.current_hr is None:
            self._hr_over_since = None
            self._hr_ok_since = None
            return

        floor = self._hr_floor_power()
        if self.current_hr >= limit:
            self._hr_ok_since = None
            if self._hr_over_since is None:
                self._hr_over_since = now
            if now - self._hr_over_since < HR_LIMIT_HOLD_S:
                return
            if now - self._hr_last_action < HR_LIMIT_COOLDOWN_S:
                return
            if self.target_power <= floor + 0.5:
                if not self._hr_step_locked:
                    self._hr_step_locked = True
                    self._hr_cap_note = "心率持续偏高，目标已降到本段下限 {}W".format(
                        int(round(floor)))
                    self._notify("hr-limit", self._hr_cap_note + "，建议降低强度或停下来缓缓")
                return
            new_target = max(floor, self.target_power * HR_LIMIT_STEP_RATIO)
            self._hr_last_action = now
            self._apply_hr_cap(new_target, now)
            return

        # 心率回落到上限以下：连续一段时间都正常就回到原计划
        self._hr_over_since = None
        if self.target_power < self._hr_plan_power() - 0.5:
            if self._hr_ok_since is None:
                self._hr_ok_since = now
            if now - self._hr_ok_since >= HR_LIMIT_RECOVER_S:
                self._hr_ok_since = None
                self._hr_step_locked = False
                self._apply_hr_cap(self._hr_plan_power(), now, restoring=True)
        else:
            self._hr_ok_since = None

    def _hr_plan_power(self) -> float:
        """本段原本的目标功率（心率保护是临时下调，不是改计划）。"""
        if 0 <= self.step_index < len(self.plan):
            return float(self.plan[self.step_index].get("target_power") or self.target_power)
        return float(self.target_power)

    def _hr_floor_power(self) -> float:
        return self._hr_plan_power() * HR_LIMIT_FLOOR_RATIO

    def _apply_hr_cap(self, watts: float, now: float, restoring: bool = False) -> None:
        self.target_power = float(max(0.0, watts))
        if self.controller is not None:
            self.controller.target_power = self.target_power
        if self.active_erg_mode == ERG_RESISTANCE:
            pass                      # 闭环控制器下一拍自己会跟过去
        elif self.active_erg_mode == ERG_FTMS:
            asyncio.ensure_future(self._send_target_power(self.target_power))
        elif self.active_erg_mode == ERG_FREE:
            # 自由骑行不控功率，能量化地"降强度"做不到，只能提示
            self._notify("hr-limit", "心率超过上限 {}bpm，建议降低配速".format(
                int(self._hr_settings().get("hr_limit_bpm") or 0)))
            return
        # 目标变了，降级观察窗口和探测结论都作废（和手动改目标同理）
        self._native_watch.clear()
        self._native_probe = None
        self._hr_cap_note = ("心率回落到上限以下，已恢复本段目标 {}W".format(
            int(round(self.target_power))) if restoring else
            "心率超过上限，目标已下调到 {}W".format(int(round(self.target_power))))
        self._notify("hr-limit", self._hr_cap_note)

    def _avg_power(self) -> Optional[float]:
        if not self._power_window:
            return None
        total = sum(p for _, p in self._power_window)
        return total / len(self._power_window)

    def _control_power(self) -> Optional[float]:
        """闭环控制器用的功率：取最近 CONTROL_WINDOW_S 秒的平均。"""
        if not self._control_window:
            return self._avg_power()
        total = sum(p for _, p in self._control_window)
        return total / len(self._control_window)

    def _update_normalized_power(self, now: float) -> None:
        if self.current_power is None:
            return
        self._np_window.append((now, self.current_power))
        cutoff = now - 30.0
        while self._np_window and self._np_window[0][0] < cutoff:
            self._np_window.popleft()
        # 判据必须是"窗口真的横跨了 30 秒"，不能写 len(...) >= 30 —— 采样是 10Hz，
        # 那样只有 3 秒就开始计入了，而 3 秒的增长型窗口远比 30 秒平均剧烈，
        # 会让 NP 系统性偏高（实测短训练能高 10% 以上）。
        span = self._np_window[-1][0] - self._np_window[0][0]
        if span >= 30.0 - self.TICK * 2:
            avg30 = sum(p for _, p in self._np_window) / len(self._np_window)
            self._np_sum += avg30 ** 4
            self._np_count += 1

    async def _tick_native(self, now: float) -> None:
        """原生 ERG：定期重发目标功率，并判断固件是不是在装样子。

        规范里控制权一旦取得就持续有效，但现实中不少固件会超时释放，重发是最省事
        也最保险的做法（Zwift 同样会周期性下发）。
        """
        if now - self._last_keepalive >= self.POWER_KEEPALIVE_S:
            self._last_keepalive = now
            try:
                await self._send_target_power(self._native_send_target())
            except TrainerError as exc:
                self.error_message = str(exc)

        # 数据陈旧时没有可信的功率，判断和探测都无从谈起
        if self._stale:
            if self._native_probe is not None:
                # 半途丢了数据，这次探测的结果不可信，作废（重发目标功率的
                # keepalive 会在两秒内把真实目标补回去）
                self._native_probe = None
            return

        # 正在探测固件响不响应目标功率，别在探测期间做别的判断
        if self._native_probe is not None:
            await self._tick_native_probe(now)
            return

        if self._should_fall_back(now):
            caps = getattr(self.trainer, "capabilities", {}) or {}
            if caps.get("has_control_point") and (
                    caps.get("supports_resistance_target")
                    or not caps.get("declared_targets")):
                await self._start_native_probe(now)

    def _native_send_target(self) -> float:
        """原生 ERG 当前真正该下发的目标功率（探测期间是探测值）。"""
        if self._native_probe is not None:
            return float(self._native_probe["probe_target"])
        return self.target_power

    async def _start_native_probe(self, now: float) -> None:
        """降级前的最后一道确认：把目标往下压一档，看固件到底跟不跟。

        这是降级前必须拿到的实证。"功率很稳但偏离目标"看起来很像固件装死，
        但动感单车的功率往往是固件用「阻力 × 踏频」算出来的：骑手踏频一稳，
        功率就能稳得毫无波动，看起来像固件不理人，实际上它可能只是反应慢。
        切换模式会把控制权整个夺走（骑手能立刻感觉到阻力跳变），代价太大。

        探测要问的不是"固件有没有反应"，而是"它能不能把功率压到设定值"——
        因为不少廉价固件把 Set Target Power 实现成"把瓦数换算成一次阻力档位就
        完事"，这种固件对目标变化是有反应的，但功率永远停在当前踏频骑出来的
        那个值上。只测"有没有反应"会被它骗过去。
        """
        baseline = self._avg_power()
        if baseline is None:
            return

        # 往下压而不是往上抬：功率偏高时向下能测出反应，功率偏低时向下同样
        # 能测出反应（固件真在跟就会把功率进一步拉低），而且对骑手更友好。
        probe_target = max(30.0, self.target_power - self.PROBE_DELTA_W)
        self._native_probe = {
            "started": now,
            "baseline": baseline,
            "probe_target": probe_target,
        }
        # 探测期间旧的观察窗口没有意义了
        self._native_watch.clear()
        try:
            await self._send_target_power(probe_target)
        except TrainerError as exc:
            self.error_message = str(exc)
        self.last_command_note = "正在探测骑行台是否响应目标功率……"
        self._log_mode_event(
            "probe",
            "怀疑固件没在跟目标功率，做一次阶跃探测：目标 {:.0f}W → {:.0f}W，"
            "看功率能不能收敛到 {:.0f}W，观察 {:.0f} 秒"
            "（探测期间功率会故意低一截，属正常）".format(
                self.target_power, probe_target, probe_target, self.PROBE_WAIT_S))
        self._notify(
            "probe",
            "功率稳在 {:.0f}W 但目标是 {:.0f}W，先做一次探测：临时把目标改成 "
            "{:.0f}W，看它能不能把功率压到那个值（能就继续用原生 ERG）".format(
                baseline, self.target_power, probe_target))

    async def _tick_native_probe(self, now: float) -> None:
        probe = self._native_probe
        if probe is None:
            return
        # 骑手停下或踏频过低时功率必然掉，这时候的"没反应"不能算数
        if self.current_cadence is None or self.current_cadence < 25.0:
            self._native_probe = None
            return
        if now - probe["started"] < self.PROBE_WAIT_S:
            return

        avg = self._avg_power()
        self._native_probe = None
        if avg is None:
            return

        # 判据是"**收敛到**新目标"，不是"动了一下"。
        # 这一条很关键：不少廉价固件把 Set Target Power 实现成"把瓦数换算成一次
        # 阻力档位就完事"，不再回头看。这种固件对目标变化**是有反应的**（阻力会
        # 跟着变、功率也会跟着变），但它永远停在"当前踏频恰好骑出来的那个功率"上。
        # 只测"有没有反应"会被它骗过去——真正要问的是"它能不能把功率压到你要的值"。
        probe_target = probe["probe_target"]
        tolerance = max(self.FALLBACK_MIN_ERROR_W,
                        self.FALLBACK_TOL_RATIO * probe_target)
        if abs(avg - probe_target) <= tolerance:
            self._probe_cooldown_until = now + self.PROBE_COOLDOWN_S
            self._native_watch.clear()
            self.last_command_note = "探测通过：骑行台确实在跟目标功率，继续用原生 ERG"
            self._log_mode_event(
                "probe-ok",
                "探测通过：目标改为 {:.0f}W 后功率收敛到 {:.0f}W（容差 ±{:.0f}W），"
                "确认固件真的能把功率压到设定值，继续用原生 ERG".format(
                    probe_target, avg, tolerance))
            self._notify(
                "probe-ok",
                "探测结果：目标改为 {:.0f}W 后功率收敛到 {:.0f}W，"
                "说明固件真的能把功率压到设定值，继续用原生 ERG".format(
                    probe_target, avg))
            try:
                await self._send_target_power(self.target_power)
            except TrainerError as exc:
                self.error_message = str(exc)
            return

        # 阶跃下发了 20 秒，功率既没动、也没收敛到新目标——这次有证据了，可以降级
        self._notify(
            "fallback",
            "骑行台不能把功率压到设定值（目标从 {:.0f}W 改为 {:.0f}W 后，功率停在 "
            "{:.0f}W，容差 ±{:.0f}W），已自动切换到闭环阻力控制".format(
                self.target_power, probe_target, avg, tolerance))
        await self._enter_resistance_mode(
            initial=True, reason="自动降级：目标功率阶跃探测无响应")

    def _should_fall_back(self, now: float) -> bool:
        """判断骑行台是否"收下目标功率但根本不执行"——只是立案，不定罪。"""
        # 用户明确选了"原生 ERG"，就尊重他的选择，绝不自动切换。
        # 自动降级是 auto 模式为了兼容性兜的底，不该反过来覆盖明确的选择。
        if self.erg_mode != ERG_AUTO:
            return False
        # FTP 测试期间绝对不做自动降级：坡道测试每级都在抬高目标，功率本来就
        # 会滞后几秒，正好落进"稳但偏"的判据里。一旦降级，阻力被锁死，
        # 坡道就再也加不上去，整个测试就废了。
        if self._test_meta:
            return False
        # 刚刚探测过、确认固件是在正常工作的，一段时间内别再去打扰它
        if now < self._probe_cooldown_until:
            return False
        if self.elapsed_s < self.FALLBACK_GRACE_S:
            return False

        avg = self._avg_power()
        if avg is None or self.current_cadence is None or self.current_cadence < 25.0:
            # 没在踩踏就无从判断，清空观察窗口重新开始
            self._native_watch.clear()
            return False

        tolerance = max(self.FALLBACK_MIN_ERROR_W,
                        self.FALLBACK_TOL_RATIO * self.target_power)
        self._native_watch.append((now, avg))
        while self._native_watch and self._native_watch[0][0] < now - self.FALLBACK_HOLD_S:
            self._native_watch.popleft()

        span = self._native_watch[-1][0] - self._native_watch[0][0]
        if span < self.FALLBACK_HOLD_S - 2.0 or len(self._native_watch) < 5:
            return False

        powers = [p for _, p in self._native_watch]
        # 期间只要有任意一次进入容差，就说明固件确实在闭环工作
        if any(abs(p - self.target_power) <= tolerance for p in powers):
            return False

        mean = sum(powers) / len(powers)
        stdev = (sum((p - mean) ** 2 for p in powers) / len(powers)) ** 0.5
        return stdev < self.FALLBACK_STABLE_W

    async def _tick_closed_loop(self, now: float) -> None:
        if self.controller is None or self._resistance_blocked:
            return
        # 数据陈旧时不要调阻力：那等于按一个冻结的功率值瞎推，会把阻力推到端点
        if self._stale:
            self.last_command_note = "收不到骑行台数据，暂停调整阻力"
            return
        if now - self._last_resistance_adj < self.RESISTANCE_INTERVAL_S:
            return
        self._last_resistance_adj = now

        previous = self.controller.level_raw
        avg = self._control_power()
        new_level, reason = self.controller.update(avg, self.current_cadence, now)
        self.last_command_note = reason
        if new_level is None:
            return
        try:
            await self.trainer.set_resistance_raw(int(round(new_level)))
            self.resistance_raw = new_level
            self._resistance_failures = 0
        except TrainerError as exc:
            # 指令没被接受，就把控制器心里的档位退回去。不然它以为自己已经加到
            # 13 档、设备其实还在原地，两边越差越远，接下来只会一直下发更大的、
            # 同样被拒绝的档位（实测会一路爬到量程上限）。
            self.controller.level_raw = previous
            self.error_message = str(exc)
            self._resistance_failures += 1
            if self._resistance_failures >= 3:
                self._resistance_blocked = True
                self.last_command_note = "连续调阻力被拒绝，已停止调整：{}".format(exc)
                self._notify("resistance-failed",
                             "连续 3 次调整阻力都被骑行台拒绝，已停止调整：{}".format(exc))
                await self._emit()

    # ------------------------------------------------------------------
    # FTP 测试
    # ------------------------------------------------------------------

    @property
    def _is_ramp_test(self) -> bool:
        return bool(self._test_meta) and self._test_meta.get("result_kind") == RESULT_RAMP

    async def _enter_free_mode(self) -> None:
        """自由骑行：设一次阻力就撒手，功率完全由骑手配速。

        这是 FTP 测试里 20 分钟 / 8 分钟计时段唯一正确的做法。用 ERG 的话，
        骑行台会把功率锁死在你设定的值上，你测出来的是"那个设定值"，不是你的能力。
        """
        self.last_command_note = "自由骑行：程序不控制功率，自己配速（可微调阻力）"
        try:
            await self.trainer.set_resistance_raw(int(round(self.free_resistance)))
        except TrainerError as exc:
            self.error_message = str(exc)

    async def set_free_resistance(self, raw: int) -> None:
        """自由骑行中微调阻力档位。"""
        self.free_resistance = float(max(0, min(255, int(raw))))
        if self.active_erg_mode == ERG_FREE:
            try:
                await self.trainer.set_resistance_raw(int(round(self.free_resistance)))
                self.resistance_raw = self.free_resistance
                self.last_command_note = "阻力档位已调到 {:.1f}".format(self.free_resistance / 10.0)
            except TrainerError as exc:
                self.error_message = str(exc)
        await self._emit()

    async def _check_ramp_failure(self, now: float) -> bool:
        """坡道测试里判断骑手是不是踩不动了。返回 True 表示测试已结束。

        判据有两条，命中任一条并持续一小段时间就算力竭：功率掉到目标的 80% 以下，
        或者踏频掉到 50 以下。换级后的前几秒功率还在爬，必须先给宽限期，
        否则每一级刚开始都会被误判成力竭。
        """
        if not self.plan:
            return False
        step = self.plan[min(self.step_index, len(self.plan) - 1)]
        if step.get("kind") != KIND_TEST:
            self._ramp_low_since = None
            return False
        if self.current_power is None or self.current_cadence is None:
            return False        # 没数据时交给"数据陈旧"那条逻辑去处理

        step_elapsed = self.elapsed_s - self._step_starts[self.step_index]
        if step_elapsed < RAMP_STEP_GRACE_S:
            self._ramp_low_since = None
            return False

        target = float(step.get("target_power") or 0)
        avg = self._avg_power() or self.current_power
        weak = target > 0 and avg < target * RAMP_FAIL_POWER_RATIO
        too_slow = self.current_cadence < RAMP_FAIL_CADENCE

        if not (weak or too_slow):
            self._ramp_low_since = None
            return False

        if self._ramp_low_since is None:
            self._ramp_low_since = now
            return False

        hold = RAMP_FAIL_CADENCE_HOLD_S if too_slow else RAMP_FAIL_HOLD_S
        if now - self._ramp_low_since < hold:
            return False

        reason = "踏频掉到 {:.0f}rpm".format(self.current_cadence) if too_slow \
            else "功率只到目标 {:.0f}W 的 {:.0f}%".format(target, (avg / target * 100) if target else 0)
        self._notify("test", "检测到力竭（{}），坡道测试结束".format(reason))
        await self.stop(reason="力竭，测试结束")
        return True

    # ------------------------------------------------------------------
    # 测试结果
    # ------------------------------------------------------------------

    @staticmethod
    def _best_window_power(trace: List[Dict[str, Any]], window_s: float) -> Optional[float]:
        """轨迹里最好的一个固定**时长**窗口的平均功率（按 dt 加权）。

        这里必须用时间而不是点数。轨迹是按"距上一个点超过 1 秒"记一个点，
        所以点的间距并不总是 1 秒：主循环被骑行台应答堵住时会拉到两三秒，
        暂停或数据断链之后还会更长。以前写成"取 window_s 个点"，那个"1 分钟"
        窗口实际可能横跨两三分钟，坡道测试的 FTP = 最好 1 分钟 × 75% 也就跟着
        偏。改成按 t 滑动、用 dt 加权之后，短窗口也能正确计算。
        """
        if window_s <= 0:
            return None
        pts = [(float(p["t"]), float(p["p"]), float(p.get("dt") or 1.0))
               for p in trace if p.get("p") is not None and p.get("t") is not None]
        if len(pts) < 2:
            return None

        best: Optional[float] = None
        lo = 0
        acc = 0.0          # 功率 × dt 的累加
        acc_t = 0.0        # dt 的累加
        for hi in range(len(pts)):
            t_hi, p_hi, dt_hi = pts[hi]
            acc += p_hi * dt_hi
            acc_t += dt_hi
            # 窗口右端固定在 t_hi。轨迹里一个点代表**它之前**的 dt 秒
            # （dt = 距上一个点的间隔），也就是区间 [t-dt, t)——
            # 所以"整个区间都在窗口左边"的点要丢掉。
            left = t_hi - window_s
            while lo < hi and pts[lo][0] <= left:
                acc -= pts[lo][1] * pts[lo][2]
                acc_t -= pts[lo][2]
                lo += 1
            # 最左边那个点通常只有一部分落在窗口里，按比例扣掉伸出去的部分，
            # 这样窗口就是精确的 window_s 秒，而不是"window_s 加上小半个采样间隔"
            # （多算一个点会让坡道测试的"最好 1 分钟"偏低一档）。
            t_lo, p_lo, dt_lo = pts[lo]
            inside = max(0.0, min(dt_lo, t_lo - max(t_lo - dt_lo, left)))
            adj_acc = acc - p_lo * (dt_lo - inside)
            adj_t = acc_t - (dt_lo - inside)
            # 只有窗口真的横跨了 window_s 才算数（开头那段凑不满）
            if adj_t >= window_s * 0.98:
                avg = adj_acc / adj_t
                if best is None or avg > best:
                    best = avg
        return best

    def _best_test_segment_power(self, trace: List[Dict[str, Any]]) -> Optional[float]:
        """所有被标记为 test 的段落里，平均功率最高的那一段。"""
        runs: List[List[float]] = []
        current: List[float] = []
        current_index: Optional[int] = None
        for point in trace:
            idx = int(point.get("step", -1))
            is_test = (0 <= idx < len(self.plan)
                       and self.plan[idx].get("kind") == KIND_TEST
                       and point.get("p") is not None)
            if not is_test:
                if current:
                    runs.append(current)
                    current = []
                    current_index = None
                continue
            if current_index is not None and idx != current_index and current:
                runs.append(current)
                current = []
            current_index = idx
            current.append(float(point["p"]))
        if current:
            runs.append(current)
        if not runs:
            return None
        # 太短的片段不算（比如刚进计时段就没数据了）
        usable = [r for r in runs if len(r) >= 30] or runs
        return max(sum(r) / len(r) for r in usable)

    def _live_test_reading(self) -> Optional[Dict[str, Any]]:
        """测试进行中的实时读数——界面要显示"现在大概是多少"。

        坡道测试给的是"到目前为止推算出的 FTP"，20/8 分钟测试给的是当前计时段的
        实时平均功率。这是全程最该盯着的一个数。
        """
        meta = self._test_meta
        if not meta or not self._trace:
            return None
        multiplier = float(meta.get("multiplier") or 0)
        if meta.get("result_kind") == RESULT_RAMP:
            best = self._best_window_power(self._trace, 60.0)
            if best is None:
                return None
            return {"kind": "projected_ftp", "label": "推算 FTP",
                    "value": int(round(best * multiplier)),
                    "source": round(best, 1)}
        # 计时段：算当前这一段的实时平均
        if self.step_index not in self._test_indices:
            return None
        powers = [float(p["p"]) for p in self._trace
                  if int(p.get("step", -1)) == self.step_index and p.get("p") is not None]
        if not powers:
            return None
        avg = sum(powers) / len(powers)
        return {"kind": "segment_avg", "label": "本段实时平均",
                "value": round(avg, 1), "projected_ftp": int(round(avg * multiplier))}

    def _compute_test_result(self, trace: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """按测试方案的规则算出 FTP。"""
        meta = self._test_meta
        if not meta or not trace:
            return None

        kind = meta.get("result_kind")
        multiplier = float(meta.get("multiplier") or 0)
        label = meta.get("source_label") or ""

        if kind == RESULT_RAMP:
            source = self._best_window_power(trace, 60.0)
            # 有效性：至少踩过 RAMP_MIN_STEPS 级，否则这次测试没意义
            test_indices = [i for i, s in enumerate(self.plan)
                            if s.get("kind") == KIND_TEST]
            reached = 0
            for i in test_indices:
                if any(int(p.get("step", -1)) == i for p in trace):
                    reached += 1
            total_levels = len(test_indices)
            # 把全部级数都踩完了却没力竭 → 坡道设得太短，算出来的 FTP 只反映
            # "课表没跑完"，不反映能力。这种情况必须标记为无效，否则会给出一个
            # 看着挺正经、其实毫无意义的数字。
            ran_out = reached >= total_levels
            valid = bool(source) and reached >= RAMP_MIN_STEPS and not ran_out
            if ran_out:
                detail = ("跑完了全部 {} 级还没力竭——坡道太短。"
                          "把「每分钟递增」调小、或「最多级数」调多，再测一次。").format(total_levels)
            else:
                detail = "踩到第 {} 级 / 共 {} 级就力竭了".format(reached, total_levels)
        elif kind == RESULT_BEST_SEGMENT:
            source = self._best_test_segment_power(trace)
            planned = sum(float(s["duration_s"]) for s in self.plan
                          if s.get("kind") == KIND_TEST)
            actual = sum(float(p.get("dt") or 0) for p in trace
                         if 0 <= int(p.get("step", -1)) < len(self.plan)
                         and self.plan[int(p.get("step", -1))].get("kind") == KIND_TEST)
            covered = (actual / planned) if planned > 0 else 0.0
            valid = bool(source) and covered >= 0.9
            detail = "计时段完成度 {:.0f}%".format(covered * 100)
        else:
            return None

        if not source:
            return None

        return {
            "protocol": self.plan_name,
            "test_id": meta.get("test_id"),
            "result_kind": kind,
            "source_label": label,
            "source_power": round(source, 1),
            "multiplier": multiplier,
            "ftp": int(round(source * multiplier)),
            "valid": bool(valid),
            "detail": detail,
            "note": meta.get("note") or "",
        }

    def _initial_resistance(self, r_min: float, r_max: float) -> float:
        """闭环控制的起始档位。

        优先用骑行台**当前实际所在的档位**，而不是量程的中间值。

        这一点很关键：从原生 ERG 切过来、或者暂停之后继续，阻力都从原地接手，
        不会先掉一大截再慢慢爬回来。以前固定用「量程的四分之一」起步，于是每次
        切换档位都会从 1/4 量程重新爬——用户的感觉就是"阻力突然从 17 掉到 6，
        然后一分多钟才慢慢回到 17"。
        """
        if self.resistance_raw is not None:
            return max(float(r_min), min(float(r_max), float(self.resistance_raw)))
        return (float(r_min) + float(r_max)) / 4.0

    async def _enter_resistance_mode(self, initial: bool,
                                     reason: str = "") -> None:
        caps = getattr(self.trainer, "capabilities", {}) or {}
        rng = caps.get("resistance_range") or {}
        r_min = int(rng.get("min_raw", rng.get("min", 0)) or 0)
        r_max = int(rng.get("max_raw", rng.get("max", 255)) or 255)
        if r_max <= r_min:
            r_min, r_max = 0, 255

        self._set_mode(ERG_RESISTANCE,
                       reason or ("切入闭环阻力控制" if initial else "暂停后继续闭环阻力控制"))
        # 重新进入这个模式就再给一次机会（之前熔断可能只是骑行台一时没应答）
        self._resistance_failures = 0
        self._resistance_blocked = False

        if self.controller is None:
            self.controller = ResistancePowerController(self.target_power, r_min, r_max)
            start_level = self._initial_resistance(r_min, r_max)
            self.controller.seed(start_level)
            self.last_command_note = "闭环阻力控制已就绪（量程 {}-{}，起始档位 {:.1f}）".format(
                r_min, r_max, start_level / 10.0)
        else:
            # 已经有控制器（暂停继续、或再次进入）：保留它已经收敛的档位和 k 估计，
            # 只把目标更新一下。重建控制器等于把好不容易调好的阻力白扔掉。
            self.controller.target_power = self.target_power

        try:
            await self.trainer.set_resistance_raw(int(round(self.controller.level_raw or r_min)))
            self.resistance_raw = self.controller.level_raw
        except TrainerError as exc:
            self.error_message = str(exc)
        if not initial:
            self._last_resistance_adj = asyncio.get_event_loop().time()

    async def _send_target_power(self, watts: float) -> None:
        await self.trainer.set_target_power(int(round(watts)))

    # ------------------------------------------------------------------
    # 对外状态
    # ------------------------------------------------------------------

    def _notify(self, kind: str, message: str) -> None:
        log.info("%s: %s", kind, message)
        if self.on_event is not None:
            try:
                self.on_event(kind, message)
            except Exception:
                log.exception("事件回调出错")

    def snapshot(self) -> Dict[str, Any]:
        np_value = None
        # 门槛必须和 _build_summary 一致（60 秒）：30 秒滑动窗口刚起步时窗口只增
        # 不减，早期爬升段会被严重放大，算出来的 NP 甚至可能低于平均功率。
        if self._np_count > 0 and self.active_s >= 60.0:
            np_value = round((self._np_sum / self._np_count) ** 0.25, 1)

        avg_power = None
        # 分母必须有数据的秒数，不能用 active_s：断链那段时间 active_s 还在走，
        # 而积分停了，拿 active_s 当分母等于把平均值往 0 摊。
        if self._sampled_s > 1.0:
            avg_power = round(self._power_integral / self._sampled_s, 1)
        else:
            avg_power = self._avg_power()
            avg_power = round(avg_power, 1) if avg_power is not None else None

        remaining = max(0.0, self.duration_s - self.elapsed_s)
        progress = 0.0 if self.duration_s <= 0 else min(1.0, self.elapsed_s / self.duration_s)

        avg5 = self._avg_power()

        # 当前步骤（间歇训练用）
        step: Dict[str, Any] = {}
        step_duration = self.duration_s
        step_elapsed = self.elapsed_s
        if self.plan:
            step = self.plan[min(self.step_index, len(self.plan) - 1)]
            step_duration = float(step.get("duration_s", self.duration_s))
            step_start = (self._step_starts[self.step_index]
                          if self.step_index < len(self._step_starts) else 0.0)
            step_elapsed = max(0.0, self.elapsed_s - step_start)
        next_step = (self.plan[self.step_index + 1]
                     if self.plan and self.step_index + 1 < len(self.plan) else None)

        return {
            "state": self.state,
            "target_power": round(self.target_power, 1),
            "duration_s": round(self.duration_s, 1),
            "elapsed_s": round(self.elapsed_s, 1),
            "active_s": round(self.active_s, 1),
            "skipped_s": round(max(0.0, self.elapsed_s - self.active_s), 1),
            "remaining_s": round(remaining, 1),
            "progress": round(progress, 4),
            "erg_mode": self.erg_mode,
            "active_erg_mode": self.active_erg_mode,
            "erg_mode_label": self._mode_label(),
            "power": round(self.current_power, 1) if self.current_power is not None else None,
            "power_avg5": round(avg5, 1) if avg5 is not None else None,
            "power_avg": avg_power,
            "power_max": round(self.max_power, 1),
            "normalized_power": np_value,
            "cadence": round(self.current_cadence, 1) if self.current_cadence is not None else None,
            "speed": round(self.current_speed, 2) if self.current_speed is not None else None,
            "heart_rate": self.current_hr,
            "hr_source": self.hr_source,
            "hr_stale": bool(self.heart_rate is not None
                             and getattr(self.heart_rate, "connected", False)
                             and self.current_hr is None),
            "hr_zone": self._hr_zone(self.current_hr),
            "hr_zones": hr_zones((self._hr_settings() or {}).get("max_hr"),
                                 (self._hr_settings() or {}).get("rest_hr"),
                                 (self._hr_settings() or {}).get("zone_mode") or "max"),
            "hr_limit": {
                "enabled": bool((self._hr_settings() or {}).get("hr_limit_enabled")),
                "bpm": (self._hr_settings() or {}).get("hr_limit_bpm"),
                "note": self._hr_cap_note,
            },
            "rr_intervals": self.rr_intervals,
            # 电极接触状态（很多带子会报）。干电极读数不稳，这条能让用户先排除"没戴好"
            "hr_contact": self.hr_contact if self.hr_source == "strap" else None,
            "distance_m": round(self.distance_m, 1),
            "energy_kj": round(self.energy_kj, 1),
            "resistance_raw": round(self.resistance_raw, 1) if self.resistance_raw is not None else None,
            "controller_k": round(self.controller.k_hat, 5) if (self.controller and self.controller.k_hat) else None,
            "command_note": self.last_command_note,
            "error": self.error_message,
            "stale_data": self._stale,
            "sampled_s": round(self._sampled_s, 1),
            "summary": self.summary,
            # ---- 间歇训练 ----
            "plan_name": self.plan_name,
            "is_interval": len(self.plan) > 1,
            # ---- FTP 测试 ----
            "is_test": bool(self._test_meta),
            "test_id": (self._test_meta or {}).get("test_id"),
            "test_self_paced": bool((self._test_meta or {}).get("self_paced")),
            "test_multiplier": (self._test_meta or {}).get("multiplier"),
            "free_resistance": (round(self.free_resistance, 1)
                                if self.active_erg_mode == ERG_FREE else None),
            "test_level": (self._test_indices.index(self.step_index) + 1
                           if self.step_index in self._test_indices else None),
            "test_level_count": len(self._test_indices),
            "live_test": self._live_test_reading(),
            "step_index": self.step_index,
            "step_count": len(self.plan),
            "step_name": step.get("name"),
            "step_kind": step.get("kind"),
            "step_zone": step.get("zone"),
            "step_pct_ftp": step.get("pct_ftp"),
            "step_duration_s": round(step_duration, 1),
            "step_elapsed_s": round(step_elapsed, 1),
            "step_remaining_s": round(max(0.0, step_duration - step_elapsed), 1),
            "work_index": self._work_position.get(self.step_index),
            "work_count": len(self._work_indices),
            "next_step_name": next_step.get("name") if next_step else None,
            "next_step_power": next_step.get("target_power") if next_step else None,
            "plan": [
                {"name": s.get("name"), "kind": s.get("kind"),
                 "duration_s": round(float(s.get("duration_s", 0)), 1),
                 "target_power": s.get("target_power")}
                for s in self.plan
            ],
            "trainer": self.trainer.state() if hasattr(self.trainer, "state") else {},
        }

    async def _emit(self) -> None:
        if self.on_update is not None:
            try:
                self.on_update(self.snapshot())
            except Exception:
                log.exception("状态回调出错")
