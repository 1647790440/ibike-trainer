"""自定义课程：存储、校验、生成训练计划。

设计上有一个刻意的取舍：**功率同时存 %FTP 和绝对瓦数两份，由课程级的
``power_mode`` 决定用哪一份。**

HIIT 内置方案全部按 %FTP 定义，因为"乳酸阈值因人而异"是那套方案的立足点。
但自定义课程追求的是自由——有人就是想要"稳稳踩 150W，不管我的 FTP 是多少"，
也有人想要"永远压在阈值上，FTP 变了课表跟着变"。两种诉求都合理，所以让用户
按课程选，而不是替他们决定。

两份值都存下来（而不是只存一份、用的时候换算），是为了切换模式时不丢信息：
从 %FTP 切到绝对瓦数，之前存的瓦数还在；再切回来，百分比也还在。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import custom_courses_path
from .workouts import KIND_COOLDOWN, KIND_RECOVERY, KIND_WARMUP, KIND_WORK, zone_of

log = logging.getLogger("ibike.custom")

VALID_KINDS = (KIND_WARMUP, KIND_WORK, KIND_RECOVERY, KIND_COOLDOWN)

KIND_LABELS = {
    KIND_WARMUP: "热身",
    KIND_WORK: "高强度",
    KIND_RECOVERY: "恢复",
    KIND_COOLDOWN: "冷身",
}

POWER_MODE_PCT = "pct"
POWER_MODE_ABS = "abs"
VALID_POWER_MODES = (POWER_MODE_PCT, POWER_MODE_ABS)

# 限制住规模，避免一节课塞进几千个小节把前端和骑行台都拖垮
MAX_STEPS = 80
MIN_DURATION_S = 5
MAX_DURATION_S = 3 * 3600
MIN_PCT = 20.0
MAX_PCT = 300.0
MIN_WATTS = 20
MAX_WATTS = 2000
MAX_NAME = 40

# 默认路径见 paths.py；测试通过 IBIKE_DATA_DIR 换成临时目录
DEFAULT_PATH = None


def default_course() -> Dict[str, Any]:
    """新建课程时的初始内容：一节能直接用的三段式课表。"""
    return {
        "id": "",
        "name": "我的课程",
        "desc": "",
        "power_mode": POWER_MODE_PCT,
        "steps": [
            {"name": "热身", "kind": KIND_WARMUP, "duration_s": 600,
             "pct": 60.0, "watts": 120},
            {"name": "高强度", "kind": KIND_WORK, "duration_s": 1200,
             "pct": 95.0, "watts": 190},
            {"name": "冷身", "kind": KIND_COOLDOWN, "duration_s": 300,
             "pct": 50.0, "watts": 100},
        ],
    }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _number(raw: Any, fallback: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    if value != value or value in (float("inf"), float("-inf")):   # NaN / inf
        return fallback
    return value


def normalize_course(payload: Dict[str, Any], ftp: float,
                     existing: Optional[Dict[str, Any]] = None,
                     derive: bool = True) -> Dict[str, Any]:
    """把前端提交的内容整理成一份合法、可存储的课程。

    越界一律夹住而不是报错：用户填了个 999 分钟，想要的是"尽量接近"，
    而不是让整个编辑白费。

    ``derive=False`` 专供读取已存文件时使用：那时两份功率值都是当初存下来的事实，
    不能拿一个固定的 FTP 去重算——否则会把另一份悄悄改掉（比如按 FTP 250 存的
    150W/60%，加载时按 200 重算就变成 75%，用户一切换表示方式功率就变了）。
    """
    ftp = _clamp(_number(ftp, 200.0), 50.0, 600.0)
    mode = str(payload.get("power_mode") or POWER_MODE_PCT)
    if mode not in VALID_POWER_MODES:
        mode = POWER_MODE_PCT

    name = str(payload.get("name") or "").strip()[:MAX_NAME] or "未命名课程"
    desc = str(payload.get("desc") or "").strip()[:200]

    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("课程至少要有一个小节")

    steps: List[Dict[str, Any]] = []
    for raw in raw_steps[:MAX_STEPS]:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or KIND_WORK)
        if kind not in VALID_KINDS:
            kind = KIND_WORK

        duration = _clamp(_number(raw.get("duration_s"), 300.0),
                          MIN_DURATION_S, MAX_DURATION_S)

        if derive:
            # 输入的那一边是权威值，另一边按当前 FTP 推导出来
            if mode == POWER_MODE_ABS:
                watts = _clamp(_number(raw.get("watts"), 150.0), MIN_WATTS, MAX_WATTS)
                pct = _clamp(watts / ftp * 100.0, MIN_PCT, MAX_PCT)
            else:
                pct = _clamp(_number(raw.get("pct"), 70.0), MIN_PCT, MAX_PCT)
                watts = _clamp(pct / 100.0 * ftp, MIN_WATTS, MAX_WATTS)
        else:
            # 两份值都原样保留，只做合法性夹取
            pct = _clamp(_number(raw.get("pct"), 70.0), MIN_PCT, MAX_PCT)
            watts = _clamp(_number(raw.get("watts"), pct / 100.0 * ftp),
                           MIN_WATTS, MAX_WATTS)

        label = str(raw.get("name") or "").strip()[:20] or KIND_LABELS.get(kind, "小节")
        steps.append({
            "name": label,
            "kind": kind,
            "duration_s": round(duration, 1),
            "pct": round(pct, 1),
            "watts": int(round(watts)),
        })

    if not steps:
        raise ValueError("课程至少要有一个有效的小节")

    now = time.time()
    course_id = (existing or {}).get("id") or payload.get("id") or "c" + uuid.uuid4().hex[:10]
    return {
        "id": str(course_id),
        "name": name,
        "desc": desc,
        "power_mode": mode,
        "steps": steps,
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
    }


def build_custom_steps(course: Dict[str, Any], ftp: float) -> List[Dict[str, Any]]:
    """把一节自定义课程展开成训练会话能用的步骤列表。"""
    ftp = _clamp(_number(ftp, 200.0), 50.0, 600.0)
    mode = course.get("power_mode", POWER_MODE_PCT)

    steps: List[Dict[str, Any]] = []
    for raw in course.get("steps", []):
        if mode == POWER_MODE_ABS:
            watts = float(raw.get("watts", 150))
            pct = watts / ftp
        else:
            pct = float(raw.get("pct", 70)) / 100.0
            watts = pct * ftp
        # 先夹取功率，再按**夹取后**的功率回算百分比和区间。
        # 反过来的话（先算 pct/zone 再夹功率），低 FTP 下会自相矛盾：比如 20% @FTP50
        # 本意是 10W，被下限抬到 20W 实际是 40% FTP，标签却还写着"20% 主动恢复"。
        watts = _clamp(watts, MIN_WATTS, MAX_WATTS)
        if mode != POWER_MODE_ABS:
            pct = watts / ftp if ftp > 0 else pct
        steps.append({
            "name": raw.get("name") or "小节",
            "kind": raw.get("kind") or KIND_WORK,
            "duration_s": float(raw.get("duration_s", 300)),
            # 绝对值模式下这个百分比是推导出来的，仅用于显示区间，不参与控制
            "pct_ftp": round(pct, 4),
            "target_power": int(round(watts)),
            "zone": zone_of(pct)["label"],
        })
    return steps


class CustomStore:
    """自定义课程的持久化。用一个 JSON 文件，写入走"临时文件 + 替换"避免写坏。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else custom_courses_path()
        self.courses: List[Dict[str, Any]] = []
        # 读不出来的条目先原样存着，保存时再写回去（见 load()）
        self._unreadable: List[Any] = []
        self.load()

    # ------------------------------------------------------------------

    def load(self) -> None:
        self.courses = []
        if not self.path.exists():
            return
        raw_text = ""
        try:
            raw_text = self.path.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except (OSError, ValueError) as exc:
            # 文件坏了不能让程序起不来，但也不能悄悄当没发生过：用户看到的是
            # "课程全没了"，而紧接着的第一次保存会把唯一现场覆盖掉。所以把坏文件
            # 改名留档，并明确告诉用户去哪里找。
            self._quarantine("无法解析：{}".format(exc))
            return

        raw_list = data.get("courses") if isinstance(data, dict) else data
        if not isinstance(raw_list, list):
            # 结构不对和解析失败一样危险：接着的第一次保存会把文件覆盖掉。
            # 所以走同一套"改名留档"的流程，而不是只打一条日志。
            self._quarantine("结构不对：期望一个课程列表")
            return

        skipped: List[Any] = []
        for raw in raw_list:
            if not isinstance(raw, dict):
                # 一个字符串/数字混进来，以前会直接在 normalize_course 里
                # AttributeError 抛出去——而 CustomStore 是在 Server() 构造函数里
                # new 出来的，后果是整个程序起不来。
                skipped.append(raw)
                continue
            try:
                # 存的东西自己也可能过期或被手改过，过一遍校验再收下。
                # derive=False：不要重算那两份功率值，它们都是当初存下来的事实。
                self.courses.append(
                    normalize_course(raw, 200.0, existing=raw, derive=False))
            except (ValueError, TypeError, AttributeError, KeyError):
                skipped.append(raw)
        if skipped:
            # 关键：把这些条目原样留在内存里，保存时会一起写回去。
            # 否则用户只是"其中一门课被人手改坏了"，下一次保存任何课程都会
            # 把坏掉的那门从磁盘上永久删掉，连备份都没有。
            self._unreadable = skipped
            log.warning("自定义课程里有 %d 条内容无法读取，已原样保留（不会在保存时丢失）。",
                        len(skipped))

    def _quarantine(self, why: str) -> None:
        """文件整体不可用：改名留档，别让紧跟着的保存把它覆盖掉。"""
        backup = self.path.with_name(
            "{}.corrupt.{}".format(self.path.name, int(time.time())))
        try:
            os.replace(self.path, backup)
            detail = "已备份到 {}".format(backup.name)
        except OSError:
            detail = "备份失败，请手工检查该文件"
        log.error("自定义课程文件不可用（%s）：%s，%s。将从空列表开始。",
                  why, self.path, detail)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 读不出来的条目原样写回去，不能让它们因为"这次读不懂"就被删掉
        payload = {"version": 1, "courses": list(self.courses) + list(self._unreadable)}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------

    def list(self) -> List[Dict[str, Any]]:
        return list(self.courses)

    def get(self, course_id: str) -> Optional[Dict[str, Any]]:
        for course in self.courses:
            if course["id"] == course_id:
                return course
        return None

    def upsert(self, payload: Dict[str, Any], ftp: float) -> Dict[str, Any]:
        course_id = str(payload.get("id") or "")
        existing = self.get(course_id) if course_id else None
        course = normalize_course(payload, ftp, existing=existing)
        if existing is not None:
            self.courses[self.courses.index(existing)] = course
        else:
            self.courses.append(course)
        self.save()
        return course

    def delete(self, course_id: str) -> bool:
        course = self.get(course_id)
        if course is None:
            return False
        # 先改内存、再保存，但保存失败必须把内存改回去：否则界面上课程没了、
        # 磁盘上还在，重启之后它又冒出来。
        self.courses.remove(course)
        try:
            self.save()
        except OSError:
            self.courses.append(course)
            log.exception("删除课程后保存失败，已回滚")
            raise
        return True


def course_stats(course: Dict[str, Any], ftp: float) -> Dict[str, Any]:
    """课程概览，给前端列表用（不展开成步骤）。"""
    steps = build_custom_steps(course, ftp)
    total = sum(s["duration_s"] for s in steps)
    work = [s for s in steps if s["kind"] == KIND_WORK]
    return {
        "total_s": total,
        "step_count": len(steps),
        "work_s": sum(s["duration_s"] for s in work),
        "peak_power": max((s["target_power"] for s in steps), default=0),
        "avg_power": (round(sum(s["target_power"] * s["duration_s"] for s in steps) / total)
                      if total > 0 else 0),
    }
