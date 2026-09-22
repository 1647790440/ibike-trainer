#!/usr/bin/env python3
"""自定义课程的测试：存储、两种功率表示方式、编辑器预览、真正跑一次。

    python3 tests/test_custom_courses.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
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

from aiohttp import ClientSession, web  # noqa: E402

from ibike.custom import (CustomStore, build_custom_steps, default_course,  # noqa: E402
                          normalize_course)
from ibike.server import Server  # noqa: E402

_failures: List[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print("  {} {}{}".format("\033[32m✔\033[0m" if cond else "\033[31m✘\033[0m",
                             label, "  → " + detail if detail else ""))
    if not cond:
        _failures.append(label)
    return cond


def temp_store() -> CustomStore:
    return CustomStore(Path(tempfile.mkdtemp()) / "custom.json")


COURSE = {
    "name": "我的稳定课",
    "desc": "稳稳踩",
    "power_mode": "abs",
    "steps": [
        {"name": "热身", "kind": "warmup", "duration_s": 600, "watts": 100},
        {"name": "主课", "kind": "work", "duration_s": 1800, "watts": 150},
        {"name": "冷身", "kind": "cooldown", "duration_s": 300, "watts": 90},
    ],
}


# ======================================================================
# 存储与规范化
# ======================================================================


def test_store_roundtrip() -> None:
    print("\n[1] 保存 / 重新加载 / 删除")
    path = Path(tempfile.mkdtemp()) / "custom.json"
    store = CustomStore(path)
    check(store.list() == [], "初始为空")

    saved = store.upsert(COURSE, ftp=200)
    check(bool(saved["id"]), "自动分配了 id", saved["id"])
    check(saved["name"] == "我的稳定课", "名称正确")
    check(len(saved["steps"]) == 3, "3 个小节")
    check(path.exists(), "课程已落盘", str(path))

    reloaded = CustomStore(path)
    check(len(reloaded.list()) == 1, "重新加载后课程还在")
    check(reloaded.get(saved["id"])["steps"][1]["watts"] == 150, "内容一致")

    # 更新而不是新增
    updated = store.upsert(dict(COURSE, id=saved["id"], name="改过名字"), ftp=200)
    check(updated["id"] == saved["id"], "同一个 id 是更新")
    check(len(store.list()) == 1, "没有产生重复课程")
    check(store.get(saved["id"])["name"] == "改过名字", "名称已更新")

    check(store.delete(saved["id"]) is True, "删除成功")
    check(store.list() == [], "删除后为空")
    check(store.delete("不存在") is False, "删除不存在的 id 返回 False")


def test_power_modes() -> None:
    print("\n[2] 两种功率表示方式")
    store = temp_store()

    abs_course = store.upsert(COURSE, ftp=200)
    at200 = build_custom_steps(abs_course, 200)
    at300 = build_custom_steps(abs_course, 300)
    check([s["target_power"] for s in at200] == [100, 150, 90],
          "绝对瓦数模式下功率固定", str([s["target_power"] for s in at200]))
    check([s["target_power"] for s in at300] == [100, 150, 90],
          "换 FTP 也不变（这正是绝对模式的意义）",
          str([s["target_power"] for s in at300]))
    check(at300[1]["pct_ftp"] == 0.5, "绝对值模式下推导出百分比用于显示区间",
          str(at300[1]["pct_ftp"]))

    pct_course = store.upsert({
        "name": "阈值课", "power_mode": "pct",
        "steps": [{"name": "阈值", "kind": "work", "duration_s": 1200, "pct": 95}],
    }, ftp=200)
    check([s["target_power"] for s in build_custom_steps(pct_course, 200)] == [190],
          "百分比模式按 FTP=200 折算", "190W")
    check([s["target_power"] for s in build_custom_steps(pct_course, 300)] == [285],
          "百分比模式跟着 FTP 缩放（FTP=300 → 285W）")

    # 两份值都保留，切换模式不丢信息。
    # 注意"pct > 0 且 watts > 0"这种断言是恒真的（两边都被 clamp 保证为正），
    # 所以这里改成验证真正有意义的事：存下来的是**当初那两个具体数值**。
    kept = store.upsert(dict(COURSE, id=abs_course["id"]), ftp=250)
    check(kept["steps"][1]["watts"] == 150, "绝对模式下的权威值没被 FTP 覆盖",
          str(kept["steps"][1]["watts"]))
    check(kept["steps"][1]["pct"] == 60, "绝对模式下推导出的百分比按原 FTP=250 存下来",
          str(kept["steps"][1]["pct"]))
    back = store.get(abs_course["id"])
    check(back is not None and back["steps"][1]["watts"] == 150,
          "重新读回来还是同一个瓦数", str(back["steps"][1]["watts"] if back else None))


def test_validation() -> None:
    print("\n[3] 校验与越界处理")
    store = temp_store()

    bad = store.upsert({
        "name": "x" * 100,
        "steps": [{"kind": "不认识", "duration_s": 999999, "pct": 99999}],
    }, ftp=200)
    check(len(bad["name"]) <= 40, "超长名称被截断", "{} 字".format(len(bad["name"])))
    step = bad["steps"][0]
    check(step["kind"] == "work", "未知类型退回高强度", step["kind"])
    check(step["duration_s"] == 3 * 3600, "超长时长被夹到上限",
          "{}s".format(step["duration_s"]))
    check(step["pct"] == 300.0, "超范围百分比被夹住", str(step["pct"]))
    check(step["name"] == "高强度", "空名称按类型自动补上", step["name"])

    try:
        store.upsert({"name": "空的", "steps": []}, ftp=200)
        check(False, "空课程应当被拒绝")
    except ValueError as exc:
        check("至少" in str(exc), "空课程被拒绝且提示清楚", str(exc))

    # 小节数量上限
    many = store.upsert({
        "name": "很多节",
        "steps": [{"kind": "work", "duration_s": 60, "pct": 80}] * 200,
    }, ftp=200)
    check(len(many["steps"]) <= 80, "小节数量被限制", "{} 节".format(len(many["steps"])))

    # 坏文件不该让程序起不来——但也不能悄悄把它删掉：用户看到的会是"课程全没了"，
    # 而紧跟着的第一次保存就会把唯一现场覆盖掉。所以必须留下备份。
    # （只断言"列表是空的"是不够的：把 os.replace 改成 unlink 也能通过。）
    workdir = Path(tempfile.mkdtemp())
    broken = workdir / "broken.json"
    broken.write_text("{这不是合法 json", encoding="utf-8")
    try:
        recovered = CustomStore(broken)
        check(recovered.list() == [], "损坏的存储文件被安全忽略，程序照常启动")
        backups = list(workdir.glob("broken.json.corrupt*"))
        check(len(backups) == 1, "坏文件被改名留档（而不是被删掉）",
              str([b.name for b in backups]))
        if backups:
            check(backups[0].read_text(encoding="utf-8").startswith("{这不是"),
                  "备份里就是原来的内容")
        check(not broken.exists(), "原文件已让位")
    except Exception as exc:  # noqa: BLE001
        check(False, "损坏的存储文件被安全忽略", str(exc))

    # 原子写：不能直接写目标文件（中途断电会留下半截 JSON，所有课程一起没了）
    import ibike.custom as custom_mod
    original_replace = custom_mod.os.replace
    calls = []
    custom_mod.os.replace = lambda a, b: (calls.append((str(a), str(b))), original_replace(a, b))[1]
    try:
        store_atomic = CustomStore(Path(tempfile.mkdtemp()) / "c.json")
        store_atomic.upsert(dict(COURSE), ftp=200)
        check(any(src.endswith(".tmp") for src, _ in calls),
              "写盘走的是「先写临时文件再替换」", str(calls[-1] if calls else "无"))
    finally:
        custom_mod.os.replace = original_replace


def test_default_course() -> None:
    print("\n[4] 新建课程的默认内容")
    course = default_course()
    check(len(course["steps"]) == 3, "默认给了 3 节", "{} 节".format(len(course["steps"])))
    check([s["kind"] for s in course["steps"]] == ["warmup", "work", "cooldown"],
          "结构是热身→高强度→冷身")
    check(course["power_mode"] == "pct", "默认按 %FTP")
    steps = build_custom_steps(normalize_course(course, 200), 200)
    check(len(steps) == 3 and all(s["target_power"] > 0 for s in steps),
          "默认课程能直接展开成合法步骤",
          str([s["target_power"] for s in steps]))


# ======================================================================
# HTTP
# ======================================================================


async def with_server(fn) -> None:
    server = Server(use_simulator=True)
    server.custom = temp_store()          # 用临时文件，别动真实数据
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
    print("\n[5] HTTP：新建 / 保存 / 列出 / 预览 / 删除")

    async def scenario(http, base, server):
        async with http.get(base + "/api/custom/new") as r:
            data = await r.json()
        check(data.get("ok") and len(data["course"]["steps"]) == 3,
              "新建接口返回默认课程")

        async with http.get(base + "/api/custom") as r:
            data = await r.json()
        check(data.get("courses") == [], "初始没有课程")
        # 以前这里会把数据目录的绝对路径一起返回，号称"便于排查"。实际上前端
        # 从来不读它，而它会把用户名和目录结构暴露给局域网里任何一个客户端，
        # 所以移除了。这条断言反过来守住"别再把它加回来"。
        check("path" not in data, "不再把数据目录的绝对路径发给客户端",
              str(sorted(data.keys())))

        async with http.post(base + "/api/custom",
                             json=dict(COURSE, ftp=200)) as r:
            data = await r.json()
        check(data.get("ok"), "保存课程成功")
        course = data["course"]
        check(bool(course["id"]), "返回了课程 id", course["id"])

        async with http.get(base + "/api/custom?ftp=200") as r:
            data = await r.json()
        check(len(data["courses"]) == 1, "列表里出现了新课程")
        check(data["courses"][0]["stats"]["step_count"] == 3, "列表带上了统计")

        # 预览草稿（未保存）
        draft = {
            "name": "草稿", "power_mode": "pct",
            "steps": [{"name": "A", "kind": "work", "duration_s": 60, "pct": 100},
                      {"name": "B", "kind": "recovery", "duration_s": 30, "pct": 50}],
        }
        async with http.post(base + "/api/plan", json={"course": draft, "ftp": 200}) as r:
            data = await r.json()
        check(data.get("ok"), "草稿预览成功")
        check([s["target_power"] for s in data["steps"]] == [200, 100],
              "草稿按 FTP 正确折算", str([s["target_power"] for s in data["steps"]]))
        async with http.get(base + "/api/custom") as r:
            again = await r.json()
        check(len(again["courses"]) == 1, "预览草稿不会把它存进去")

        # 预览已保存的课程
        async with http.post(base + "/api/plan",
                             json={"custom_id": course["id"], "ftp": 300}) as r:
            data = await r.json()
        check([s["target_power"] for s in data["steps"]] == [100, 150, 90],
              "已保存的绝对瓦数课程预览正确",
              str([s["target_power"] for s in data["steps"]]))

        # 预览不存在的课程
        async with http.post(base + "/api/plan",
                             json={"custom_id": "nope", "ftp": 200}) as r:
            check(r.status == 404, "预览不存在的课程返回 404")

        # 保存非法内容
        async with http.post(base + "/api/custom",
                             json={"name": "空的", "steps": [], "ftp": 200}) as r:
            data = await r.json()
        check(r.status == 400 and not data.get("ok"), "拒绝空课程",
              str(data.get("error")))

        # 删除
        async with http.post(base + "/api/custom/delete", json={"id": course["id"]}) as r:
            data = await r.json()
        check(data.get("ok"), "删除成功")
        async with http.get(base + "/api/custom") as r:
            data = await r.json()
        check(data["courses"] == [], "列表已空")
        async with http.post(base + "/api/custom/delete", json={"id": "nope"}) as r:
            check(r.status == 404, "删除不存在的课程返回 404")

    await with_server(scenario)


async def test_run_custom_course() -> None:
    print("\n[6] 用自定义课程真正跑一次训练")

    async def scenario(http, base, server):
        # 缩到 5 秒一节，好在几秒内跑完
        short = {
            "name": "短测试课", "power_mode": "abs",
            "steps": [
                {"name": "热身", "kind": "warmup", "duration_s": 5, "watts": 60},
                {"name": "冲刺", "kind": "work", "duration_s": 8, "watts": 200},
                {"name": "冷身", "kind": "cooldown", "duration_s": 5, "watts": 60},
            ],
        }
        async with http.post(base + "/api/custom", json=dict(short, ftp=200)) as r:
            course = (await r.json())["course"]

        async with http.post(base + "/api/start",
                             json={"mode": "custom", "custom_id": course["id"],
                                   "ftp": 200, "erg_mode": "auto"}) as r:
            data = await r.json()
        check(data.get("ok"), "自定义课程启动成功", str(data.get("error")))

        async with http.get(base + "/api/state") as r:
            st = await r.json()
        check(st.get("state") == "running", "状态为 running")
        check(st.get("is_interval") is True, "多节课程被识别为分段训练")
        check(st.get("step_name") == "热身", "从第一节开始", str(st.get("step_name")))
        check(st.get("plan_name") == "短测试课", "方案名用的是课程名",
              str(st.get("plan_name")))

        # 等它自己跑完
        for _ in range(40):
            await asyncio.sleep(1.0)
            async with http.get(base + "/api/state") as r:
                st = await r.json()
            if st.get("state") == "finished":
                break

        check(st.get("state") == "finished", "课程跑完自动结束", str(st.get("state")))
        summary = st.get("summary") or {}
        check(summary.get("plan_name") == "短测试课", "总结里记录了课程名")
        check(len(summary.get("intervals") or []) == 3, "总结里三节各一行",
              "{} 行".format(len(summary.get("intervals") or [])))
        work_rows = [r for r in summary["intervals"] if r["kind"] == "work"]
        check(all(r["in_zone_pct"] is not None for r in work_rows),
              "高强度节给出了达标率")
        check(all(r["in_zone_pct"] is None for r in summary["intervals"]
                  if r["kind"] != "work"), "非高强度节不给达标率")
        check((summary.get("config") or {}).get("custom_id") == course["id"],
              "总结里记下了课程 id（供「再来一次」用）")

        # 再来一次要能复现同一门课
        async with http.post(base + "/api/repeat", json={}) as r:
            data = await r.json()
        check(data.get("ok"), "「再来一次」可用", str(data.get("error")))
        async with http.get(base + "/api/state") as r:
            st2 = await r.json()
        check(st2.get("plan_name") == "短测试课", "复现的是同一门课程")
        await http.post(base + "/api/stop", json={})

    await with_server(scenario)


async def test_single_step_course() -> None:
    print("\n[7] 只有一节的课程等价于恒定功率")

    async def scenario(http, base, server):
        async with http.post(base + "/api/custom", json={
            "name": "单节", "power_mode": "abs",
            "steps": [{"name": "稳定", "kind": "work", "duration_s": 30, "watts": 120}],
            "ftp": 200,
        }) as r:
            course = (await r.json())["course"]
        async with http.post(base + "/api/start",
                             json={"mode": "custom", "custom_id": course["id"],
                                   "ftp": 200, "erg_mode": "auto"}) as r:
            await r.json()
        async with http.get(base + "/api/state") as r:
            st = await r.json()
        check(st.get("step_count") == 1, "只有一节")
        check(st.get("is_interval") is False, "单节课程不显示分段界面")
        check(st.get("target_power") == 120, "目标功率正确", str(st.get("target_power")))
        await http.post(base + "/api/stop", json={})

    await with_server(scenario)


async def main() -> int:
    print("=" * 70)
    print("自定义课程测试")
    print("=" * 70)
    test_store_roundtrip()
    test_power_modes()
    test_validation()
    test_default_course()
    await test_http_crud()
    await test_run_custom_course()
    await test_single_step_course()

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
