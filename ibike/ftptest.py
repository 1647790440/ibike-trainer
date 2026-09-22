"""FTP 测试方案。

三种主流做法，各自的依据和取舍：

1. **坡道测试（Ramp Test）** —— 室内最常用。
   从起始功率开始每分钟加固定瓦数，直到踩不动为止；FTP = 最好 1 分钟功率 × 75%。
   优点是不需要配速技巧（对第一次测试的人很重要），全程 ERG 自动加载。
   但它本质是"最大有氧功率测试"而不是阈值测试：耐力型车手会被低估，
   爆发型会被高估。那个 75% 也是人群平均值，个体差异能到 81%~85%。

2. **20 分钟测试（Allen-Coggan 标准方案）** —— 文献里的"标准做法"。
   FTP = 20 分钟平均功率 × 95%。

   两个容易被忽略但很关键的细节：

   * **20 分钟前必须先做一次 5 分钟全力**。目的是先把无氧储备耗掉，
     否则 20 分钟的功率会虚高——无氧能力贡献的那部分撑不满 20 分钟，
     却会让均功率看起来更漂亮。Coggan 和 Allen 在原始方案里写得很清楚。
   * **不能用 ERG**。ERG 会把功率锁在你设定的值上，而测试就是要把最大值找出来；
     用 ERG 测出来的是"你设定的那个数"，不是你的能力。必须自由骑行、自己配速。

3. **8 分钟测试（2×8）** —— 比 20 分钟轻松一点的替代方案。
   两次 8 分钟全力、中间充分恢复；FTP = 较好那次的平均功率 × 90%。
   同样必须自由骑行。

关于"职业车队怎么做"：公开资料里，WorldTour 车队很少做这种室内的单一 FTP 测试。
他们主要靠**血乳酸测试**（逐级加载采指血，找 LT1/LT2 拐点）和 **INSCYD 之类的
代谢组学测试**（测 VO2max、VLamax），再加上从比赛和训练数据里拟合**功率-时长曲线**
和**临界功率（CP/W'）模型**。单一 FTP 测试更多是业余和室内软件的做法。

所以这个功能定位很清楚：**它是一个可复现的室内估算工具，不是一个生理测量**。
界面上也会这么写。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .workouts import KIND_COOLDOWN, KIND_RECOVERY, KIND_WARMUP, KIND_WORK, make_step

# 被测量的那一段。和 KIND_WORK 区分开，因为达标率只对 work 统计，
# 而测试段需要的是完整平均值（含开头几秒的过渡），不能被"过渡期"排除掉。
KIND_TEST = "test"

# 结果计算方式
RESULT_RAMP = "ramp"
RESULT_BEST_SEGMENT = "best_segment"


def P(key: str, label: str, default: float, minimum: float, maximum: float,
      step: float = 1, unit: str = "") -> Dict[str, Any]:
    return {"key": key, "label": label, "default": default, "min": minimum,
            "max": maximum, "step": step, "unit": unit}


def _warmup(params: Dict[str, float], ftp_hint: float, key: str = "warmup_min",
            pct_key: str = "warmup_pct") -> List[Dict[str, Any]]:
    minutes = float(params.get(key, 10))
    if minutes <= 0:
        return []
    pct = float(params.get(pct_key, 55))
    return [make_step("热身", KIND_WARMUP, minutes * 60, pct / 100.0, ftp_hint)]


def _cooldown(params: Dict[str, float], ftp_hint: float, key: str = "cooldown_min") -> List[Dict[str, Any]]:
    minutes = float(params.get(key, 10))
    if minutes <= 0:
        return []
    pct = float(params.get("cooldown_pct", 50))
    return [make_step("冷身", KIND_COOLDOWN, minutes * 60, pct / 100.0, ftp_hint)]


# ----------------------------------------------------------------------
# 1. 坡道测试
# ----------------------------------------------------------------------


def _build_ramp(params: Dict[str, float], ftp_hint: float) -> List[Dict[str, Any]]:
    steps = _warmup(params, ftp_hint)
    start = float(params.get("start_w", 100))
    step_w = float(params.get("step_w", 20))
    step_s = float(params.get("step_s", 60))
    count = int(params.get("max_steps", 25))

    for i in range(count):
        watts = start + step_w * i
        steps.append({
            "name": "第 {} 级 {}W".format(i + 1, int(watts)),
            "kind": KIND_TEST,
            "duration_s": step_s,
            # 坡道测试的目标就是"必须踩到"的功率，所以这里用绝对值而不是 %FTP：
            # 还没测出 FTP 之前，用 FTP 百分比定义坡道是自相矛盾的。
            "pct_ftp": round(watts / ftp_hint, 4) if ftp_hint > 0 else None,
            "target_power": int(round(watts)),
            "zone": None,
        })
    steps += _cooldown(params, ftp_hint)
    return steps


# ----------------------------------------------------------------------
# 2. 20 分钟测试（Allen-Coggan）
# ----------------------------------------------------------------------


def _build_twenty(params: Dict[str, float], ftp_hint: float) -> List[Dict[str, Any]]:
    steps = _warmup(params, ftp_hint, "warmup_min")

    # 3 × 1 分钟快频，把腿唤醒
    reps = int(params.get("opener_reps", 3))
    for i in range(reps):
        steps.append(make_step("快频", KIND_WORK, 60, 1.00, ftp_hint))
        if i < reps - 1:
            steps.append(make_step("缓一缓", KIND_RECOVERY, 60, 0.50, ftp_hint))

    steps.append(make_step("轻松骑", KIND_RECOVERY, 300, 0.50, ftp_hint))

    # 关键的一步：先把无氧储备耗掉，否则接下来的 20 分钟会虚高
    blowout = float(params.get("blowout_min", 5))
    steps.append(make_step("{} 分钟全力（清空无氧）".format(int(blowout)), KIND_WORK, blowout * 60, 1.15, ftp_hint))

    steps.append(make_step("缓过来", KIND_RECOVERY, 300, 0.45, ftp_hint))

    # 被测量的那一段
    tt = float(params.get("test_min", 20))
    steps.append(make_step("{} 分钟计时（尽最大努力）".format(int(tt)),
                           KIND_TEST, tt * 60, 1.00, ftp_hint))

    steps += _cooldown(params, ftp_hint, "cooldown_min")
    return steps


# ----------------------------------------------------------------------
# 3. 8 分钟测试
# ----------------------------------------------------------------------


def _build_eight(params: Dict[str, float], ftp_hint: float) -> List[Dict[str, Any]]:
    steps = _warmup(params, ftp_hint, "warmup_min")

    reps = int(params.get("reps", 2))
    span = float(params.get("test_min", 8))
    rest = float(params.get("rest_min", 10))

    for i in range(reps):
        steps.append(make_step("第 {} 次 {} 分钟全力".format(i + 1, int(span)),
                               KIND_TEST, span * 60, 1.05, ftp_hint))
        if i < reps - 1:
            steps.append(make_step("充分恢复", KIND_RECOVERY, rest * 60, 0.45, ftp_hint))

    steps += _cooldown(params, ftp_hint, "cooldown_min")
    return steps


# ----------------------------------------------------------------------
# 方案定义
# ----------------------------------------------------------------------

_COMMON_WARM = P("warmup_min", "热身", 15, 5, 45, 1, "分钟")
_COMMON_COOL = P("cooldown_min", "冷身", 10, 0, 30, 1, "分钟")


TESTS: List[Dict[str, Any]] = [
    {
        "id": "ramp",
        "name": "坡道测试",
        "subtitle": "室内最常用 · 全程 ERG 自动加载",
        "desc": "从起始功率开始每分钟加一档，直到踩不动为止。FTP = 最好 1 分钟功率 × 75%。"
                "不需要配速技巧，适合第一次测试，但它本质是最大有氧功率测试——"
                "耐力型车手会被低估，爆发型会被高估。程序会自动识别力竭并结束。",
        "erg_mode": "ftms",
        "self_paced": False,
        "result": {"kind": RESULT_RAMP, "multiplier": 0.75,
                   "source_label": "最好 1 分钟功率"},
        "params": [
            P("warmup_min", "热身", 5, 0, 30, 1, "分钟"),
            P("start_w", "起始功率", 100, 60, 250, 5, "W"),
            P("step_w", "每分钟递增", 20, 5, 50, 1, "W"),
            P("step_s", "单级时长", 60, 30, 180, 10, "秒"),
            P("max_steps", "最多级数", 25, 10, 40, 1, "级"),
            P("cooldown_min", "冷身", 10, 0, 30, 1, "分钟"),
        ],
        "build": _build_ramp,
    },
    {
        "id": "twenty",
        "name": "20 分钟测试",
        "subtitle": "Allen-Coggan 标准方案 · 自由骑行",
        "desc": "含 3×1 分钟快频、一次 5 分钟全力、然后 20 分钟计时。FTP = 计时平均功率 × 95%。"
                "那两个细节都很关键：前面那次 5 分钟全力是为了清空无氧储备，"
                "否则 20 分钟功率会虚高；而计时段必须自己配速——ERG 会把功率锁死，"
                "测出来的是你设定的数，不是你的能力。所以这个测试全程不控阻力。",
        "erg_mode": "free",
        "self_paced": True,
        "result": {"kind": RESULT_BEST_SEGMENT, "multiplier": 0.95,
                   "source_label": "计时段平均功率"},
        "params": [
            _COMMON_WARM,
            P("opener_reps", "快频次数", 3, 0, 5, 1, "次"),
            P("blowout_min", "5 分钟全力", 5, 3, 10, 1, "分钟"),
            P("test_min", "计时时长", 20, 8, 40, 1, "分钟"),
            _COMMON_COOL,
        ],
        "build": _build_twenty,
    },
    {
        "id": "eight",
        "name": "8 分钟测试",
        "subtitle": "两次全力 · 自由骑行",
        "desc": "两次 8 分钟全力、中间充分恢复，FTP = 较好那次的平均功率 × 90%。"
                "比 20 分钟轻松一些，但两次之间的恢复很关键——恢复不够会让第二次明显掉功率。"
                "同样必须自由骑行、自己配速。",
        "erg_mode": "free",
        "self_paced": True,
        "result": {"kind": RESULT_BEST_SEGMENT, "multiplier": 0.90,
                   "source_label": "较好一次的平均功率"},
        "params": [
            _COMMON_WARM,
            P("reps", "次数", 2, 2, 4, 1, "次"),
            P("test_min", "每次时长", 8, 5, 15, 1, "分钟"),
            P("rest_min", "组间恢复", 10, 5, 20, 1, "分钟"),
            _COMMON_COOL,
        ],
        "build": _build_eight,
    },
]


def get_test(test_id: str) -> Optional[Dict[str, Any]]:
    for t in TESTS:
        if t["id"] == test_id:
            return t
    return None


def public_tests() -> List[Dict[str, Any]]:
    return [{k: v for k, v in t.items() if k != "build"} for t in TESTS]


def default_params(test_id: str) -> Dict[str, float]:
    t = get_test(test_id)
    return {p["key"]: p["default"] for p in t["params"]} if t else {}


def clamp_params(test_id: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    t = get_test(test_id)
    if t is None:
        raise ValueError("未知的测试方案：{}".format(test_id))
    merged: Dict[str, float] = {}
    for p in t["params"]:
        raw = (params or {}).get(p["key"], p["default"])
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(p["default"])
        merged[p["key"]] = max(float(p["min"]), min(float(p["max"]), value))
    return merged


def build_test_plan(test_id: str, params: Optional[Dict[str, Any]] = None,
                    ftp_hint: float = 200.0) -> List[Dict[str, Any]]:
    """生成测试课表。

    ``ftp_hint`` 只用来给"轻松骑"这类陪衬段定一个大致强度，以及换算显示用的
    百分比——真正的测试结论不依赖它。
    """
    t = get_test(test_id)
    if t is None:
        raise ValueError("未知的测试方案：{}".format(test_id))
    merged = clamp_params(test_id, params)
    hint = max(80.0, min(500.0, float(ftp_hint) or 200.0))
    steps: List[Dict[str, Any]] = []
    for step in t["build"](merged, hint):
        step = dict(step)
        if step.get("target_power", 0) < 20:
            step["target_power"] = 20
        steps.append(step)
    return steps


def plan_stats(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """课表概览。测试课表用"预计时长"没有意义（坡道测试是踩到力竭为止），
    所以这里给出测量段的时长和坡道能到的最高功率。"""
    total = sum(float(s["duration_s"]) for s in steps)
    measured = [s for s in steps if s.get("kind") == KIND_TEST]
    return {
        "total_s": total,
        "step_count": len(steps),
        "work_s": sum(float(s["duration_s"]) for s in steps
                      if s.get("kind") == KIND_WORK),
        "measured_s": sum(float(s["duration_s"]) for s in measured),
        "measured_count": len(measured),
        "peak_power": max((int(s.get("target_power", 0)) for s in steps), default=0),
        "avg_power": (round(sum(s["target_power"] * s["duration_s"] for s in steps) / total)
                      if total > 0 else 0),
        "est_kj": (round(sum(s["target_power"] * s["duration_s"] for s in steps) / 1000.0, 1)
                   if total > 0 else 0.0),
    }
