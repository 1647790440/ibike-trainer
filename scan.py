#!/usr/bin/env python3
"""诊断脚本：把骑行台的 BLE 服务/特征值结构完整打印出来。

当程序连不上、或者连上了但控不住功率时，先跑这个脚本，把输出发出来就能定位
问题出在协议层还是固件层。

    python3 scan.py                 # 扫描并列出所有设备
    python3 scan.py --address UUID  # 连接指定设备并 dump 它的 GATT 结构
    python3 scan.py --pick          # 交互式选择一台设备再 dump
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from bleak import BleakClient

from ibike import ftms
from ibike.trainer import TrainerClient

# 这些 UUID 我们认识，dump 的时候顺便解析出来
KNOWN_CHARS = {
    ftms.CHAR_FITNESS_MACHINE_FEATURE: "Fitness Machine Feature",
    ftms.CHAR_INDOOR_BIKE_DATA: "Indoor Bike Data",
    ftms.CHAR_SUPPORTED_POWER_RANGE: "Supported Power Range",
    ftms.CHAR_SUPPORTED_RESISTANCE_LEVEL_RANGE: "Supported Resistance Level Range",
    ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT: "Fitness Machine Control Point",
    ftms.CHAR_FITNESS_MACHINE_STATUS: "Fitness Machine Status",
    ftms.CHAR_CYCLING_POWER_MEASUREMENT: "Cycling Power Measurement",
    ftms.CHAR_BATTERY_LEVEL: "Battery Level",
    ftms.CHAR_MANUFACTURER_NAME: "Manufacturer Name",
    ftms.CHAR_MODEL_NUMBER: "Model Number",
    ftms.CHAR_FIRMWARE_REVISION: "Firmware Revision",
}

KNOWN_SERVICES = {
    ftms.FTMS_SERVICE: "Fitness Machine Service (FTMS)",
    ftms.CPS_SERVICE: "Cycling Power Service",
    ftms.BAS_SERVICE: "Battery Service",
    ftms.DIS_SERVICE: "Device Information Service",
}


async def dump(address: str, watch: float) -> int:
    print("正在连接 {} …".format(address))
    try:
        client = BleakClient(address, timeout=20.0)
        await client.connect()
    except Exception as exc:
        print("连接失败：{}".format(exc))
        return 1

    try:
        print("已连接。\n")
        print("=" * 78)
        print("GATT 结构")
        print("=" * 78)

        ftms_found = False
        control_point = None

        for service in client.services:
            su = service.uuid.lower()
            label = KNOWN_SERVICES.get(su, "")
            print("\n[服务] {}  {}".format(service.uuid, label))
            if su == ftms.FTMS_SERVICE:
                ftms_found = True

            for char in service.characteristics:
                cu = char.uuid.lower()
                cname = KNOWN_CHARS.get(cu, "")
                props = ",".join(char.properties)
                print("   ├─ {}  [{}]  {}".format(char.uuid, props, cname))

                if cu == ftms.CHAR_FITNESS_MACHINE_CONTROL_POINT:
                    control_point = char

                # 读一下静态特征值，动态的跳过
                if "read" in char.properties and cu != ftms.CHAR_INDOOR_BIKE_DATA:
                    try:
                        raw = await client.read_gatt_char(char.uuid)
                        print("   │     值: {}".format(_describe(cu, raw)))
                    except Exception as exc:
                        print("   │     读取失败: {}".format(exc))

        print()
        print("=" * 78)
        print("结论")
        print("=" * 78)
        if ftms_found and control_point is not None:
            print("✔ 设备支持标准 FTMS 并且有控制点，本程序可以直接控制它。")
            if "indicate" in control_point.properties:
                print("✔ 控制点支持 indicate，指令应答可以正常收到。")
            elif "notify" in control_point.properties:
                print("! 控制点只支持 notify 不支持 indicate，部分指令可能收不到应答。")
        elif ftms_found:
            print("! 有 FTMS 服务但没有控制点：只能读数据，无法控制阻力。")
        else:
            print("✘ 没有发现标准 FTMS 服务。")
            print("  这台设备可能用的是厂商私有协议。请把上面的完整输出发给我，")
            print("  我可以据此补一个私有协议的适配。")

        if watch > 0:
            print()
            print("=" * 78)
            print("监听实时数据 {:.0f} 秒（踩动骑行台就能看到输出）".format(watch))
            print("=" * 78)

            def on_data(_c, data: bytearray) -> None:
                parsed = ftms.parse_indoor_bike_data(bytes(data))
                shown = {k: v for k, v in parsed.items() if k != "flags"}
                print("  {}".format(shown))

            if ftms.CHAR_INDOOR_BIKE_DATA in {
                c.uuid.lower() for s in client.services for c in s.characteristics
            }:
                await client.start_notify(ftms.CHAR_INDOOR_BIKE_DATA, on_data)
                await asyncio.sleep(watch)
                await client.stop_notify(ftms.CHAR_INDOOR_BIKE_DATA)
            else:
                print("  该设备没有 Indoor Bike Data 特征值，无法监听 FTMS 数据。")

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return 0


def _describe(uuid: str, raw: bytes) -> str:
    if uuid == ftms.CHAR_FITNESS_MACHINE_FEATURE:
        parsed = ftms.parse_fitness_machine_feature(bytes(raw))
        return "能上报: {} | 可设定: {}".format(
            ", ".join(parsed["machine"]) or "无",
            ", ".join(parsed["target"]) or "无",
        )
    if uuid == ftms.CHAR_SUPPORTED_POWER_RANGE:
        return "{} W".format(ftms.parse_supported_power_range(bytes(raw)))
    if uuid == ftms.CHAR_SUPPORTED_RESISTANCE_LEVEL_RANGE:
        return "{}".format(ftms.parse_supported_resistance_level_range(bytes(raw)))
    try:
        text = raw.decode("utf-8", errors="ignore").strip("\x00").strip()
        if text and all(32 <= ord(c) < 127 or ord(c) > 127 for c in text):
            return repr(text)
    except Exception:
        pass
    return raw.hex(" ")


async def pick_and_dump(watch: float) -> int:
    print("正在扫描设备…")
    devices = await TrainerClient.scan(timeout=8.0)
    if not devices:
        print("没有搜到设备。")
        return 1

    for i, d in enumerate(devices):
        proto = "FTMS" if d.has_ftms else ("功率计" if d.has_cps else "-")
        print("  [{}] {:<32} {}  ({})".format(i, d.name[:32], d.address, proto))

    try:
        choice = input("\n输入要诊断的设备编号（直接回车选第一个 FTMS 设备）: ").strip()
    except EOFError:
        choice = ""

    if not choice:
        index = next((i for i, d in enumerate(devices) if d.has_ftms), 0)
    else:
        try:
            index = int(choice)
        except ValueError:
            print("编号不合法")
            return 1

    return await dump(devices[index].address, watch)


def main() -> int:
    parser = argparse.ArgumentParser(description="iBike 骑行台 BLE 诊断工具")
    parser.add_argument("--address", "-a", help="直接连接指定设备地址")
    parser.add_argument("--pick", "-p", action="store_true", help="交互式选择设备")
    parser.add_argument("--watch", "-w", type=float, default=0.0,
                        help="连接后监听实时数据若干秒（默认 0）")
    args = parser.parse_args()

    try:
        if args.address:
            return asyncio.run(dump(args.address, args.watch))
        return asyncio.run(pick_and_dump(args.watch))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
