"""训练报告的本地存储。

一次训练结束就写一份 JSON 到 `data/reports/`。用"一次训练一个文件"而不是把
所有报告塞进一个大文件，原因是报告只会越来越多：单文件方案每存一次都要重写全部
历史，数据量一大就变慢，而且写坏一次全部丢失。

两个必须守住的地方：

* **id 来自客户端，绝不能直接拼进路径。** 否则 `GET /api/reports/../../etc/passwd`
  这类请求就可能读到目录外的文件。这里对 id 做白名单校验，并且解析之后还要确认
  结果仍在报告目录之内——两道保险。
* **写入必须原子。** 先写临时文件再 `os.replace`，中途断电不会留下半截文件。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import reports_dir

log = logging.getLogger("ibike.reports")

# 默认目录见 paths.py；测试通过 IBIKE_DATA_DIR 换成临时目录
DEFAULT_DIR = None

# 报告 id 只允许这些字符。这是防目录穿越的第一道防线。
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 上限：一份报告大约 20-30KB（含降采样后的功率曲线），500 份约 15MB。
# 超出后丢掉最旧的，并明确记一条日志——不静默删用户数据。
MAX_REPORTS = 500


class ReportStore:
    def __init__(self, directory: Optional[Path] = None) -> None:
        self.dir = Path(directory) if directory else reports_dir()

    # ------------------------------------------------------------------
    # 路径安全
    # ------------------------------------------------------------------

    def _path_for(self, report_id: str) -> Optional[Path]:
        """把 id 映射成文件路径，任何可疑的 id 一律拒绝。"""
        if not isinstance(report_id, str) or not ID_PATTERN.match(report_id):
            return None
        candidate = (self.dir / "{}.json".format(report_id)).resolve()
        try:
            root = self.dir.resolve()
        except OSError:
            return None
        # 第二道保险：解析完（含符号链接）之后必须仍在报告目录内
        if root != candidate.parent:
            return None
        return candidate

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def save(self, summary: Dict[str, Any],
             sample: bool = False) -> Optional[Dict[str, Any]]:
        """保存一份训练报告，返回它的元信息。

        ``sample=True`` 表示这是 tools/make_sample_reports.py 生成的合成数据。
        标记出来是为了让界面能显示一个"示例"角标——否则过几天看到一条自己没骑过
        的记录，会以为是程序出错了。
        """
        if not isinstance(summary, dict) or not summary:
            return None

        finished = summary.get("finished_at") or time.time()
        report = {
            "version": 1,
            "id": "r{}-{}".format(int(finished), uuid.uuid4().hex[:6]),
            "saved_at": time.time(),
            "sample": bool(sample),
            "summary": summary,
        }

        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / "{}.json".format(report["id"])
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            log.exception("保存训练报告失败")
            return None

        self._prune()
        return self.meta(report)

    def _prune(self) -> None:
        entries = self._files()
        if len(entries) <= MAX_REPORTS:
            return
        # 文件名以时间戳开头，按名字排序就是按时间排序
        for path in entries[:len(entries) - MAX_REPORTS]:
            try:
                path.unlink()
                log.info("训练报告超过 %d 份，已删除最旧的一份：%s",
                         MAX_REPORTS, path.name)
            except OSError:
                log.warning("清理旧训练报告失败：%s", path.name, exc_info=True)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def _files(self) -> List[Path]:
        if not self.dir.exists():
            return []
        try:
            files = [p for p in self.dir.iterdir()
                     if p.is_file() and p.suffix == ".json"]
        except OSError:
            log.warning("读取训练报告目录失败", exc_info=True)
            return []
        # 文件名 r<时间戳>-<随机>，排序即按时间
        return sorted(files, key=lambda p: p.name)

    def _read(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("训练报告文件无法解析，已跳过：%s", path.name)
            return None
        if not isinstance(data, dict) or not isinstance(data.get("summary"), dict):
            log.warning("训练报告结构不对，已跳过：%s", path.name)
            return None
        data.setdefault("id", path.stem)
        return data

    @staticmethod
    def meta(report: Dict[str, Any]) -> Dict[str, Any]:
        """列表用的精简信息——**不带功率曲线**，列表接口不需要几十 KB 的轨迹。"""
        summary = report.get("summary") or {}
        return {
            "id": report.get("id"),
            "saved_at": report.get("saved_at"),
            "sample": bool(report.get("sample")),
            "plan_name": summary.get("plan_name"),
            "is_interval": summary.get("is_interval"),
            "completed": summary.get("completed"),
            "reason": summary.get("reason"),
            "started_at": summary.get("started_at"),
            "finished_at": summary.get("finished_at"),
            "actual_s": summary.get("actual_s"),
            "planned_s": summary.get("planned_s"),
            "target_power": summary.get("target_power"),
            "avg_power": summary.get("avg_power"),
            "normalized_power": summary.get("normalized_power"),
            "max_power": summary.get("max_power"),
            "avg_cadence": summary.get("avg_cadence"),
            "energy_kj": summary.get("energy_kj"),
            "distance_m": summary.get("distance_m"),
            "in_zone_pct": summary.get("in_zone_pct"),
            "work_in_zone_pct": summary.get("work_in_zone_pct"),
            "erg_mode_label": summary.get("erg_mode_label"),
        }

    def list(self) -> List[Dict[str, Any]]:
        """按时间倒序返回所有报告的元信息（最新的在前）。"""
        out = []
        for path in self._files():
            report = self._read(path)
            if report is not None:
                out.append(self.meta(report))
        out.sort(key=lambda r: r.get("finished_at") or r.get("saved_at") or 0, reverse=True)
        return out

    def get(self, report_id: str) -> Optional[Dict[str, Any]]:
        path = self._path_for(report_id)
        if path is None or not path.exists():
            return None
        return self._read(path)

    # ------------------------------------------------------------------
    # 删
    # ------------------------------------------------------------------

    def delete(self, report_id: str) -> bool:
        path = self._path_for(report_id)
        if path is None or not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError:
            log.warning("删除训练报告失败：%s", report_id, exc_info=True)
            return False

    def remove_samples(self) -> int:
        """删除所有被标记为示例的报告，用户真实训练的记录不动。"""
        removed = 0
        for path in self._files():
            report = self._read(path)
            if report is None or not report.get("sample"):
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                log.warning("删除示例报告失败：%s", path.name, exc_info=True)
        return removed

    def clear(self) -> int:
        path = self.dir
        if not path.exists():
            return 0
        count = len(self._files())
        try:
            shutil.rmtree(path)
        except OSError:
            log.warning("清空训练报告失败", exc_info=True)
            return 0
        return count
