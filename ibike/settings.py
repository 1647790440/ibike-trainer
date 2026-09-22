"""本地设置。

目前只存两样：**FTP** 和**自由骑行的阻力档位**。都放在 `data/settings.json`。

单独抽一个模块是因为 FTP 会在三个地方用到（HIIT 方案、自定义课程、FTP 测试），
而且必须跨重启保留——测完一次 FTP 却每次启动都要重新输一遍，那就白测了。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .paths import data_dir

log = logging.getLogger("ibike.settings")

# 取值范围和前端输入框、服务端校验保持一致
FTP_MIN, FTP_MAX, FTP_DEFAULT = 50, 600, 200

# 心率相关。最大心率是心率区间的必要参数；静息心率只有选了"储备心率"算法才需要。
HR_MAX_MIN, HR_MAX_MAX, HR_MAX_DEFAULT = 100, 230, 180
HR_REST_MIN, HR_REST_MAX, HR_REST_DEFAULT = 30, 120, 60
HR_LIMIT_MIN, HR_LIMIT_MAX, HR_LIMIT_DEFAULT = 100, 220, 165
ZONE_MODES = ("max", "reserve")


def settings_path() -> Path:
    return data_dir() / "settings.json"


DEFAULTS: Dict[str, Any] = {
    "ftp": FTP_DEFAULT,
    "free_resistance": 90,
    # 心率带：记住上次连的那一根，"下次开训自动重连"要用
    "hr_strap_address": "",
    "hr_strap_name": "",
    "hr_max": HR_MAX_DEFAULT,
    "hr_rest": HR_REST_DEFAULT,
    "hr_zone_mode": "max",
    # 心率上限保护默认关：它会在训练中主动下调功率，得让用户明确打开
    "hr_limit_enabled": False,
    "hr_limit_bpm": HR_LIMIT_DEFAULT,
}


def _clamp_int(raw: Any, low: int, high: int, fallback: int) -> int:
    try:
        value = int(round(float(raw)))
    except (TypeError, ValueError):
        return fallback
    except OverflowError:
        # 手工把 settings.json 改成 {"ftp": 1e400} 时 float→int 会抛这个。
        # 设置文件坏了就该退回默认值，而不是让整个程序起不来。
        return fallback
    return max(low, min(high, value))


class Settings:
    """一个很小的键值存储，够用就行。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else settings_path()
        self.values: Dict[str, Any] = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        self.values = dict(DEFAULTS)
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 坏了就用默认值，但别悄悄当没发生
            log.warning("设置文件无法解析（%s），已改用默认值：%s", exc, self.path)
            return
        if not isinstance(data, dict):
            log.warning("设置文件结构不对，已改用默认值：%s", self.path)
            return
        self.values.update(self._clean(data))

    @staticmethod
    def _clean(data: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if "ftp" in data:
            out["ftp"] = _clamp_int(data["ftp"], FTP_MIN, FTP_MAX, FTP_DEFAULT)
        if "free_resistance" in data:
            out["free_resistance"] = _clamp_int(data["free_resistance"], 0, 255, 90)
        # 心率带地址/名字是字符串，且不能太长（它会被写进 settings.json）
        for key in ("hr_strap_address", "hr_strap_name"):
            if key in data:
                out[key] = str(data[key] or "")[:64]
        if "hr_max" in data:
            out["hr_max"] = _clamp_int(data["hr_max"], HR_MAX_MIN, HR_MAX_MAX,
                                       HR_MAX_DEFAULT)
        if "hr_rest" in data:
            out["hr_rest"] = _clamp_int(data["hr_rest"], HR_REST_MIN, HR_REST_MAX,
                                        HR_REST_DEFAULT)
        if "hr_zone_mode" in data:
            mode = str(data["hr_zone_mode"] or "")
            out["hr_zone_mode"] = mode if mode in ZONE_MODES else "max"
        if "hr_limit_enabled" in data:
            out["hr_limit_enabled"] = bool(data["hr_limit_enabled"])
        if "hr_limit_bpm" in data:
            out["hr_limit_bpm"] = _clamp_int(data["hr_limit_bpm"], HR_LIMIT_MIN,
                                             HR_LIMIT_MAX, HR_LIMIT_DEFAULT)
        return out

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.values, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """改设置并落盘。写盘失败会抛 OSError —— 不能吞掉。

        以前这里 catch 住 OSError 只打一条日志，接口照样回 ok:true，用户以为
        FTP 存下来了，重启之后发现又变回去了，而且屏幕上没有任何提示。
        """
        self.values.update(self._clean(patch or {}))
        self.save()
        return dict(self.values)

    def get(self, key: str, fallback: Any = None) -> Any:
        return self.values.get(key, fallback)
