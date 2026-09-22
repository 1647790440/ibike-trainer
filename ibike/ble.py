"""一次蓝牙广播扫描，骑行台和心率带共用。

为什么要单独一个模块：骑行台和心率带是**两个外设**，但它们的信息来自**同一次
广播扫描**。`BleakScanner.discover()` 一次就能拿到周围所有设备的广播内容
（名字、服务 UUID、信号强度），骑行台靠 FTMS 服务筛、心率带靠心率服务筛，
没有任何理由扫两遍——扫两遍既慢一倍，又会在界面上逼着用户点两次按钮。

所以这里只做"扫一次、返回原始结果"这一件事，怎么筛由各自的模块负责
（`trainer.classify_trainers` / `heartrate.classify_straps`）。这样也避免了
两个模块互相 import。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from bleak import BleakError, BleakScanner

log = logging.getLogger("ibike.ble")


class BleScanError(RuntimeError):
    """扫描失败的统一异常，上层各自翻译成自己那套错误类型。"""


@dataclass
class RawDevice:
    """广播里的一台设备，未经分类。"""

    address: str
    name: str
    rssi: Optional[int]
    service_uuids: List[str]


async def scan_raw(timeout: float = 8.0) -> List[RawDevice]:
    """扫描一次，返回周围所有**报了服务或名字**的设备。

    连名字都没有的设备会被丢掉：这种多半是手机、耳机、手环之类，列出来只会干扰
    用户判断，而且 FTMS 和心率带都一定会在广播里带名字或服务 UUID。
    """
    try:
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except BleakError as exc:
        raise BleScanError(str(exc)) from exc
    except Exception as exc:                    # noqa: BLE001
        # 没授权蓝牙、适配器被关掉之类，bleak 抛的异常类型并不统一
        raise BleScanError(str(exc)) from exc

    devices: List[RawDevice] = []
    for address, (device, adv) in found.items():
        name = (adv.local_name or device.name or "").strip()
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        if not name and not uuids:
            continue
        devices.append(RawDevice(address=device.address, name=name,
                                 rssi=getattr(adv, "rssi", None),
                                 service_uuids=uuids))
    log.info("扫描到 %d 台设备（含服务/名字的）", len(devices))
    return devices
