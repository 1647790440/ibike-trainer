#!/usr/bin/env python3
"""iBike 控制台入口。

    python3 main.py                  # 起服务（浏览器/手机里操作）
    python3 main.py --scan           # 只扫描一遍设备，看看能不能找到骑行台
    python3 main.py --sim            # 没有硬件时用模拟骑行台试玩
    python3 main.py --sim --sim-dumb # 模拟一台"不理会目标功率"的骑行台
    python3 main.py --diag           # 诊断原生 ERG：为什么功率压不到设定值
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ibike.ble import BleScanError, scan_raw
from ibike.heartrate import HeartRateClient
from ibike.server import lan_ip, run
from ibike.trainer import TrainerClient


def cmd_scan(timeout: float) -> int:
    print("正在扫描蓝牙设备（{:.0f} 秒）…".format(timeout))
    try:
        # 一次广播扫描，骑行台和心率带都从这一份结果里筛（见 ibike/ble.py）
        raw = asyncio.run(scan_raw(timeout=timeout))
    except BleScanError as exc:
        print("扫描失败：{}".format(exc))
        print("提示：第一次运行时，macOS 会弹窗询问蓝牙权限，需要在"
              "「系统设置 → 隐私与安全性 → 蓝牙」里允许终端访问。")
        return 1

    devices = TrainerClient.classify_trainers(raw)
    straps = HeartRateClient.classify_straps(raw)

    if not devices and not straps:
        print("没有搜到任何有名字的蓝牙设备。")
        print("请确认：骑行台已通电；没有被手机上的 Zwift/MyWhoosh 占用；")
        print("离电脑近一点；然后重试。")
        return 1

    print()
    print("{:<32} {:<18} {:>6}  {}".format("名称", "地址", "信号", "协议"))
    print("-" * 74)
    for d in devices:
        proto = "FTMS 支持" if d.has_ftms else ("仅功率计" if d.has_cps else "-")
        print("{:<32} {:<18} {:>6}  {}".format(d.name[:32], d.address, d.rssi or "?", proto))
    print()
    print("带 “FTMS 支持” 的那一行就是你的骑行台。直接跑 python3 main.py 起服务即可。")

    print()
    print("心率带（按标准心率服务 0x180D 或名字识别，扫到 {} 根）：".format(len(straps)))
    if not straps:
        print("  没扫到。确认带子已经戴上（电极沾点水）、没有被手机上的 App 占着。")
        print("  有些带子不广播服务 UUID：戴上之后它才开始广播，再扫一次通常就有了。")
    for d in straps:
        print("  {:<32} {:<18} {:>6}".format(d.name[:32], d.address, d.rssi or "?"))
    return 0


def cmd_diag(address: str, target: float, seconds: float, quick: bool,
             simulator: bool) -> int:
    """诊断原生 ERG。没有硬件时用 --sim 先看一遍输出长什么样。"""
    from ibike.diag import run as run_diag

    async def go() -> int:
        if simulator:
            from ibike.simulator import SimulatedTrainer
            tester = SimulatedTrainer(cadence=85.0)
            await tester.connect()
        else:
            tester = TrainerClient()
            if not address:
                print("正在扫描骑行台（{:.0f} 秒）…".format(8.0))
                devices = await TrainerClient.scan(timeout=8.0)
                found = [d for d in devices if d.has_ftms]
                if not found:
                    print("没找到支持 FTMS 的设备。先跑 python3 main.py --scan 看看。")
                    return 1
                address = found[0].address
                print("使用：{}  {}".format(found[0].name, address))
            print("正在连接 {} …".format(address))
            await tester.connect(address)

        try:
            return await run_diag(tester, target=target, seconds=seconds, quick=quick)
        finally:
            try:
                await tester.disconnect()
            except Exception:  # noqa: BLE001
                pass

    return asyncio.run(go())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="iBike 智能骑行台 ERG 控制台",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--scan", action="store_true", help="只扫描设备然后退出")
    parser.add_argument("--scan-timeout", type=float, default=8.0, help="扫描时长（秒）")
    parser.add_argument("--sim", "--simulator", dest="sim", action="store_true",
                        help="使用模拟设备（骑行台 + 心率带），不需要真实硬件")
    parser.add_argument("--sim-dumb", action="store_true",
                        help="模拟一台收下目标功率但不执行的骑行台，用于验证自动降级")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0，手机可访问）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    parser.add_argument("--diag", action="store_true",
                        help="诊断原生 ERG：为什么功率压不到设定值（约 4 分钟）")
    parser.add_argument("--address", default="", help="配合 --diag：直接连指定设备")
    parser.add_argument("--target", type=float, default=130.0,
                        help="配合 --diag：诊断用的目标功率（默认 130W）")
    parser.add_argument("--diag-seconds", type=float, default=60.0,
                        help="配合 --diag：每个阶段的时长（默认 60 秒）")
    parser.add_argument("--quick", action="store_true",
                        help="配合 --diag：阶段缩短到 30 秒")
    args = parser.parse_args(argv)

    if args.scan:
        return cmd_scan(args.scan_timeout)

    if args.diag:
        return cmd_diag(args.address, args.target,
                        30.0 if args.quick else args.diag_seconds,
                        args.quick, args.sim)

    if args.sim_dumb:
        args.sim = True

    print("iBike 控制台  ·  本机局域网地址 {}".format(lan_ip()))
    if args.sim:
        print("模式：模拟设备（骑行台 + 心率带）{}".format(
            "（骑行台不响应目标功率，用于验证自动降级）" if args.sim_dumb else ""))
    run(host=args.host, port=args.port, use_simulator=args.sim,
        simulator_responds=not args.sim_dumb,
        simulator_advertise=True if args.sim else None,
        open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
