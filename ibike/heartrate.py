"""心率带（标准蓝牙心率服务 0x180D）的客户端与解析。

为什么单独一个模块、而不是塞进 ``ftms.py``：心率服务和 FTMS 是两套独立的蓝牙
规范，只是碰巧都跟训练有关。心率带也不属于"骑行台"，它是**第二个独立外设**：
不用配对、不用申请控制权、和骑行台各连各的，所以生命周期也完全独立
（骑行台断了心率带照样在跑，反之亦然）。

顺带说清楚一件事：FTMS 的 Indoor Bike Data 里也有一个心率字段，程序一直会解析它。
但那条路只在"骑行台自己带心率传感器"时才有数据（比如某些动感单车），普通骑行台
永远是空的。所以心率有**两个来源**，程序里按"独立心率带优先"来取。

``0x2A37`` 的帧格式（蓝牙规范 Heart Rate Measurement）：

    字节 0      标志位
                bit0 = 心率值宽度（0: uint8，1: uint16）
                bit1-2 = 传感器接触状态
                bit3 = 有"能量消耗"字段（uint16，kJ）
                bit4 = 有 RR 间期字段（uint16 × N，单位 1/1024 秒）
    字节 1..    心率值（按 bit0 决定 1 或 2 字节）
    可选        能量消耗（2 字节）
    可选        RR 间期（2 字节 × N，一直读到帧尾）

RR 间期是逐拍的间隔，有它才能算 HRV。程序目前**只把它记录下来**（存进 trace 的
原始心跳间隔数量），不做 HRV 计算——因为那需要静息条件下单独采集，属于另一个功能。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional

from bleak import BleakClient

from .ble import RawDevice

log = logging.getLogger("ibike.heartrate")

# 标准蓝牙心率服务
HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_CHAR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
# 电池服务（顺带读一下电量，知道带子还剩多少）
BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_CHAR_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

# 超过这么久没收到心跳数据就认为这条链路不可信
HR_STALE_AFTER_S = 10.0

# 重连节奏：心率带飘出范围很常见，断了要自己找回
RECONNECT_DELAYS_S = (2.0, 5.0, 10.0, 20.0, 30.0)

# 物理上不可能的心率：用来识别"带子没戴好/电极没湿"而不是照单全收
HR_MIN_PLAUSIBLE = 25
HR_MAX_PLAUSIBLE = 250


class HeartRateError(RuntimeError):
    """连接或订阅心率带时的可预期错误，上层直接把 message 展示给用户。"""


@dataclass
class HrDeviceInfo:
    """扫描到的一根心率带。"""

    address: str
    name: str
    rssi: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def parse_heart_rate_measurement(data: bytes) -> Dict[str, Any]:
    """解析 0x2A37 的一帧，返回 ``{"heart_rate_bpm", "rr_intervals_ms", ...}``。

    解析不出来（帧截断）时返回能解析出的那部分，绝不抛异常——通知回调里抛异常
    会被 bleak 吞掉甚至断掉订阅，那就成"心率莫名其妙不刷新"了。
    """
    raw = bytes(data)
    out: Dict[str, Any] = {"heart_rate_bpm": None, "rr_intervals_ms": [],
                           "energy_kj": None, "contact": None}
    if not raw:
        return out

    flags = raw[0]
    wide = bool(flags & 0x01)
    contact_bits = (flags >> 1) & 0x03
    out["contact"] = {0: "不支持检测", 1: "不支持检测", 2: "支持但未接触",
                      3: "已接触"}.get(contact_bits)
    pos = 1

    if wide:
        if pos + 2 > len(raw):
            return out
        out["heart_rate_bpm"] = int.from_bytes(raw[pos:pos + 2], "little")
        pos += 2
    else:
        if pos + 1 > len(raw):
            return out
        out["heart_rate_bpm"] = raw[pos]
        pos += 1

    if flags & 0x08:                    # 能量消耗
        if pos + 2 <= len(raw):
            out["energy_kj"] = int.from_bytes(raw[pos:pos + 2], "little")
        pos += 2

    if flags & 0x10:                    # RR 间期：剩下的全是，每个 2 字节
        rrs = []
        while pos + 2 <= len(raw):
            ticks = int.from_bytes(raw[pos:pos + 2], "little")
            pos += 2
            # 单位是 1/1024 秒
            ms = ticks * 1000.0 / 1024.0
            if 200.0 <= ms <= 3000.0:   # 30~300bpm 之外的当作坏值丢掉
                rrs.append(round(ms, 1))
        out["rr_intervals_ms"] = rrs

    hr = out["heart_rate_bpm"]
    if hr is not None and not (HR_MIN_PLAUSIBLE <= hr <= HR_MAX_PLAUSIBLE):
        out["heart_rate_bpm"] = None    # 明显是坏帧，不往外传
        out["implausible"] = hr
    return out


# --------------------------------------------------------------------------
# 心率区间
# --------------------------------------------------------------------------

# 两种算法都用同一套 5 区标签，只是"百分比"的算法不同
HR_ZONE_BANDS = [
    ("Z1", "恢复", 0.00, 0.60),
    ("Z2", "有氧", 0.60, 0.70),
    ("Z3", "节奏", 0.70, 0.80),
    ("Z4", "阈值", 0.80, 0.90),
    ("Z5", "最大", 0.90, float("inf")),
]

HR_MODE_MAX = "max"           # %HRmax：需要最大心率
HR_MODE_RESERVE = "reserve"   # 储备心率（Karvonen）：还需要静息心率，更准


def hr_zones(max_hr: Optional[float], rest_hr: Optional[float] = None,
             mode: str = HR_MODE_MAX) -> Optional[List[Dict[str, Any]]]:
    """算出 5 个心率区间的绝对 bpm 边界。参数不全时返回 None。"""
    if not max_hr or max_hr <= 0:
        return None
    use_reserve = mode == HR_MODE_RESERVE and rest_hr and 0 < rest_hr < max_hr
    bands = []
    for code, name, lo, hi in HR_ZONE_BANDS:
        if use_reserve:
            base = float(rest_hr)
            span = float(max_hr) - base
            lo_bpm = base + span * lo
            hi_bpm = base + span * hi
        else:
            lo_bpm = float(max_hr) * lo
            hi_bpm = float(max_hr) * hi
        bands.append({
            "code": code,
            "name": name,
            "label": "{} {}".format(code, name),
            "min_bpm": round(lo_bpm, 1),
            "max_bpm": (round(hi_bpm, 1) if math.isfinite(hi_bpm) else None),
        })
    return bands


def hr_zone_of(bpm: Optional[float], max_hr: Optional[float],
               rest_hr: Optional[float] = None,
               mode: str = HR_MODE_MAX) -> Optional[Dict[str, Any]]:
    """一个心率值落在哪个区。算不出来（没设最大心率/值无效）时返回 None。"""
    if bpm is None or not max_hr or max_hr <= 0:
        return None
    use_reserve = mode == HR_MODE_RESERVE and rest_hr and 0 < rest_hr < max_hr
    if use_reserve:
        span = float(max_hr) - float(rest_hr)
        pct = (float(bpm) - float(rest_hr)) / span if span > 0 else 0.0
    else:
        pct = float(bpm) / float(max_hr)
    for code, name, lo, hi in HR_ZONE_BANDS:
        if lo <= pct < hi:
            return {"code": code, "name": name, "pct": round(pct, 4),
                    "label": "{} {}".format(code, name)}
    return {"code": "Z1", "name": "恢复", "pct": round(pct, 4), "label": "Z1 恢复"}


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class HeartRateClient:
    """一根心率带的连接与订阅。

    接口刻意和 ``TrainerClient`` 保持一致的形状（``latest`` / ``last_data_time`` /
    ``connected`` / ``connect`` / ``disconnect``），这样训练会话那边可以用同一套
    "读最新一帧 + 判活"的代码同时处理两个数据源。
    """

    def __init__(self,
                 on_data: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_event: Optional[Callable[[str, str], None]] = None,
                 auto_reconnect: bool = True) -> None:
        self.on_data = on_data
        self.on_event = on_event
        self.auto_reconnect = auto_reconnect

        self.name = ""
        self.address = ""
        self.connected = False
        self.latest: Dict[str, Any] = {}
        # 和 TrainerClient 一样用单调时钟（见 trainer.py 里的说明）
        self.last_data_time = 0.0
        self.battery: Optional[int] = None
        self.rr_seen = False            # 这根带子到底会不会给 RR 间期
        self.last_rr_count = 0

        self._client: Optional[BleakClient] = None
        self._chars: Dict[str, Any] = {}
        self._reconnect_task: Optional[asyncio.Task] = None
        self._closing = False
        self._disconnect_reason = ""

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------

    @staticmethod
    def classify_straps(raw: List[RawDevice]) -> List[HrDeviceInfo]:
        """从一次广播扫描的结果里筛出心率带，按信号强度排序。

        收两类：广播里带 0x180D 的（标准情况），以及名字像心率带的。
        后者不能省——有些便宜的带子（以及部分固件版本）广播里不带服务列表，
        只凭 0x180D 过滤会把它们整个漏掉。
        """
        devices: List[HrDeviceInfo] = []
        for d in raw:
            uuids = [u.lower() for u in d.service_uuids]
            if HR_SERVICE not in uuids and not _looks_like_strap(d.name):
                continue
            devices.append(HrDeviceInfo(address=d.address, name=d.name or "(无名)",
                                        rssi=d.rssi))
        devices.sort(key=lambda d: -(d.rssi or -999))
        return devices

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------

    async def connect(self, address: str, name: str = "", timeout: float = 20.0) -> None:
        """连接并订阅。``name`` 由调用方从扫描结果里带过来。

        以前这里连上之后又扫 1.5 秒去广播里找设备名——那既慢，又因为外面套了
        `except Exception: pass` 而把失败藏起来（名字永远是地址，用户看不出问题）。
        名字本来就是扫描时顺手就有的东西，让调用方传进来即可。
        """
        await self._teardown()
        self._closing = False
        self.address = address
        self._client = BleakClient(address, timeout=timeout,
                                   disconnected_callback=self._on_disconnected)
        try:
            await self._client.connect()
        except Exception as exc:                # noqa: BLE001
            self._client = None
            raise HeartRateError("连接心率带失败：{}".format(exc)) from exc

        self._chars = {c.uuid.lower(): c for c in self._client.services.characteristics.values()} \
            if hasattr(self._client, "services") and self._client.services is not None else {}
        if HR_CHAR_MEASUREMENT not in self._chars:
            # 有些库/设备在连接后立刻拿不到 services，再问一次
            try:
                services = await self._client.get_services()
                self._chars = {c.uuid.lower(): c for c in services.characteristics.values()}
            except Exception:
                pass
        if HR_CHAR_MEASUREMENT not in self._chars:
            await self.disconnect()
            raise HeartRateError(
                "这根设备没有标准心率特征值（0x2A37）——它可能不支持蓝牙心率服务，"
                "或者需要用厂商自己的 App。")

        try:
            await self._client.start_notify(HR_CHAR_MEASUREMENT, self._handle_measurement)
        except Exception as exc:                # noqa: BLE001
            await self.disconnect()
            raise HeartRateError("订阅心率数据失败：{}".format(exc)) from exc

        self.connected = True
        # 名字优先用调用方从扫描结果里带过来的；没有就退回地址（诚实，不编）
        self.name = (name or "").strip() or address
        # 电量是顺带的，失败无所谓
        self.battery = await self._read_battery()
        self.last_data_time = time.monotonic()
        self._emit_event("hr-connected", "心率带已连接：{}".format(self.name or address))
        log.info("心率带已连接：%s（%s）", self.name, address)

    async def _read_battery(self) -> Optional[int]:
        if self._client is None or BATTERY_CHAR_LEVEL not in self._chars:
            return None
        try:
            raw = await self._client.read_gatt_char(BATTERY_CHAR_LEVEL)
            return int(raw[0]) if raw else None
        except Exception:
            return None

    async def disconnect(self) -> None:
        self._closing = True
        task, self._reconnect_task = self._reconnect_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._teardown()
        if self.connected:
            self._emit_event("hr-disconnected", "心率带已断开")
        self.connected = False
        self.latest = {}
        self.last_data_time = 0.0
        self._closing = False

    async def _teardown(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                if client.is_connected:
                    await client.stop_notify(HR_CHAR_MEASUREMENT)
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception:
                log.debug("断开心率带时出错", exc_info=True)
        self.connected = False
        self._chars = {}

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------

    def _handle_measurement(self, _char: Any, data: bytearray) -> None:
        parsed = parse_heart_rate_measurement(bytes(data))
        if parsed.get("heart_rate_bpm") is None:
            if parsed.get("implausible") is not None:
                log.debug("丢掉一个不合理的心率值：%s", parsed["implausible"])
            return
        rrs = parsed.get("rr_intervals_ms") or []
        if rrs:
            self.rr_seen = True
            self.last_rr_count = len(rrs)
        self.latest = {
            "heart_rate_bpm": parsed["heart_rate_bpm"],
            "rr_intervals_ms": rrs,
            "contact": parsed.get("contact"),
            "energy_kj": parsed.get("energy_kj"),
        }
        self.last_data_time = time.monotonic()
        if self.on_data is not None:
            try:
                self.on_data(dict(self.latest))
            except Exception:
                log.exception("心率数据回调出错")

    def _on_disconnected(self, _client: Any) -> None:
        was_connected = self.connected
        self.connected = False
        self.latest = {}
        self.last_data_time = 0.0
        if self._closing:
            return
        if was_connected:
            log.warning("心率带连接断开：%s", self._disconnect_reason or "原因未知")
            self._emit_event("hr-disconnected", "心率带连接断开了，正在尝试重连")
        if self.auto_reconnect and self.address:
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.ensure_future(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """循环重连。心率带飘出范围、电量耗尽、被别的 App 抢走都会走这里。"""
        for delay in RECONNECT_DELAYS_S:
            await asyncio.sleep(delay)
            if self._closing or not self.auto_reconnect:
                return
            try:
                await self.connect(self.address, name=self.name)
                self._emit_event("hr-reconnected", "心率带已重新连接")
                return
            except HeartRateError as exc:
                log.warning("心率带重连失败：%s", exc)
        self._emit_event("hr-reconnect-gave-up", "心率带重连多次失败，请在设置里重新连接")

    def _emit_event(self, kind: str, message: str) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, message)
            except Exception:
                log.exception("心率带事件回调出错")

    def state(self) -> Dict[str, Any]:
        return {
            "kind": "heart_rate",
            "connected": self.connected,
            "name": self.name,
            "address": self.address,
            "battery": self.battery,
            "heart_rate": (self.latest or {}).get("heart_rate_bpm"),
            "contact": (self.latest or {}).get("contact"),
            "rr_seen": self.rr_seen,
            "stale": self.is_stale(),
        }

    def is_stale(self) -> bool:
        if not self.connected or not self.last_data_time:
            return True
        return (time.monotonic() - self.last_data_time) > HR_STALE_AFTER_S


def _looks_like_strap(name: str) -> bool:
    lowered = name.lower()
    return any(k in lowered for k in
               ("hr", "heart", "pulse", "hrm", "magene", "polar", "wahoo",
                "tickr", "coospo", "garmin", "bryton", "h10", "h9", "h64", "h303",
                "h603", "心率"))


# --------------------------------------------------------------------------
# 模拟心率带（没有硬件时用）
# --------------------------------------------------------------------------


class SimulatedHeartRate:
    """模拟一根心率带：心率跟着功率走，用一阶惯性逼近。

    刻意做成"有滞后、有抖动"的样子——真实心率就是这个脾气，如果模拟器给一条
    瞬间跟着功率跳的曲线，界面上和算法里都会得出错误的手感结论。
    """

    def __init__(self, cadence: float = 85.0, rest_hr: float = 60.0,
                 max_hr: float = 190.0, with_rr: bool = True) -> None:
        self.on_data: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_event: Optional[Callable[[str, str], None]] = None
        self.rest_hr = rest_hr
        self.max_hr = max_hr
        self.with_rr = with_rr

        self.name = "模拟心率带"
        self.address = "simulator-hr"
        self.connected = False
        self.latest: Dict[str, Any] = {}
        self.last_data_time = 0.0
        self.battery = 88
        self.rr_seen = with_rr
        self._hr = rest_hr
        self._power = 0.0
        self._publishing = True
        self._task: Optional[asyncio.Task] = None

    async def connect(self, address: str = "simulator-hr", timeout: float = 0.0) -> None:
        self.connected = True
        self.address = address
        self.last_data_time = time.monotonic()
        self._task = asyncio.ensure_future(self._loop())
        if self.on_event:
            self.on_event("hr-connected", "心率带已连接：{}".format(self.name))
        await asyncio.sleep(0.05)

    async def disconnect(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.connected = False
        self.latest = {}
        self.last_data_time = 0.0

    def set_power(self, watts: float) -> None:
        """由模拟骑行台把当前功率喂进来，心率才会跟着动。"""
        self._power = max(0.0, float(watts))

    def set_publishing(self, enabled: bool) -> None:
        self._publishing = enabled

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.25)
                if not self._publishing:
                    continue
                # 心率随功率上升，但远慢于功率（时间常数约 25 秒）
                target = self.rest_hr + (self.max_hr - self.rest_hr) * min(
                    1.0, self._power / 260.0)
                self._hr += (target - self._hr) * (0.25 / 25.0)
                import random
                bpm = int(round(self._hr + random.uniform(-1.5, 1.5)))
                rr = []
                if self.with_rr and bpm > 0:
                    interval = 60000.0 / bpm
                    rr = [round(interval + random.uniform(-8, 8), 1)]
                self.latest = {"heart_rate_bpm": bpm, "rr_intervals_ms": rr,
                               "contact": "已接触", "energy_kj": None}
                self.last_data_time = time.monotonic()
                if self.on_data is not None:
                    try:
                        self.on_data(dict(self.latest))
                    except Exception:
                        log.exception("模拟心率数据回调出错")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("模拟心率循环异常")

    def is_stale(self) -> bool:
        if not self.connected or not self.last_data_time:
            return True
        return (time.monotonic() - self.last_data_time) > HR_STALE_AFTER_S

    def state(self) -> Dict[str, Any]:
        return {
            "kind": "heart_rate",
            "connected": self.connected,
            "name": self.name,
            "address": self.address,
            "battery": self.battery,
            "heart_rate": (self.latest or {}).get("heart_rate_bpm"),
            "contact": (self.latest or {}).get("contact"),
            "rr_seen": self.rr_seen,
            "stale": self.is_stale(),
        }
