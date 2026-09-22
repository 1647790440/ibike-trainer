"""间歇训练方案（HIIT / 乳酸阈值训练）。

设计上的一个关键决定：**所有强度都用 %FTP 表示，而不是绝对瓦数**。

原因就是"科学"这两个字——乳酸阈值（LT2，常用 FTP 近似）是因人而异、也随时
变化的。同样是"4 分钟高强度"，对 FTP 200W 的人是 190W，对 FTP 300W 的人是
285W。方案只有表达成阈值百分比才谈得上科学；写死瓦数就只是抄了个数字。

区间划分沿用自行车训练里通行的 Coggan 六区（相对 FTP）：

    Z1 主动恢复   < 55%      促进恢复，几乎不产生训练刺激
    Z2 耐力       56–75%     基础有氧，提升脂肪供能与毛细血管密度
    Z3 节奏       76–90%     有氧力量，乳酸开始稳定升高但可平衡
    Z4 乳酸阈值   91–105%    乳酸产生与清除的平衡点附近，直接抬升阈值
    Z5 最大摄氧   106–120%   逼近 VO2max，刺激心肺上限
    Z6 无氧能力   > 120%     短时高强度，主要消耗无氧糖酵解

HIIT 之所以对乳酸系统有效，核心机制是**乳酸穿梭（lactate shuttle）**：乳酸不是
废物，而是可以被慢肌纤维和心肌重新氧化利用的燃料。反复把乳酸推高、再给一段
不完全恢复的时间让它被清除，这个"推高—清除"的循环本身就是在训练清除能力。
不同方案的差别，本质上就是在"推多高"和"留多久清除"这两个维度上取不同的点：

  * 30/15、4×4 这类短间歇：恢复不够彻底，乳酸持续高位累积，主要刺激 VO2max；
  * 2×20 这类阈值课：把功率稳定压在阈值附近，训练的是"清除速率追上产生速率"的
    那个平衡点本身；
  * Over-Under 超阈值：刻意在阈值上下反复穿越，是乳酸穿梭最直接的练法。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# --------------------------------------------------------------------------
# 区间
# --------------------------------------------------------------------------

# 区间按"左闭右开"划分，**必须首尾相接**。以前的写法是 0.55 / 0.56 / 0.75 / 0.76…
# 每个边界之间留了 0.01 的空隙，凡是通过瓦数换算出来的百分比（比如 181W @FTP200
# = 0.905）恰好落进空隙里，就会被兜底判成 Z1 主动恢复——数值越接近阈值越容易中招，
# 而这个标签会一路写进课表、分段统计和训练报告。
# 上界取"区间标称上限 + 0.01"，这样既保持原来的整数百分比归属不变
# （55% 仍是 Z1、90% 仍是 Z3），又把 0.01 的缝填上，不会有数值掉进缝里。
ZONES = [
    ("Z1", "主动恢复", 0.00, 0.56),
    ("Z2", "耐力", 0.56, 0.76),
    ("Z3", "节奏", 0.76, 0.91),
    ("Z4", "乳酸阈值", 0.91, 1.06),
    ("Z5", "最大摄氧", 1.06, 1.21),
    ("Z6", "无氧能力", 1.21, float("inf")),
]


def zone_of(pct_ftp: float) -> Dict[str, Any]:
    """根据 %FTP 判断所属训练区间。

    第一个命中的区间就返回（左闭右开），所以任何数值都能落进恰好一个区间，
    不存在"掉进缝里"的情况。
    """
    for code, name, lo, hi in ZONES:
        if lo <= pct_ftp < hi:
            return {"code": code, "name": name, "label": "{} {}".format(code, name)}
    # 只有 pct_ftp 是 NaN 或负数时才会走到这里
    return {"code": "Z1", "name": "主动恢复", "label": "Z1 主动恢复"}


# --------------------------------------------------------------------------
# 步骤
# --------------------------------------------------------------------------

KIND_WARMUP = "warmup"
KIND_WORK = "work"
KIND_RECOVERY = "recovery"
KIND_COOLDOWN = "cooldown"


def make_step(name: str, kind: str, duration_s: float, pct_ftp: float,
              ftp: float) -> Dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "duration_s": float(duration_s),
        "pct_ftp": float(pct_ftp),
        "target_power": int(round(pct_ftp * ftp)),
        "zone": zone_of(pct_ftp)["label"],
    }


def _repeats(reps: int, work_s: float, work_pct: float,
             rest_s: float, rest_pct: float, ftp: float,
             work_name: str = "高强度", rest_name: str = "恢复",
             work_kind: str = KIND_WORK) -> List[Dict[str, Any]]:
    """标准重复组：最后一组之后不再插恢复段。"""
    steps: List[Dict[str, Any]] = []
    for i in range(reps):
        steps.append(make_step(work_name, work_kind, work_s, work_pct, ftp))
        if rest_s > 0 and i < reps - 1:
            steps.append(make_step(rest_name, KIND_RECOVERY, rest_s, rest_pct, ftp))
    return steps


# --------------------------------------------------------------------------
# 方案定义
# --------------------------------------------------------------------------

def P(key: str, label: str, default: float, minimum: float, maximum: float,
      step: float = 1, unit: str = "") -> Dict[str, Any]:
    return {"key": key, "label": label, "default": default, "min": minimum,
            "max": maximum, "step": step, "unit": unit}


def _warmup(params: Dict[str, float], ftp: float) -> List[Dict[str, Any]]:
    minutes = float(params.get("warmup_min", 10))
    if minutes <= 0:
        return []
    pct = float(params.get("warmup_pct", 60))
    return [make_step("热身", KIND_WARMUP, minutes * 60, pct / 100.0, ftp)]


def _cooldown(params: Dict[str, float], ftp: float) -> List[Dict[str, Any]]:
    minutes = float(params.get("cooldown_min", 5))
    if minutes <= 0:
        return []
    pct = float(params.get("cooldown_pct", 50))
    return [make_step("冷身", KIND_COOLDOWN, minutes * 60, pct / 100.0, ftp)]


def _build_repeat_style(params: Dict[str, float], ftp: float) -> List[Dict[str, Any]]:
    steps = _warmup(params, ftp)
    steps += _repeats(
        reps=int(params.get("reps", 4)),
        work_s=float(params.get("work_s", 240)),
        work_pct=float(params.get("work_pct", 95)) / 100.0,
        rest_s=float(params.get("rest_s", 180)),
        rest_pct=float(params.get("rest_pct", 55)) / 100.0,
        ftp=ftp,
    )
    steps += _cooldown(params, ftp)
    return steps


def _build_over_under(params: Dict[str, float], ftp: float) -> List[Dict[str, Any]]:
    """超阈值：在 FTP 上下反复穿越，最直接地训练乳酸清除。"""
    steps = _warmup(params, ftp)
    sets = int(params.get("sets", 3))
    per_set = int(params.get("reps_per_set", 3))
    under_s = float(params.get("under_s", 120))
    under_pct = float(params.get("under_pct", 95)) / 100.0
    over_s = float(params.get("over_s", 60))
    over_pct = float(params.get("over_pct", 108)) / 100.0
    set_rest_s = float(params.get("set_rest_s", 300))
    set_rest_pct = float(params.get("set_rest_pct", 50)) / 100.0

    for s in range(sets):
        for r in range(per_set):
            steps.append(make_step("阈值下", KIND_WORK, under_s, under_pct, ftp))
            steps.append(make_step("阈值上", KIND_WORK, over_s, over_pct, ftp))
        if set_rest_s > 0 and s < sets - 1:
            steps.append(make_step("组间恢复", KIND_RECOVERY, set_rest_s, set_rest_pct, ftp))
    steps += _cooldown(params, ftp)
    return steps


def _build_tabata(params: Dict[str, float], ftp: float) -> List[Dict[str, Any]]:
    steps = _warmup(params, ftp)
    rounds = int(params.get("rounds", 1))
    for r in range(rounds):
        steps += _repeats(
            reps=int(params.get("reps", 8)),
            work_s=float(params.get("work_s", 20)),
            work_pct=float(params.get("work_pct", 170)) / 100.0,
            rest_s=float(params.get("rest_s", 10)),
            rest_pct=float(params.get("rest_pct", 40)) / 100.0,
            ftp=ftp,
            work_name="全力",
        )
        if r < rounds - 1:
            steps.append(make_step("轮间恢复", KIND_RECOVERY, 300, 0.45, ftp))
    steps += _cooldown(params, ftp)
    return steps


# 公共参数：热身与冷身
_COMMON_WARM = P("warmup_min", "热身", 10, 0, 40, 1, "分钟")
_COMMON_COOL = P("cooldown_min", "冷身", 5, 0, 40, 1, "分钟")


TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "hiit_30_15",
        "name": "30/15 间歇",
        "subtitle": "Rønnestad 式短间歇",
        "desc": "30 秒高强度接 15 秒恢复。恢复时间刻意短于完全清除乳酸所需，"
                "乳酸在整组过程中持续累积，是目前证据最扎实的 VO2max 提升方案之一。"
                "强度定在略高于阈值——踩得动但绝对不轻松。",
        "params": [
            _COMMON_WARM,
            P("reps", "组数", 13, 4, 30, 1, "组"),
            P("work_s", "高强度时长", 30, 10, 180, 5, "秒"),
            P("work_pct", "高强度强度", 105, 90, 150, 1, "% FTP"),
            P("rest_s", "恢复时长", 15, 5, 180, 5, "秒"),
            P("rest_pct", "恢复强度", 50, 30, 80, 1, "% FTP"),
            _COMMON_COOL,
        ],
        "build": _build_repeat_style,
    },
    {
        "id": "hiit_4x4",
        "name": "4×4 分钟",
        "subtitle": "Helgerud 经典方案",
        "desc": "4 分钟高强度接 3 分钟恢复，重复 4 次。强度略低于阈值线，"
                "但单段足够长，能真正把摄氧量顶到高位并维持住。"
                "研究里这是提升 VO2max 效率最高的方案之一。",
        "params": [
            _COMMON_WARM,
            P("reps", "组数", 4, 2, 10, 1, "组"),
            P("work_s", "高强度时长", 240, 60, 1200, 15, "秒"),
            P("work_pct", "高强度强度", 95, 85, 120, 1, "% FTP"),
            P("rest_s", "恢复时长", 180, 30, 600, 15, "秒"),
            P("rest_pct", "恢复强度", 55, 30, 80, 1, "% FTP"),
            _COMMON_COOL,
        ],
        "build": _build_repeat_style,
    },
    {
        "id": "threshold_2x20",
        "name": "2×20 阈值",
        "subtitle": "乳酸阈值课",
        "desc": "两个 20 分钟、刚好压在阈值线上。这条课训练的不是上限，而是"
                "「清除速率追上产生速率」的那个平衡点本身——乳酸阈值提高，"
                "意味着同样的功率下你产生的乳酸更少、或者清得更快。"
                "这是提升 FTP 最直接的课。",
        "params": [
            P("warmup_min", "热身", 12, 0, 40, 1, "分钟"),
            P("reps", "组数", 2, 1, 6, 1, "组"),
            P("work_s", "高强度时长", 1200, 300, 3600, 60, "秒"),
            P("work_pct", "高强度强度", 98, 88, 105, 1, "% FTP"),
            P("rest_s", "恢复时长", 300, 60, 900, 30, "秒"),
            P("rest_pct", "恢复强度", 55, 30, 80, 1, "% FTP"),
            P("cooldown_min", "冷身", 8, 0, 40, 1, "分钟"),
        ],
        "build": _build_repeat_style,
    },
    {
        "id": "hiit_5x5",
        "name": "5×5 分钟",
        "subtitle": "最大摄氧课",
        "desc": "5 分钟一组、共 5 组，强度略高于阈值。单段比 4×4 更长，"
                "累积的乳酸更多，对耐受与清除能力的要求也更高。"
                "如果 4×4 已经踩得很稳，可以上这条。",
        "params": [
            P("warmup_min", "热身", 12, 0, 40, 1, "分钟"),
            P("reps", "组数", 5, 2, 10, 1, "组"),
            P("work_s", "高强度时长", 300, 60, 1200, 15, "秒"),
            P("work_pct", "高强度强度", 105, 90, 125, 1, "% FTP"),
            P("rest_s", "恢复时长", 150, 30, 600, 15, "秒"),
            P("rest_pct", "恢复强度", 50, 30, 80, 1, "% FTP"),
            _COMMON_COOL,
        ],
        "build": _build_repeat_style,
    },
    {
        "id": "over_under",
        "name": "Over-Under 超阈值",
        "subtitle": "乳酸穿梭专练",
        "desc": "在阈值上下反复穿越：阈值下 2 分钟、阈值上 1 分钟，"
                "让你在乳酸还没清干净的时候再次把它推高。"
                "这是乳酸穿梭最直接的练法——身体被迫学会"
                "「一边产生、一边清除」，而不是先清完再重新开始。",
        "params": [
            P("warmup_min", "热身", 12, 0, 40, 1, "分钟"),
            P("sets", "大组数", 3, 1, 8, 1, "组"),
            P("reps_per_set", "每组次数", 3, 1, 10, 1, "次"),
            P("under_s", "阈值下时长", 120, 30, 600, 15, "秒"),
            P("under_pct", "阈值下强度", 95, 85, 100, 1, "% FTP"),
            P("over_s", "阈值上时长", 60, 15, 600, 15, "秒"),
            P("over_pct", "阈值上强度", 108, 100, 130, 1, "% FTP"),
            P("set_rest_s", "组间恢复", 300, 60, 900, 30, "秒"),
            P("set_rest_pct", "组间恢复强度", 50, 30, 80, 1, "% FTP"),
            P("cooldown_min", "冷身", 8, 0, 40, 1, "分钟"),
        ],
        "build": _build_over_under,
    },
    {
        "id": "tabata",
        "name": "Tabata 20/10",
        "subtitle": "极限短间歇",
        "desc": "20 秒全力接 10 秒休息，重复 8 次为一轮。原始研究用的是"
                "170% VO2max 强度的「全力」，恢复极不充分，是这几条里最狠的。"
                "注意：真正的 Tabata 要求每一组都接近全力，如果踩不到预定功率，"
                "说明强度设高了，可以下调「全力」的 %FTP。",
        "params": [
            P("warmup_min", "热身", 10, 0, 40, 1, "分钟"),
            P("rounds", "轮数", 1, 1, 4, 1, "轮"),
            P("reps", "每组次数", 8, 4, 16, 1, "次"),
            P("work_s", "全力时长", 20, 10, 60, 5, "秒"),
            P("work_pct", "全力强度", 170, 120, 300, 5, "% FTP"),
            P("rest_s", "休息时长", 10, 5, 60, 5, "秒"),
            P("rest_pct", "休息强度", 40, 20, 60, 5, "% FTP"),
            P("cooldown_min", "冷身", 8, 0, 40, 1, "分钟"),
        ],
        "build": _build_tabata,
    },
]


def get_template(template_id: str) -> Optional[Dict[str, Any]]:
    for t in TEMPLATES:
        if t["id"] == template_id:
            return t
    return None


def public_templates() -> List[Dict[str, Any]]:
    """给前端的模板列表（去掉不能序列化的构建函数）。"""
    return [
        {k: v for k, v in t.items() if k != "build"}
        for t in TEMPLATES
    ]


def default_params(template_id: str) -> Dict[str, float]:
    t = get_template(template_id)
    if t is None:
        return {}
    return {p["key"]: p["default"] for p in t["params"]}


def clamp_params(template_id: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """把用户填的参数夹到模板声明的合法范围内。

    越界直接夹住而不是报错：一个手滑多打了一个 0，用户想要的是"尽量接近"，
    而不是让整个课表生成失败。返回值也回传给前端，输入框里的数字会跟着修正。
    """
    t = get_template(template_id)
    if t is None:
        raise ValueError("未知的训练方案：{}".format(template_id))

    merged: Dict[str, float] = {}
    for p in t["params"]:
        raw = (params or {}).get(p["key"], p["default"])
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(p["default"])
        value = max(float(p["min"]), min(float(p["max"]), value))
        merged[p["key"]] = value
    return merged


def build_plan(template_id: str, params: Optional[Dict[str, Any]] = None,
               ftp: float = 200.0) -> List[Dict[str, Any]]:
    """按模板和参数生成步骤列表。参数缺失时用模板默认值补齐。"""
    t = get_template(template_id)
    if t is None:
        raise ValueError("未知的训练方案：{}".format(template_id))

    merged = clamp_params(template_id, params)
    ftp = max(50.0, min(600.0, float(ftp)))
    builder: Callable[[Dict[str, float], float], List[Dict[str, Any]]] = t["build"]
    steps = builder(merged, ftp)

    # 低于 20W 的目标功率对 ERG 没有意义（骑行台在低功率区普遍控不准）
    for s in steps:
        if s["target_power"] < 20:
            s["target_power"] = 20
    return steps


def plan_stats(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """方案的汇总信息，给前端做预览。"""
    total = sum(s["duration_s"] for s in steps)
    work = sum(s["duration_s"] for s in steps if s["kind"] == KIND_WORK)
    peaks = [s for s in steps if s["kind"] == KIND_WORK]
    return {
        "total_s": total,
        "work_s": work,
        "recovery_s": sum(s["duration_s"] for s in steps
                          if s["kind"] == KIND_RECOVERY),
        "step_count": len(steps),
        "work_step_count": len(peaks),
        "peak_power": max((s["target_power"] for s in steps), default=0),
        "avg_power": (round(sum(s["target_power"] * s["duration_s"] for s in steps) / total)
                      if total > 0 else 0),
        "est_kj": (round(sum(s["target_power"] * s["duration_s"] for s in steps) / 1000.0, 1)
                   if total > 0 else 0.0),
    }
