"""真实骑行台的 BLE 客户端（基于 bleak / macOS CoreBluetooth）。

对外暴露的方法刻意和 simulator.SimulatedTrainer 保持一致，这样上层的训练会话
和 Web 服务完全不关心背后是真车还是模拟器。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from bleak import BleakClient

from .ble import BleScanError, RawDevice, scan_raw
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from . import ftms

log = logging.getLogger("ibike.trainer")

# 控制点应答超时。正常固件几十毫秒就应答；连续超时说明这个固件压根不回应答，
# 就把等待时间放宽，避免每次都把训练主循环堵住。
CP_RESPONSE_TIMEOUT_S = 2.5
CP_FAST_TIMEOUT_S = 0.4
CP_TIMEOUTS_BEFORE_FAST = 3


@dataclass
class DeviceInfo:
    """扫描到的一台设备，会被直接序列化成 JSON 发给前端。"""

    address: str
    name: str
    rssi: Optional[int] = None
    has_ftms: bool = False
    has_cps: bool = False
    service_uuids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TrainerError(RuntimeError):
    """连接或控制骑行台时的可预期错误，上层直接把 message 展示给用户。"""


class TrainerClient:
    """一台真实智能骑行台的连接与控制。"""

    def __init__(self, on_data: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_event: Optional[Callable[[str, str], None]] = None) -> None:
        self.on_data = on_data
        self.on_event = on_event

        self.client: Optional[BleakClient] = None
        self.device: Optional[BLEDevice] = None
        self.is_simulator = False
        self.name = "未连接"

        # 发现的特征值 UUID
        self._chars: Dict[str, str] = {}
        self._has_indicate_control_point = False
        self.service_map: Dict[str, List[str]] = {}
        self._address: str = ""

        # 能力描述
        self.capabilities: Dict[str, Any] = {}
        self.machine_info: Dict[str, Any] = {}

        # 最近一次合并后的骑行数据
        self.latest: Dict[str, Any] = {}
        self.last_data_time: float = 0.0
        self.connected: bool = False
        self.has_control: bool = False

        self._write_lock = asyncio.Lock()
        self._response_waiters: Dict[int, asyncio.Future] = {}
        # 某个 op 超时之后，到这个时刻之前收到的同类应答一律丢弃。
        # 原因：FTMS 的应答里没有请求序号，只能靠 op code 配对。一次超时之后，
        # 那个迟到的应答会被算到下一条同 op 的指令头上——实测会把一条本来成功的
        # 指令报成"Operation Failed"，反过来也能用迟到的 Success 掩盖真正的拒绝。
        self._late_until: Dict[int, float] = {}
        self._response_timeout = CP_RESPONSE_TIMEOUT_S
        self._cp_timeouts = 0
        self._cp_responded = False
        self._disconnect_reason: Optional[str] = None
        self._intentional_disconnect = False

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------

    @staticmethod
    def classify_trainers(raw: List[RawDevice]) -> List[DeviceInfo]:
        """从一次广播扫描的结果里筛出骑行台候选。

        返回**全部有名字的设备**（不只是 FTMS 的），因为有些国产骑行台的名字里
        完全看不出是骑行台，用户需要靠名字自己认——按 FTMS 优先排序方便挑选。
        """
        devices: List[DeviceInfo] = []
        for d in raw:
            if not d.name:
                continue
            uuids = [u.lower() for u in d.service_uuids]
            devices.append(
                DeviceInfo(
                    address=d.address,
                    name=d.name,
                    rssi=d.rssi,
                    has_ftms=ftms.FTMS_SERVICE in uuids,
                    has_cps=ftms.CPS_SERVICE in uuids,
                    service_uuids=uuids,
                )
            )
        devices.sort(key=lambda d: (not d.has_ftms, not d.has_cps, -(d.rssi or -999)))
        return devices

    @staticmethod
    async def scan(timeout: float = 8.0) -> List[DeviceInfo]:
        """单独扫描一次（命令行 --scan 用）。Web 界面走的是合并扫描。"""
        try:
            raw = await scan_raw(timeout)
        except BleScanError as exc:
            raise TrainerError("扫描失败：{}".format(exc)) from exc
        return TrainerClient.classify_trainers(raw)

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------

    async def connect(self, address: str, timeout: float = 20.0) -> None:
        """连接并完成服务发现、能力探测与数据订阅。"""
        await self._cleanup_client()

        log.info("正在连接 %s ...", address)
        client = BleakClient(address, disconnected_callback=self._handle_disconnect,
                             timeout=timeout)
        try:
            await client.connect()
        except (BleakError, asyncio.TimeoutError, OSError) as exc:
            raise TrainerError("连接失败：{}".format(exc)) from exc

        self.client = client
        self._address = getattr(client, "address", address)
        self._intentional_disconnect = False
        self.connected = True
        self.name = address
        self.latest = {}
        self.last_data_time = 0.0

        try:
            await self._discover()
            await self._subscribe()
            await self._read_capabilities()
        except Exception:
            await self._cleanup_client()
            raise

        self._emit_event("connected", "已连接 {}".format(self.name))

    async def _cleanup_client(self) -> None:
        client = self.client
        self.client = None
        self.connected = False
        self.has_control = False
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _discover(self) -> None:
        """找出我们关心的特征值，并识别厂商/型号。"""
        assert self.client is not None
        services = self.client.services
        if services is None:
            raise TrainerError("无法读取 GATT 服务列表")

        wanted = [
            ftms.CHAR_FITNESS_MACHINE_FEATURE,
            ftms.CHAR_INDOOR_BIKE_DATA,
            ftms.CHAR_SUPPORTED_POWER_RANGE,
            ftms.CHAR_SUPPORTED_RESISTANCE_LEVEL_RANGE,
            ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT,
            ftms.CHAR_FITNESS_MACHINE_STATUS,
            ftms.CHAR_CYCLING_POWER_MEASUREMENT,
            ftms.CHAR_BATTERY_LEVEL,
            ftms.CHAR_MANUFACTURER_NAME,
            ftms.CHAR_MODEL_NUMBER,
            ftms.CHAR_FIRMWARE_REVISION,
        ]
        self._chars = {}
        self.service_map: Dict[str, List[str]] = {}
        for service in services:
            char_uuids = []
            for char in service.characteristics:
                uuid = char.uuid.lower()
                char_uuids.append(uuid)
                if uuid in wanted:
                    self._chars[uuid] = char.uuid
                    if uuid == ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT:
                        self._has_indicate_control_point = "indicate" in char.properties
            self.service_map[service.uuid.lower()] = char_uuids

        if ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT not in self._chars:
            log.warning("该设备没有 FTMS 控制点(0x2AD9)，只能读取数据，无法控阻力")

    def describe_gatt(self) -> str:
        """把设备实际暴露的服务/特征值整理成一行，便于排查和反馈问题。"""
        parts = []
        for service_uuid, chars in self.service_map.items():
            short = service_uuid[4:8] if service_uuid.endswith("-0000-1000-8000-00805f9b34fb") else service_uuid
            char_shorts = [
                c[4:8] if c.endswith("-0000-1000-8000-00805f9b34fb") else c for c in chars
            ]
            parts.append("{}[{}]".format(short, ",".join(char_shorts)))
        return " ".join(parts)

    def _missing_ftms_error(self) -> "TrainerError":
        """连接上了、但拿不到骑行数据时的报错。

        这是最容易被误判成"连上了却用不了"的情况，所以错误信息必须自带诊断数据，
        让用户可以直接把服务列表发出来定位问题。
        """
        found = self.describe_gatt() or "（没有读到任何服务）"
        has_control = ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT in self._chars
        if has_control:
            head = "这台设备有 FTMS 控制点，但没有提供骑行数据（功率/踏频），无法做 ERG 控功率。"
        else:
            head = "这台设备没有提供标准的 FTMS 服务（0x1826），无法读取功率也无法控制阻力。"
        return TrainerError(
            "{}它实际暴露的服务是：{}。"
            "请把这段信息发给我，或运行 python3 scan.py --pick 查看完整结构。".format(head, found)
        )

    async def _subscribe(self) -> None:
        assert self.client is not None

        # 没有功率来源就完全没法做 ERG。必须在连接阶段就拦下来并说清楚原因，
        # 否则会变成"连上了、按钮能点、一开训练却毫无反应"这种更难查的状态。
        if (ftms.CHAR_INDOOR_BIKE_DATA not in self._chars
                and ftms.CHAR_CYCLING_POWER_MEASUREMENT not in self._chars):
            raise self._missing_ftms_error()

        targets = []
        if ftms.CHAR_INDOOR_BIKE_DATA in self._chars:
            targets.append((ftms.CHAR_INDOOR_BIKE_DATA, self._handle_indoor_bike_data))
        if ftms.CHAR_CYCLING_POWER_MEASUREMENT in self._chars:
            targets.append((ftms.CHAR_CYCLING_POWER_MEASUREMENT, self._handle_cycling_power))
        if ftms.CHAR_FITNESS_MACHINE_STATUS in self._chars:
            targets.append((ftms.CHAR_FITNESS_MACHINE_STATUS, self._handle_status))
        if ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT in self._chars:
            targets.append((ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT, self._handle_control_response))

        subscribed = 0
        for uuid, handler in targets:
            try:
                await self.client.start_notify(uuid, handler)
                subscribed += 1
            except Exception as exc:
                log.warning("订阅 %s 失败：%s", uuid[:8], exc)

        if not subscribed:
            raise TrainerError(
                "订阅骑行数据失败——设备有相关特征值但拒绝了订阅。"
                "服务结构：{}".format(self.describe_gatt())
            )
        if ftms.CHAR_INDOOR_BIKE_DATA not in self._chars:
            log.warning("该设备没有 Indoor Bike Data(0x2AD2)，功率数据来自 Cycling Power 服务")

    async def _read_capabilities(self) -> None:
        raw_feature = await self._read_optional(ftms.CHAR_FITNESS_MACHINE_FEATURE)
        if raw_feature is not None:
            parse = ftms.parse_fitness_machine_feature(raw_feature)
        else:
            parse = {}

        power_range = None
        raw_pr = await self._read_optional(ftms.CHAR_SUPPORTED_POWER_RANGE)
        if raw_pr is not None:
            power_range = ftms.parse_supported_power_range(raw_pr)

        resistance_range = None
        raw_rr = await self._read_optional(ftms.CHAR_SUPPORTED_RESISTANCE_LEVEL_RANGE)
        if raw_rr is not None:
            resistance_range = ftms.parse_supported_resistance_level_range(raw_rr)

        battery = await self._read_optional(ftms.CHAR_BATTERY_LEVEL)
        manufacturer = await self._read_optional(ftms.CHAR_MANUFACTURER_NAME)
        model = await self._read_optional(ftms.CHAR_MODEL_NUMBER)
        firmware = await self._read_optional(ftms.CHAR_FIRMWARE_REVISION)

        def text(raw: Optional[bytes]) -> Optional[str]:
            if raw is None:
                return None
            try:
                return raw.decode("utf-8", errors="ignore").strip("\x00").strip()
            except Exception:
                return None

        self.machine_info = {
            "manufacturer": text(manufacturer),
            "model": text(model),
            "firmware": text(firmware),
            "battery": battery[0] if battery else None,
        }
        if self.machine_info["model"] or self.machine_info["manufacturer"]:
            self.name = " ".join(
                x for x in [self.machine_info["manufacturer"], self.machine_info["model"]] if x
            )

        has_control_point = ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT in self._chars
        supports_power_target = bool(parse.get("supports_power_target"))
        supports_resistance_target = bool(parse.get("supports_resistance_target"))

        # 有些廉价固件不实现 Feature 特征值（读不到），但控制点其实是好用的。
        # 这时不要因为缺少"声明"就直接判定它不能用，留给上层去实际试。
        if not parse.get("target_raw"):
            if has_control_point:
                supports_power_target = True
                supports_resistance_target = True

        self.capabilities = {
            "has_ftms": bool(self._chars),
            "has_control_point": has_control_point,
            "supports_power_target": supports_power_target,
            "supports_resistance_target": supports_resistance_target,
            "supports_inclination_target": bool(parse.get("supports_inclination_target")),
            "has_indoor_bike_data": ftms.CHAR_INDOOR_BIKE_DATA in self._chars,
            "has_cycling_power": ftms.CHAR_CYCLING_POWER_MEASUREMENT in self._chars,
            "power_range": power_range,
            "resistance_range": resistance_range,
            "declared_targets": parse.get("target", []),
            "declared_measurements": parse.get("machine", []),
        }
        log.info("骑行台能力：%s", self.capabilities)

    async def _read_optional(self, uuid: str) -> Optional[bytes]:
        if self.client is None or uuid not in self._chars:
            return None
        try:
            return await self.client.read_gatt_char(uuid)
        except Exception as exc:
            log.debug("读取 %s 失败：%s", uuid[:8], exc)
            return None

    # ------------------------------------------------------------------
    # 数据回调
    # ------------------------------------------------------------------

    def _handle_indoor_bike_data(self, _char: Any, data: bytearray) -> None:
        parsed = ftms.parse_indoor_bike_data(bytes(data))
        if not parsed:
            return
        self._merge(parsed)

    def _handle_cycling_power(self, _char: Any, data: bytearray) -> None:
        parsed = ftms.parse_cycling_power_measurement(bytes(data))
        if not parsed:
            return
        # FTMS 的功率优先，只有在 FTMS 没给功率时才用 CPS 的
        if "power_w" not in self.latest:
            self._merge(parsed)

    def _handle_status(self, _char: Any, data: bytearray) -> None:
        parsed = ftms.parse_fitness_machine_status(bytes(data))
        if parsed.get("status"):
            log.debug("骑行台状态：%s", parsed["status"])
            self._emit_event("status", parsed["status"])

    def _handle_control_response(self, _char: Any, data: bytearray) -> None:
        parsed = ftms.parse_control_point_response(bytes(data))
        log.debug("控制点应答：%s", parsed)
        op = parsed.get("op_code")
        if op is not None:
            deadline = self._late_until.get(op)
            if deadline is not None:
                if time.monotonic() < deadline:
                    # 这是一条迟到的应答，属于上一次已经超时的请求。丢掉它，
                    # 否则会把上一次的结果安到这一次头上。
                    log.debug("丢弃迟到的控制点应答：%s", parsed)
                    return
                self._late_until.pop(op, None)
        if op is not None:
            self._cp_responded = True
        waiter = self._response_waiters.pop(op, None) if op is not None else None
        if waiter is not None and not waiter.done():
            waiter.set_result(parsed)
        if op == ftms.OP_REQUEST_CONTROL:
            self.has_control = bool(parsed.get("has_control"))

    def _merge(self, parsed: Dict[str, Any]) -> None:
        self.latest.update(parsed)
        # 用单调时钟。陈旧判定问的是「距上次收到数据过了多久」，
        # 和墙上时钟无关；用 time.time() 的话，系统对时往回一跳就可能
        # 把这个差值算成负数，于是设备明明静默、却被判成「数据是新的」，
        # 程序会拿冻结的功率继续积分。
        self.last_data_time = time.monotonic()
        if self.on_data is not None:
            self.on_data(parsed)

    def _handle_disconnect(self, _client: Any) -> None:
        was_connected = self.connected
        self.connected = False
        self.has_control = False
        # 断连之后必须把最新数据清掉：latest 是跨帧累积的字典，留着的话上层会
        # 以为还在正常收数据（"power_w 还在"），继续拿冻结的功率积分和调阻力。
        self.latest = {}
        self.last_data_time = 0.0
        # 连接失败时的清理也会触发这个回调，那不是"意外断开"，不能报给用户，
        # 否则一次失败会弹出两条互相矛盾的消息。
        if self._intentional_disconnect or not was_connected:
            return
        log.warning("与骑行台的连接意外断开")
        self._emit_event("disconnected", "与骑行台的连接断开了")

    def _emit_event(self, kind: str, message: str) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, message)
            except Exception:
                log.exception("事件回调出错")

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------

    async def _send(self, payload: bytes, expect_op: int,
                    timeout: Optional[float] = None) -> Dict[str, Any]:
        """写控制点并等待对应的指示应答。

        应答等待会占住调用方（训练主循环），所以超时必须能自适应：正常固件
        几十毫秒就应答，而不回应答的固件如果每次都等满 4 秒，2 秒一次的保活
        就会把主循环彻底卡住，数据采集和闭环调整都会退化成几秒一次。
        """
        if self.client is None or not self.connected:
            raise TrainerError("骑行台未连接")
        if ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT not in self._chars:
            raise TrainerError("该骑行台没有 FTMS 控制点，无法下发指令")

        wait = self._response_timeout if timeout is None else timeout

        async with self._write_lock:
            loop = asyncio.get_event_loop()
            waiter: asyncio.Future = loop.create_future()
            self._response_waiters[expect_op] = waiter
            try:
                await self.client.write_gatt_char(
                    ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT, payload, response=True
                )
            except Exception as exc:
                self._response_waiters.pop(expect_op, None)
                raise TrainerError(
                    "下发指令失败：{}".format(exc)
                ) from exc

            try:
                result = await asyncio.wait_for(waiter, timeout=wait)
            except asyncio.TimeoutError:
                self._response_waiters.pop(expect_op, None)
                # 记下"到这个时刻为止，这个 op 的应答都当作迟到的丢弃"。
                # 代价要说清楚：窗口内如果下一条同类指令的应答来得很快，也会被
                # 一并丢掉，表现出来就是那次调用"超时"。这是有意选的方向——
                # 超时在调用方那边被当成"指令已经生效"（大多数固件确实执行了，
                # 只是不回应答），而把迟到的失败应答安到新指令头上会抛
                # TrainerError，可能让一次训练直接以 error 收场。两害相权取其轻。
                self._late_until[expect_op] = time.monotonic() + CP_RESPONSE_TIMEOUT_S
                # 大多数骑行台都会应答；不回应答的少数固件其实也执行了指令，
                # 所以这里不当作失败，只是放宽等待时间。
                self._cp_timeouts += 1
                # 只有"从来没应答过"的固件才放宽。如果它应答过、只是慢，放宽反而有
                # 害：应答里没有请求序号，迟到的应答会被算到下一次请求头上，把上
                # 一次的失败结果报成这一次的失败。
                if (not self._cp_responded
                        and self._cp_timeouts >= CP_TIMEOUTS_BEFORE_FAST
                        and self._response_timeout != CP_FAST_TIMEOUT_S):
                    self._response_timeout = CP_FAST_TIMEOUT_S
                    log.warning(
                        "控制点连续 %d 次未回应答，后续指令不再等待应答"
                        "（指令照常下发，只是不再阻塞训练循环）", self._cp_timeouts)
                elif self._response_timeout == CP_FAST_TIMEOUT_S:
                    # 已经确认这固件不回应答了，别再每 2 秒刷一条警告
                    log.debug("指令 %s 未收到应答（已按不回应答处理）",
                              ftms.OP_NAMES.get(expect_op, hex(expect_op)))
                else:
                    log.warning("指令 %s 未收到应答（超时 %.1fs），按已生效处理",
                                ftms.OP_NAMES.get(expect_op, hex(expect_op)), wait)
                return {"ok": None, "timeout": True}
            except asyncio.CancelledError:
                # 等待期间被取消（比如训练停止），留下的 waiter 要清掉
                self._response_waiters.pop(expect_op, None)
                raise

            # 收到应答说明固件是正常的，恢复正常等待时间
            self._cp_timeouts = 0
            self._response_timeout = CP_RESPONSE_TIMEOUT_S
            return result

    async def request_control(self) -> Dict[str, Any]:
        result = await self._send(ftms.encode_request_control(), ftms.OP_REQUEST_CONTROL)
        if result.get("ok") is False:
            raise TrainerError("骑行台拒绝交出控制权：{}（可能被其他 App 占用）"
                               .format(result.get("result")))
        self.has_control = True
        return result

    async def start(self) -> None:
        """Request Control + Start/Resume，进入可控制状态。"""
        await self.request_control()
        result = await self._send(ftms.encode_start_or_resume(), ftms.OP_START_OR_RESUME)
        if result.get("ok") is False:
            log.warning("Start/Resume 返回 %s，继续尝试下发目标功率", result.get("result"))

    async def set_target_power(self, watts: int) -> None:
        """ERG 目标功率。若控制权被抢走，重试一次 Request Control。"""
        try:
            result = await self._send(ftms.encode_set_target_power(watts),
                                      ftms.OP_SET_TARGET_POWER)
        except TrainerError:
            raise
        if result.get("ok") is False:
            if result.get("result_code") == 0x05:  # Control Not Permitted
                log.info("控制权失效，重新申请")
                await self.request_control()
                result = await self._send(ftms.encode_set_target_power(watts),
                                          ftms.OP_SET_TARGET_POWER)
            if result.get("ok") is False:
                raise TrainerError("设定目标功率失败：{}".format(result.get("result")))

    async def set_resistance_raw(self, raw: int) -> None:
        """按 0.1 单位的原始整数值直接设阻力，供闭环控制器使用，避免单位歧义。"""
        raw = max(0, min(255, int(round(raw))))
        payload = bytes([ftms.OP_SET_TARGET_RESISTANCE_LEVEL, raw])
        result = await self._send(payload, ftms.OP_SET_TARGET_RESISTANCE_LEVEL)
        if result.get("ok") is False and result.get("result_code") == 0x05:
            await self.request_control()
            result = await self._send(payload, ftms.OP_SET_TARGET_RESISTANCE_LEVEL)
        if result.get("ok") is False:
            raise TrainerError("设定阻力失败：{}".format(result.get("result")))

    async def set_resistance_level(self, level: float) -> None:
        await self.set_resistance_raw(int(round(level * 10)))

    async def set_target_inclination(self, percent: float) -> None:
        await self._send(ftms.encode_set_target_inclination(percent),
                         ftms.OP_SET_TARGET_INCLINATION)

    async def pause(self) -> None:
        await self._send(ftms.encode_pause(), ftms.OP_STOP_OR_PAUSE)

    async def stop(self) -> None:
        """停止训练：解除 ERG，骑行台回到自由骑行。"""
        if self.client is None or not self.connected:
            return
        try:
            await self._send(ftms.encode_stop(), ftms.OP_STOP_OR_PAUSE)
        finally:
            self.has_control = False

    async def disconnect(self) -> None:
        self._intentional_disconnect = True
        try:
            await self.stop()
        except Exception:
            pass
        await self._cleanup_client()

    # ------------------------------------------------------------------

    def state(self) -> Dict[str, Any]:
        return {
            "kind": "simulator" if self.is_simulator else "ble",
            "connected": self.connected,
            "name": self.name,
            "capabilities": self.capabilities,
            "machine_info": self.machine_info,
            "has_control": self.has_control,
            "gatt": self.describe_gatt() if self.service_map else "",
        }
