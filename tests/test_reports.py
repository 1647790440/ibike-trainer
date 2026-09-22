#!/usr/bin/env python3
"""训练报告的测试：本地保存、查看、删除，以及 id 的路径安全。

    python3 tests/test_reports.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# 测试隔离：绝不能碰用户的真实数据目录。
# 训练报告是"训练一结束就自动保存"的，不隔离的话跑一次测试就会往
# data/reports/ 里留下一条真实报告。数据根目录在构造存储对象时读这个环境变量。
# ---------------------------------------------------------------------------
import os as _os
import tempfile as _tempfile

_os.environ.setdefault("IBIKE_DATA_DIR",
                       _tempfile.mkdtemp(prefix="ibike-test-data-"))

import ibike.reports as reports_mod  # noqa: E402
from aiohttp import ClientSession, web  # noqa: E402

from ibike.reports import ReportStore  # noqa: E402
from ibike.server import Server  # noqa: E402

_failures: List[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print("  {} {}{}".format("\033[32m✔\033[0m" if cond else "\033[31m✘\033[0m",
                             label, "  → " + detail if detail else ""))
    if not cond:
        _failures.append(label)
    return cond


def make_summary(**over) -> Dict[str, Any]:
    summary = {
        "reason": "完成目标时长",
        "completed": True,
        "plan_name": "恒定功率",
        "is_interval": False,
        "target_power": 100.0,
        "planned_s": 600.0,
        "actual_s": 600.0,
        "skipped_s": 0.0,
        "started_at": 1700000000.0,
        "finished_at": 1700000600.0,
        "avg_power": 100.2,
        "max_power": 130.0,
        "normalized_power": 103.0,
        "avg_cadence": 88.0,
        "energy_kj": 60.1,
        "distance_m": 5000.0,
        "in_zone_pct": 92.5,
        "intervals": [],
        "distribution": [],
        "trace": [{"t": 0, "p": 100, "target": 100}, {"t": 1, "p": 101, "target": 100}],
    }
    summary.update(over)
    return summary


def temp_store() -> ReportStore:
    return ReportStore(Path(tempfile.mkdtemp()) / "reports")


# ======================================================================
# 存储
# ======================================================================


def test_store_basics() -> None:
    print("\n[1] 保存 / 列出 / 读取 / 删除")
    store = temp_store()
    check(store.list() == [], "初始没有报告")

    saved = store.save(make_summary())
    check(saved is not None and bool(saved["id"]), "保存成功并分配了 id",
          str(saved and saved["id"]))
    check(saved["plan_name"] == "恒定功率", "元信息带上了方案名")
    check(saved["avg_power"] == 100.2, "元信息带上了平均功率")

    listed = store.list()
    check(len(listed) == 1, "列表里出现一条")
    check("trace" not in listed[0], "列表不带功率曲线（省流量）")
    check("summary" not in listed[0], "列表不带完整总结")

    full = store.get(saved["id"])
    check(full is not None, "能按 id 读回完整报告")
    check(len(full["summary"]["trace"]) == 2, "完整报告里有功率曲线")
    check(full["summary"]["plan_name"] == "恒定功率", "内容一致")

    check(store.delete(saved["id"]) is True, "删除成功")
    check(store.list() == [], "删除后列表为空")
    check(store.delete(saved["id"]) is False, "重复删除返回 False")


def test_ordering_and_clear() -> None:
    print("\n[2] 按时间倒序 / 清空全部")
    store = temp_store()
    for i in range(3):
        store.save(make_summary(finished_at=1700000000.0 + i * 3600,
                                plan_name="第{}次".format(i + 1)))
    listed = store.list()
    check(len(listed) == 3, "三条都在")
    check([r["plan_name"] for r in listed] == ["第3次", "第2次", "第1次"],
          "最新的排在最前", str([r["plan_name"] for r in listed]))

    removed = store.clear()
    check(removed == 3, "清空返回删除数量", str(removed))
    check(store.list() == [], "清空后列表为空")
    check(store.get(listed[0]["id"]) is None, "清空后按 id 也读不到")


def test_path_traversal_blocked() -> None:
    print("\n[3] id 不能用来读目录外的文件")
    root = Path(tempfile.mkdtemp())
    store = ReportStore(root / "reports")
    store.save(make_summary())

    # 放一个"诱饵"文件在报告目录之外
    secret = root / "secret.json"
    secret.write_text(json.dumps({"summary": {"plan_name": "不该被读到"}}),
                      encoding="utf-8")

    evil_ids = [
        "../secret",
        "../../secret",
        "..%2Fsecret",
        "/etc/passwd",
        "a/b",
        "a\\b",
        "..",
        "",
        "x" * 100,
    ]
    for evil in evil_ids:
        check(store.get(evil) is None, "拒绝 id={!r}".format(evil))
        check(store.delete(evil) is False, "删除也拒绝 id={!r}".format(evil))
    check(secret.exists(), "诱饵文件没有被删掉")


def test_corrupt_and_foreign_files() -> None:
    print("\n[4] 坏文件和不相关文件不会让列表崩掉")
    directory = Path(tempfile.mkdtemp()) / "reports"
    directory.mkdir(parents=True)
    store = ReportStore(directory)
    store.save(make_summary(plan_name="好的"))

    (directory / "r-broken.json").write_text("{不是合法 json", encoding="utf-8")
    (directory / "r-empty.json").write_text("{}", encoding="utf-8")
    (directory / "notes.txt").write_text("无关文件", encoding="utf-8")

    listed = store.list()
    check(len(listed) == 1, "只有合法的那份被列出", "{} 条".format(len(listed)))
    check(listed[0]["plan_name"] == "好的", "内容正确")


def test_prune_keeps_newest() -> None:
    print("\n[5] 超过上限时丢掉最旧的（不是静默删数据）")
    original = reports_mod.MAX_REPORTS
    reports_mod.MAX_REPORTS = 3
    try:
        store = temp_store()
        for i in range(6):
            store.save(make_summary(finished_at=1700000000.0 + i, plan_name="第{}次".format(i)))
        listed = store.list()
        check(len(listed) == 3, "只保留上限内的条数", "{} 条".format(len(listed)))
        check([r["plan_name"] for r in listed] == ["第5次", "第4次", "第3次"],
              "保留的是最新的三份", str([r["plan_name"] for r in listed]))
    finally:
        reports_mod.MAX_REPORTS = original


# ======================================================================
# HTTP
# ======================================================================


def make_server() -> Server:
    from ibike.custom import CustomStore

    server = Server(use_simulator=True)
    # 用临时目录，别动用户的真实数据
    server.reports = temp_store()
    server.custom = CustomStore(Path(tempfile.mkdtemp()) / "custom.json")
    return server


async def with_server(fn) -> None:
    server = make_server()
    app = server.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = "http://127.0.0.1:{}".format(runner.addresses[0][1])
    try:
        async with ClientSession() as http:
            await fn(http, base, server)
    finally:
        await runner.cleanup()


async def test_http_crud() -> None:
    print("\n[6] HTTP：列表 / 查看 / 删除 / 清空")

    async def scenario(http, base, server):
        async with http.get(base + "/api/reports") as resp:
            data = await resp.json()
        check(resp.status == 200 and data.get("reports") == [], "初始列表为空")

        saved = server.reports.save(make_summary(plan_name="测试课"))
        async with http.get(base + "/api/reports") as resp:
            data = await resp.json()
        check(len(data["reports"]) == 1, "列表里出现了报告")
        check(data["reports"][0]["plan_name"] == "测试课", "内容正确")

        async with http.get(base + "/api/reports/" + saved["id"]) as resp:
            data = await resp.json()
        check(resp.status == 200 and data["ok"], "能读到完整报告")
        check("trace" in data["report"]["summary"], "完整报告里带功率曲线")

        async with http.get(base + "/api/reports/nope") as resp:
            check(resp.status == 404, "读不存在的报告返回 404")

        # 路径穿越通过 HTTP 也要被挡住
        async with http.get(base + "/api/reports/..%2f..%2fsecret") as resp:
            check(resp.status == 404, "路径穿越 id 返回 404",
                  "HTTP {}".format(resp.status))

        async with http.post(base + "/api/reports/delete",
                             json={"id": saved["id"]}) as resp:
            data = await resp.json()
        check(data.get("ok"), "删除成功")
        check(data.get("reports") == [], "删除后返回的列表已空")

        async with http.post(base + "/api/reports/delete", json={"id": "nope"}) as resp:
            check(resp.status == 404, "删除不存在的报告返回 404")

        server.reports.save(make_summary())
        server.reports.save(make_summary(finished_at=1700009999.0))
        async with http.post(base + "/api/reports/clear", json={}) as resp:
            data = await resp.json()
        check(data.get("removed") == 2, "清空返回删除数量", str(data.get("removed")))
        async with http.get(base + "/api/reports") as resp:
            data = await resp.json()
        check(data["reports"] == [], "清空后列表为空")

    await with_server(scenario)


async def test_autosave_on_finish() -> None:
    print("\n[7] 训练一结束就自动保存报告")

    async def scenario(http, base, server):
        # 用一节 6 秒的课程，几秒内跑完
        async with http.post(base + "/api/custom", json={
            "name": "自动保存测试", "power_mode": "abs",
            "steps": [{"name": "稳定", "kind": "work", "duration_s": 6, "watts": 120}],
            "ftp": 200,
        }) as r:
            course = (await r.json())["course"]

        async with http.get(base + "/api/reports") as resp:
            before = (await resp.json())["reports"]
        check(before == [], "开始前没有报告")

        async with http.post(base + "/api/start", json={
            "mode": "custom", "custom_id": course["id"], "ftp": 200, "erg_mode": "auto",
        }) as r:
            check((await r.json()).get("ok"), "训练启动")

        # 等它自然跑完（走的是"跑满时长"路径，不经过 /api/stop）
        for _ in range(40):
            await asyncio.sleep(0.5)
            async with http.get(base + "/api/state") as r:
                st = await r.json()
            if st.get("state") == "finished":
                break
        check(st.get("state") == "finished", "训练自然结束")

        async with http.get(base + "/api/reports") as resp:
            after = (await resp.json())["reports"]
        check(len(after) == 1, "自然结束后自动保存了一份报告",
              "{} 份".format(len(after)))
        if after:
            r0 = after[0]
            check(r0["plan_name"] == "自动保存测试", "报告关联了正确的课程",
                  str(r0["plan_name"]))
            check(r0["completed"] is True, "标记为已完成")
            check(r0["actual_s"] and r0["actual_s"] > 0, "记录了实际时长",
                  "{:.1f}s".format(r0["actual_s"] or 0))
            full = (await (await http.get(
                base + "/api/reports/" + r0["id"])).json())["report"]
            check(len(full["summary"].get("trace") or []) > 0,
                  "报告里带功率曲线",
                  "{} 点".format(len(full["summary"].get("trace") or [])))

        # 再点一次停止不应该又存一份
        await http.post(base + "/api/stop", json={})
        async with http.get(base + "/api/reports") as resp:
            again = (await resp.json())["reports"]
        check(len(again) == 1, "同一次训练不会重复保存",
              "{} 份".format(len(again)))

    await with_server(scenario)


async def test_report_survives_restart() -> None:
    print("\n[8] 报告在重启后依然存在")
    directory = Path(tempfile.mkdtemp()) / "reports"
    store = ReportStore(directory)
    saved = store.save(make_summary(plan_name="重启前"))
    reloaded = ReportStore(directory)
    listed = reloaded.list()
    check(len(listed) == 1, "重新加载后报告还在")
    check(listed[0]["plan_name"] == "重启前", "内容一致")
    check(reloaded.get(saved["id"]) is not None, "按 id 也能读回")


def test_sample_marker() -> None:
    print("\n[9] 示例报告带标记，可以单独清理")
    store = temp_store()
    real = store.save(make_summary(finished_at=1700000000.0, plan_name="真实训练"))
    synth = store.save(make_summary(finished_at=1700000100.0, plan_name="示例"),
                       sample=True)

    by_id = {r["id"]: r for r in store.list()}
    check(by_id[synth["id"]]["sample"] is True, "示例报告带 sample 标记")
    check(by_id[real["id"]]["sample"] is False, "真实报告不带这个标记")

    removed = store.remove_samples()
    check(removed == 1, "只删掉示例报告", "删了 {} 份".format(removed))
    check(store.get(real["id"]) is not None, "真实训练的报告没被动")
    check(store.get(synth["id"]) is None, "示例报告已删除")


def test_data_dir_is_isolated() -> None:
    """测试必须跑在临时数据目录里。

    训练报告是"训练一结束就自动保存"的，一旦测试用了真实目录，跑一次测试就会
    往用户的数据里塞一条记录。这里直接校验隔离机制本身是生效的。
    """
    import os

    from ibike.paths import ENV_VAR, data_dir

    configured = os.environ.get(ENV_VAR, "")
    check(bool(configured), "测试进程设置了 {}".format(ENV_VAR), configured)
    check("ibike-test-data-" in configured, "指向的是临时目录", configured)
    check(str(data_dir()) == configured, "数据目录确实受这个环境变量控制",
          str(data_dir()))


async def main() -> int:
    print("=" * 70)
    print("训练报告测试")
    print("=" * 70)
    test_store_basics()
    test_ordering_and_clear()
    test_path_traversal_blocked()
    test_corrupt_and_foreign_files()
    test_prune_keeps_newest()
    await test_http_crud()
    await test_autosave_on_finish()
    await test_report_survives_restart()
    test_sample_marker()
    test_data_dir_is_isolated()

    print("\n" + "=" * 70)
    if _failures:
        print("失败 {} 项：".format(len(_failures)))
        for f in _failures:
            print("   - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
