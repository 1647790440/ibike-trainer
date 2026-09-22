"""FTMS（Fitness Machine Service，Bluetooth SIG 标准服务 0x1826）协议层。

这个模块只做纯粹的数据编解码，不碰任何 BLE 逻辑，因此可以脱离硬件单独测试。

参考：Bluetooth SIG "Fitness Machine Service 1.0" 规范。
智能骑行台 / 动感单车只要支持 FTMS，Zwift、MyWhoosh 这些软件就是靠下面这几条
特征值来读数据和控阻力的：

    0x2AD2  Indoor Bike Data        —— 骑行台 → App，推送功率/踏频/速度
    0x2AD9  Fitness Machine Control Point —— App → 骑行台，下发目标功率等指令
    0x2ACC  Fitness Machine Feature —— 骑行台 → App，声明自己支持哪些能力
    0x2AD8  Supported Power Range   —— 可设定的功率范围
    0x2AD6  Supported Resistance Level Range —— 可设定的阻力范围
"""

from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# UUID
# --------------------------------------------------------------------------


def sig_uuid(short: int) -> str:
    """把 16 位短 UUID 展开成蓝牙标准的 128 位形式。"""
    return "{:08x}-0000-1000-8000-00805f9b34fb".format(short)


FTMS_SERVICE = sig_uuid(0x1826)
CHAR_FITNESS_MACHINE_FEATURE = sig_uuid(0x2ACC)
CHAR_INDOOR_BIKE_DATA = sig_uuid(0x2AD2)
CHAR_SUPPORTED_RESISTANCE_LEVEL_RANGE = sig_uuid(0x2AD6)
CHAR_SUPPORTED_POWER_RANGE = sig_uuid(0x2AD8)
CHAR_FITNESS_MACHINE_CONTROL_POINT = sig_uuid(0x2AD9)
CHAR_FITNESS_MACHINE_STATUS = sig_uuid(0x2ADA)

# 备用：Cycling Power Service（部分骑行台只在这里提供功率，FTMS 里反而没有）
CPS_SERVICE = sig_uuid(0x1818)
CHAR_CYCLING_POWER_MEASUREMENT = sig_uuid(0x2A63)
CHAR_CYCLING_POWER_FEATURE = sig_uuid(0x2A65)
CHAR_CYCLING_POWER_CONTROL_POINT = sig_uuid(0x2A66)

# 电池 / 设备信息，界面上顺手显示一下
BAS_SERVICE = sig_uuid(0x180F)
CHAR_BATTERY_LEVEL = sig_uuid(0x2A19)
DIS_SERVICE = sig_uuid(0x180A)
CHAR_MANUFACTURER_NAME = sig_uuid(0x2A29)
CHAR_MODEL_NUMBER = sig_uuid(0x2A24)
CHAR_FIRMWARE_REVISION = sig_uuid(0x2A26)

# --------------------------------------------------------------------------
# 控制点操作码（Op Code）
# --------------------------------------------------------------------------

OP_REQUEST_CONTROL = 0x00
OP_RESET = 0x01
OP_SET_TARGET_SPEED = 0x02
OP_SET_TARGET_INCLINATION = 0x03
OP_SET_TARGET_RESISTANCE_LEVEL = 0x04
OP_SET_TARGET_POWER = 0x05
OP_SET_TARGET_HEART_RATE = 0x06
OP_START_OR_RESUME = 0x07
OP_STOP_OR_PAUSE = 0x08
OP_SET_TARGETED_CADENCE = 0x09

STOP_CODE_STOP = 0x01
STOP_CODE_PAUSE = 0x02

RESPONSE_CODE = 0x80

OP_NAMES = {
    OP_REQUEST_CONTROL: "Request Control",
    OP_RESET: "Reset",
    OP_SET_TARGET_SPEED: "Set Target Speed",
    OP_SET_TARGET_INCLINATION: "Set Target Inclination",
    OP_SET_TARGET_RESISTANCE_LEVEL: "Set Target Resistance Level",
    OP_SET_TARGET_POWER: "Set Target Power",
    OP_SET_TARGET_HEART_RATE: "Set Target Heart Rate",
    OP_START_OR_RESUME: "Start or Resume",
    OP_STOP_OR_PAUSE: "Stop or Pause",
    OP_SET_TARGETED_CADENCE: "Set Targeted Cadence",
}

RESULT_NAMES = {
    0x01: "Success",
    0x02: "Op Code Not Supported",
    0x03: "Invalid Parameter",
    0x04: "Operation Failed",
    0x05: "Control Not Permitted",
}


# --------------------------------------------------------------------------
# 解析：Indoor Bike Data (0x2AD2)
# --------------------------------------------------------------------------

# flags 各 bit 的含义（FTMS 规范 Table 4.9）
FLAG_MORE_DATA = 0x0001          # 1 = 本帧不含瞬时速度
FLAG_AVG_SPEED = 0x0002
FLAG_INST_CADENCE = 0x0004
FLAG_AVG_CADENCE = 0x0008
FLAG_TOTAL_DISTANCE = 0x0010
FLAG_RESISTANCE_LEVEL = 0x0020
FLAG_INST_POWER = 0x0040
FLAG_AVG_POWER = 0x0080
FLAG_EXPENDED_ENERGY = 0x0100
FLAG_HEART_RATE = 0x0200
FLAG_METABOLIC_EQUIVALENT = 0x0400
FLAG_ELAPSED_TIME = 0x0800
FLAG_REMAINING_TIME = 0x1000


class _Cursor:
    """按顺序读取字段的小游标，越界时抛 ValueError 由外层统一兜住。"""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def _take(self, n: int) -> bytes:
        if self.offset + n > len(self.data):
            raise ValueError("数据长度不足")
        chunk = self.data[self.offset:self.offset + n]
        self.offset += n
        return chunk

    def u8(self) -> int:
        return struct.unpack("<B", self._take(1))[0]

    def s16(self) -> int:
        return struct.unpack("<h", self._take(2))[0]

    def u16(self) -> int:
        return struct.unpack("<H", self._take(2))[0]

    def u24(self) -> int:
        raw = self._take(3)
        return raw[0] | (raw[1] << 8) | (raw[2] << 16)


def parse_indoor_bike_data(data: bytes) -> Dict[str, Any]:
    """解析 Indoor Bike Data 通知帧。

    返回的字典里只会包含本帧真正携带的字段。任何一项解析失败都只影响其后的字段，
    前面的结果仍然保留——不同厂商的固件实现质量差别很大，容错比严格更重要。
    """
    raw = bytes(data)
    result: Dict[str, Any] = {}
    if len(raw) < 2:
        return result

    flags = struct.unpack_from("<H", raw, 0)[0]
    result["flags"] = flags
    cur = _Cursor(raw)
    cur.offset = 2

    try:
        if not (flags & FLAG_MORE_DATA):
            result["speed_kmh"] = round(cur.u16() / 100.0, 2)
        if flags & FLAG_AVG_SPEED:
            result["avg_speed_kmh"] = round(cur.u16() / 100.0, 2)
        if flags & FLAG_INST_CADENCE:
            result["cadence_rpm"] = cur.u16() / 2.0
        if flags & FLAG_AVG_CADENCE:
            result["avg_cadence_rpm"] = cur.u16() / 2.0
        if flags & FLAG_TOTAL_DISTANCE:
            result["distance_m"] = cur.u24()
        if flags & FLAG_RESISTANCE_LEVEL:
            result["resistance_level"] = cur.s16() / 10.0
        if flags & FLAG_INST_POWER:
            result["power_w"] = cur.s16()
        if flags & FLAG_AVG_POWER:
            result["avg_power_w"] = cur.s16()
        if flags & FLAG_EXPENDED_ENERGY:
            result["energy_total_kcal"] = cur.u16()
            result["energy_per_hour_kcal"] = cur.u16()
            result["energy_per_min_kcal"] = cur.u8()
        if flags & FLAG_HEART_RATE:
            result["heart_rate_bpm"] = cur.u8()
        if flags & FLAG_METABOLIC_EQUIVALENT:
            result["met"] = cur.u8() / 10.0
        if flags & FLAG_ELAPSED_TIME:
            result["trainer_elapsed_s"] = cur.u16()
        if flags & FLAG_REMAINING_TIME:
            result["trainer_remaining_s"] = cur.u16()
    except (ValueError, struct.error):
        result["truncated"] = True

    return result


# --------------------------------------------------------------------------
# 解析：Fitness Machine Feature (0x2ACC)
# --------------------------------------------------------------------------

# 低 32 位：机器本身能"测量/上报"什么
MACHINE_FEATURE_BITS = [
    (0, "平均速度"),
    (1, "踏频"),
    (2, "总距离"),
    (3, "坡度"),
    (4, "爬升"),
    (5, "配速"),
    (6, "步数"),
    (7, "阻力等级"),
    (8, "步幅数"),
    (9, "消耗能量"),
    (10, "心率"),
    (11, "代谢当量"),
    (12, "已用时间"),
    (13, "剩余时间"),
    (14, "功率"),
    (15, "皮带受力与功率输出"),
]

# 高 32 位：能接受哪些"目标设定"（这才是决定 ERG 能不能用的关键）
TARGET_FEATURE_BITS = [
    (0, "目标速度"),
    (1, "目标坡度"),
    (2, "目标阻力"),
    (3, "目标功率"),
    (4, "目标心率"),
    (5, "目标消耗能量"),
    (6, "目标步数"),
    (7, "目标步幅"),
    (8, "目标距离"),
    (9, "目标训练时间"),
    (10, "目标心率区间 2 区时间"),
    (11, "目标心率区间 3 区时间"),
    (12, "目标心率区间 5 区时间"),
    (13, "室内骑行模拟参数"),
    (14, "轮周长设置"),
    (15, "Spindown 校准"),
    (16, "目标踏频"),
]


def parse_fitness_machine_feature(data: bytes) -> Dict[str, Any]:
    """解析 Fitness Machine Feature，得出骑行台支持的能力集合。"""
    raw = bytes(data)
    machine = 0
    target = 0
    if len(raw) >= 4:
        machine = struct.unpack_from("<I", raw, 0)[0]
    if len(raw) >= 8:
        target = struct.unpack_from("<I", raw, 4)[0]

    return {
        "machine_raw": machine,
        "target_raw": target,
        "machine": [name for bit, name in MACHINE_FEATURE_BITS if machine & (1 << bit)],
        "target": [name for bit, name in TARGET_FEATURE_BITS if target & (1 << bit)],
        "supports_power_measurement": bool(machine & (1 << 14)),
        "supports_cadence": bool(machine & (1 << 1)),
        "supports_power_target": bool(target & (1 << 3)),
        "supports_resistance_target": bool(target & (1 << 2)),
        "supports_inclination_target": bool(target & (1 << 1)),
        "supports_simulation": bool(target & (1 << 13)),
    }


# --------------------------------------------------------------------------
# 解析：Supported Power Range (0x2AD8) / Resistance Level Range (0x2AD6)
# --------------------------------------------------------------------------


def parse_supported_power_range(data: bytes) -> Optional[Dict[str, int]]:
    """最小功率 / 最大功率 / 步进，单位瓦。"""
    raw = bytes(data)
    if len(raw) < 6:
        return None
    minimum, maximum, increment = struct.unpack_from("<hhH", raw, 0)
    return {"min": minimum, "max": maximum, "increment": increment}


def parse_supported_resistance_level_range(data: bytes) -> Optional[Dict[str, float]]:
    """最小 / 最大 / 步进阻力等级，按规范单位是 0.1，这里保留原始整数。"""
    raw = bytes(data)
    if len(raw) < 3:
        return None
    minimum, maximum, increment = struct.unpack_from("<BBB", raw, 0)
    if maximum <= minimum:
        return None
    return {
        "min": minimum,
        "max": maximum,
        "increment": increment or 1,
        "min_raw": minimum,
        "max_raw": maximum,
        "increment_raw": increment or 1,
    }


# --------------------------------------------------------------------------
# 解析：Cycling Power Measurement (0x2A63) —— FTMS 没给功率时的备用来源
# --------------------------------------------------------------------------


def parse_cycling_power_measurement(data: bytes) -> Dict[str, Any]:
    raw = bytes(data)
    if len(raw) < 4:
        return {}
    flags = struct.unpack_from("<H", raw, 0)[0]
    power = struct.unpack_from("<h", raw, 2)[0]
    result: Dict[str, Any] = {"power_w": power, "flags": flags}
    # bit5 = 曲柄/轮圈偏转角存在时，后面第 4、5 字节是踏频之外的偏移量，这里不处理
    return result


# --------------------------------------------------------------------------
# 编码：控制点请求
# --------------------------------------------------------------------------


def encode_request_control() -> bytes:
    return bytes([OP_REQUEST_CONTROL])


def encode_start_or_resume() -> bytes:
    return bytes([OP_START_OR_RESUME])


def encode_stop() -> bytes:
    """停止训练并释放控制权（ERG 随之解除，骑行台回到自由骑行）。"""
    return bytes([OP_STOP_OR_PAUSE, STOP_CODE_STOP])


def encode_pause() -> bytes:
    return bytes([OP_STOP_OR_PAUSE, STOP_CODE_PAUSE])


def encode_set_target_power(watts: int) -> bytes:
    """ERG 模式的核心指令：让骑行台把功率稳定在指定瓦数。"""
    watts = max(-32768, min(32767, int(round(watts))))
    return bytes([OP_SET_TARGET_POWER]) + struct.pack("<h", watts)


def encode_set_target_resistance_level(level: float) -> bytes:
    """设定阻力等级。规范单位是 0.1，所以 10.0 档要编码成 100。"""
    raw = int(round(level * 10))
    raw = max(0, min(255, raw))
    return bytes([OP_SET_TARGET_RESISTANCE_LEVEL, raw])


def encode_set_target_inclination(percent: float) -> bytes:
    """设定模拟坡度，单位 0.1%。部分骑行台用坡度而非阻力来做负荷控制。"""
    raw = int(round(percent * 10))
    raw = max(-32768, min(32767, raw))
    return bytes([OP_SET_TARGET_INCLINATION]) + struct.pack("<h", raw)


# --------------------------------------------------------------------------
# 解析：控制点应答
# --------------------------------------------------------------------------


def parse_control_point_response(data: bytes) -> Dict[str, Any]:
    """解析控制点指示（Indication）返回的结果。

    格式：0x80 | 请求操作码 | 结果码 [| 当前控制者]
    """
    raw = bytes(data)
    out: Dict[str, Any] = {"raw": raw.hex()}
    if len(raw) < 3 or raw[0] != RESPONSE_CODE:
        out["ok"] = None
        out["message"] = "非标准应答"
        return out

    op = raw[1]
    result = raw[2]
    out["op_code"] = op
    out["op_name"] = OP_NAMES.get(op, "0x{:02X}".format(op))
    out["result_code"] = result
    out["result"] = RESULT_NAMES.get(result, "Unknown (0x{:02X})".format(result))
    out["ok"] = result == 0x01
    if op == OP_REQUEST_CONTROL and len(raw) >= 4:
        out["control_owner"] = raw[3]
        out["has_control"] = raw[3] == 0x01
    return out


# --------------------------------------------------------------------------
# 解析：Fitness Machine Status (0x2ADA)
# --------------------------------------------------------------------------

STATUS_NAMES = {
    0x01: "Reset",
    0x02: "Fitness Machine Stopped or Paused by the User",
    0x03: "Fitness Machine Stopped by the Safety Key",
    0x04: "Fitness Machine Started or Resumed by the User",
    0x05: "Target Speed Changed",
    0x06: "Target Inclination Changed",
    0x07: "Target Resistance Level Changed",
    0x08: "Target Power Changed",
    0x09: "Target Heart Rate Changed",
    0x0A: "Targeted Expended Energy Changed",
    0x0B: "Targeted Step Number Changed",
    0x0C: "Targeted Stride Number Changed",
    0x0D: "Targeted Distance Changed",
    0x0E: "Targeted Training Time Changed",
    0x0F: "Targeted Time in Two Heart Rate Zones Changed",
    0x10: "Targeted Time in Three Heart Rate Zones Changed",
    0x11: "Targeted Time in Five Heart Rate Zones Changed",
    0x12: "Indoor Bike Simulation Parameters Changed",
    0x13: "Wheel Circumference Changed",
    0x14: "Spin Down Status",
    0x15: "Targeted Cadence Changed",
}


def parse_fitness_machine_status(data: bytes) -> Dict[str, Any]:
    raw = bytes(data)
    if not raw:
        return {}
    code = raw[0]
    out: Dict[str, Any] = {
        "status_code": code,
        "status": STATUS_NAMES.get(code, "0x{:02X}".format(code)),
    }
    if code == 0x14 and len(raw) >= 2:
        out["spindown"] = {1: "开始", 2: "结束", 3: "成功", 4: "失败"}.get(raw[1], raw[1])
    return out
