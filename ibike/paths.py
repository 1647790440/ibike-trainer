"""数据目录的位置。

单独抽出来是为了让测试能把数据写到临时目录里。默认路径指向项目下的 `data/`，
但**测试必须设置 IBIKE_DATA_DIR 环境变量**——否则跑测试会往用户的真实数据目录里
写东西。这不是假设问题：训练报告是"训练一结束就自动保存"的，任何跑了一次训练
的测试都会留下一条真实报告。

环境变量在**构造存储对象时**读取（不是 import 时），这样测试在进程里任何时候
设置都有效。
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "IBIKE_DATA_DIR"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """返回数据根目录（每次调用都重新读环境变量）。"""
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override).expanduser()
    return _PROJECT_ROOT / "data"


def custom_courses_path() -> Path:
    return data_dir() / "custom_workouts.json"


def reports_dir() -> Path:
    return data_dir() / "reports"
