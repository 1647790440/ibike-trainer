#!/usr/bin/env python3
"""服务端加固回归测试。

这一套盯的是"只有真实启动路径才会暴露"的那类缺陷——尤其是
`Server()` 在事件循环之外被构造出来的时候会发生什么。main.py 就是这么干的
（`Server()` 建好之后才 `web.run_app()`，而 run_app 会另起一个事件循环），
而之前所有测试都是在协程里构造 Server 的，于是这个坑一直没被测到。

    python3 tests/test_server_hardening.py
"""

from __future__ import annotations

import asyncio
import json
import os as _os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# 测试隔离：绝不能碰用户的真实数据目录
# ---------------------------------------------------------------------------
_os.environ.setdefault("IBIKE_DATA_DIR",
                       tempfile.mkdtemp(prefix="ibike-test-data-"))

import ibike.server as server_mod  # noqa: E402
from aiohttp import ClientSession, WSMsgType, web  # noqa: E402
from ibike.server import Server  # noqa: E402

_failures: List[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print("  {} {}{}".format("\033[32m✔\033[0m" if cond else "\033[31m✘\033[0m",
                             label, "  → " + detail if detail else ""))
    if not cond:
        _failures.append(label)
    return cond


# ---------------------------------------------------------------------------
# 用真实启动方式跑：Server 在事件循环之外构造，再由 asyncio.run 起循环
# ---------------------------------------------------------------------------


def outside_loop(use_simulator: bool = True, **kw: Any) -> Server:
    """在**没有事件循环**的同步上下文里构造 Server（复刻 main.py 的路径）。"""
    return Server(use_simulator=use_simulator, **kw)


async def serve(server: Server, fn) -> None:
    app = server.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        async with ClientSession() as http:
            await fn(http, "http://127.0.0.1:{}".format(port), server)
    finally:
        await runner.cleanup()


def run_case(server: Server, fn) -> None:
    """关键是 asyncio.run：它会新建一个事件循环，和 web.run_app 的行为一致。"""
    asyncio.run(serve(server, fn))


# ======================================================================
# 1. 实时推送（最关键的一条）
# ======================================================================


def test_ws_broadcast_really_pushes() -> None:
    print("\n[1] 状态广播：真实启动路径下，状态变化必须被主动推过来")
    server = outside_loop(use_simulator=False)     # ← 必须在循环外构造

    async def scenario(http, base, srv):
        # 先静一会儿，让广播循环进入"等状态变化"的正常状态。
        # 旧实现把 Event 建在另一个事件循环上了，循环第一次真正等待就抛
        # "got Future attached to a different loop"，当场死掉。
        # 注意：训练进行中 _dirty 每秒被 set 十几次，wait() 会立刻返回、
        # 根本不创建 future，所以那个 bug 在"骑行中"反而是被掩盖的——
        # 必须从空闲态触发才测得出来。
        await asyncio.sleep(0.8)
        async with http.ws_connect(base + "/ws") as ws:
            first = json.loads((await asyncio.wait_for(ws.receive(), timeout=2.0)).data)
            check(first.get("type") == "state", "连上时先补发一帧状态", first.get("type"))

            # 触发一次状态变化：连接模拟骑行台
            await http.post(base + "/api/connect", json={"simulator": True})
            # 连接过程中会推好几帧（先 idle、再带上 trainer），要等到"连接成功"
            # 那一帧为止——只要它来了，就说明广播循环是活的
            pushed = None
            frames = 0
            loop = asyncio.get_event_loop()
            end = loop.time() + 2.0
            while loop.time() < end:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=0.4)
                except asyncio.TimeoutError:
                    continue
                if msg.type != WSMsgType.TEXT:
                    continue
                payload = json.loads(msg.data)
                if payload.get("type") != "state":
                    continue
                frames += 1
                if (payload["data"].get("trainer") or {}).get("connected") is True:
                    pushed = payload["data"]
                    break
            if check(pushed is not None,
                     "连接成功后的新状态在 2 秒内被主动推到 WebSocket"
                     "（修复前广播循环已死，只能等前端 3 秒轮询）",
                     "收到 {} 帧 state".format(frames)):
                check((pushed.get("trainer") or {}).get("connected") is True,
                      "推过来的确实是新状态", str(pushed.get("trainer")))

    run_case(server, scenario)


# ======================================================================
# 2. 断开连接不能把这一场训练丢掉
# ======================================================================


def test_disconnect_saves_the_ride() -> None:
    print("\n[2] 训练中断开连接：这一场必须留下报告")
    server = outside_loop()

    async def scenario(http, base, srv):
        await http.post(base + "/api/connect", json={"simulator": True})
        await http.post(base + "/api/start",
                        json={"mode": "constant", "target_power": 120, "duration_min": 30})
        await asyncio.sleep(6.0)
        async with http.get(base + "/api/state") as r:
            before = await r.json()
        check(before["state"] == "running" and (before["active_s"] or 0) > 3,
              "训练确实在进行中", "{} / {}s".format(before["state"], before.get("active_s")))

        # 用"多了一份"来判断，不要假设列表里只有这一份——上一个用例结束时
        # 服务清理也会保存一次，用例之间必须互不影响
        async with http.get(base + "/api/reports") as r:
            before_count = len((await r.json())["reports"])
        await http.post(base + "/api/disconnect", json={})
        async with http.get(base + "/api/reports") as r:
            reports = (await r.json())["reports"]
        if check(len(reports) == before_count + 1,
                 "断开之后报告多了一份（以前这一场会直接消失）",
                 "{} → {} 份".format(before_count, len(reports))):
            report = reports[0]
            check(report.get("reason") == "断开连接", "报告里写明是断开结束的",
                  str(report.get("reason")))
            check((report.get("actual_s") or 0) > 3, "记录了实际骑行时间",
                  "{}s".format(report.get("actual_s")))
        async with http.get(base + "/api/state") as r:
            after = await r.json()
        check(after["state"] == "idle", "断开后回到 idle", after["state"])

    run_case(server, scenario)


# ======================================================================
# 3. 跨站请求
# ======================================================================


def test_cross_origin_is_rejected() -> None:
    print("\n[3] 跨站请求：别的网页不能偷偷调用本程序接口")
    server = outside_loop()

    async def scenario(http, base, srv):
        # 先清干净，别受上一个用例留下的报告影响
        srv.reports.clear()
        srv.reports.save({"finished_at": 1.0, "plan_name": "x", "trace": []})

        async with http.post(base + "/api/reports/clear",
                             headers={"Origin": "http://evil.example"},
                             data="x") as r:
            check(r.status == 403, "带外站 Origin 的 POST 被拒绝", "HTTP {}".format(r.status))
        async with http.get(base + "/api/reports") as r:
            left = (await r.json())["reports"]
        check(len(left) == 1, "报告没被清掉", "还剩 {} 份".format(len(left)))

        host = base.replace("http://", "")
        async with http.post(base + "/api/reports/clear",
                             headers={"Origin": "http://" + host}) as r:
            check(r.status == 200, "同源 Origin 正常放行", "HTTP {}".format(r.status))
        # 不带 Origin 的（curl / 脚本 / 本机工具）必须照常可用
        async with http.post(base + "/api/reports/clear") as r:
            check(r.status == 200, "不带 Origin 的正常放行", "HTTP {}".format(r.status))

    run_case(server, scenario)


# ======================================================================
# 4. 畸形输入不再 500
# ======================================================================


def test_malformed_input_never_500s() -> None:
    print("\n[4] 畸形 JSON 不能让接口 500")
    server = outside_loop()

    async def scenario(http, base, srv):
        await http.post(base + "/api/connect", json={"simulator": True})

        # 以前 params 传数组/字符串会在 (params or {}).get(...) 上抛
        # AttributeError，直接 500
        for payload in ({"test_id": "ramp", "params": [1, 2]},
                        {"test_id": "ramp", "params": "abc"},
                        {"template_id": "hiit_30_15", "params": [1, 2]}):
            async with http.post(base + "/api/plan", json=payload) as r:
                check(r.status < 500, "/api/plan params={} 不报 500".format(
                    json.dumps(payload["params"])), "HTTP {}".format(r.status))

        # 1e400 在 JSON 里合法，会解析成 inf；以前 int(inf) 抛 OverflowError → 500
        for path in ("/api/settings", "/api/free-resistance",
                     "/api/interval/skip", "/api/resistance"):
            async with http.post(base + path, data='{"raw":1e400,"ftp":1e400,"delta":1e400}',
                                 headers={"Content-Type": "application/json"}) as r:
                check(r.status < 500, "{} 收到 1e400 不报 500".format(path),
                      "HTTP {}".format(r.status))

        # 响应体必须是合法 JSON（以前会输出裸 Infinity，前端 JSON.parse 直接失败）
        async with http.post(base + "/api/plan", json={"test_id": "ramp", "ftp": 1e400}) as r:
            text = await r.text()
        check("Infinity" not in text and "NaN" not in text,
              "课表预览的响应体是合法 JSON", text[-40:])

        # 未知的训练类型：以前会悄悄开一场 100W/60min 的默认训练
        async with http.post(base + "/api/start", json={"mode": "weird"}) as r:
            check(r.status == 400, "未知 mode 返回 400", "HTTP {}".format(r.status))
        async with http.get(base + "/api/state") as r:
            state = (await r.json())["state"]
        check(state != "running", "没有偷偷把训练开起来", state)

    run_case(server, scenario)


def test_start_race_is_a_clean_400() -> None:
    print("\n[5] 开始训练 / 断开 的竞态：必须是干净的 400，不是 500")
    server = outside_loop()

    async def scenario(http, base, srv):
        await http.post(base + "/api/connect", json={"simulator": True})
        # 两个请求并发：一个 start，一个 disconnect
        start = asyncio.ensure_future(
            http.post(base + "/api/start",
                      json={"mode": "constant", "target_power": 120, "duration_min": 5}))
        await asyncio.sleep(0.02)
        disc = asyncio.ensure_future(http.post(base + "/api/disconnect", json={}))
        results = await asyncio.gather(start, disc, return_exceptions=True)
        codes = []
        for r in results:
            if isinstance(r, Exception):
                codes.append("EXC")
            else:
                codes.append(r.status)
                r.close()
        check(all(c != 500 for c in codes), "没有出现 500", str(codes))

    run_case(server, scenario)


# ======================================================================
# 6. 损坏的数据文件不能让程序起不来 / 悄悄丢数据
# ======================================================================


def test_broken_custom_file_does_not_block_startup() -> None:
    print("\n[6] custom_workouts.json 被人改坏：程序要能起来，且不能丢数据")
    import shutil
    from ibike.custom import CustomStore
    from ibike.paths import data_dir

    good = {"id": "c1", "name": "好课", "power_mode": "pct",
            "steps": [{"name": "热身", "kind": "warmup", "duration_s": 300,
                       "pct": 55, "watts": 110}]}

    # (a) courses 里混进一个字符串：以前会在 Server() 构造时 AttributeError，
    #     整个程序起不来
    path = data_dir() / "custom_workouts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    for junk in (["oops"], [42], [good, "oops"], []):
        path.write_text(json.dumps({"version": 1, "courses": junk}), encoding="utf-8")
        try:
            store = CustomStore()
            ok = True
            n = len(store.list())
        except Exception as exc:                    # noqa: BLE001
            ok, n = False, str(exc)
        check(ok, "courses={} 时加载不崩".format(json.dumps(junk)[:24]), str(n))

    # (b) 一条好的一条坏的：坏的那条不能在下次保存时被永久删掉
    path.write_text(json.dumps({"version": 1, "courses": [good, {"id": "c2", "steps": []}]}),
                    encoding="utf-8")
    store = CustomStore()
    store.upsert(dict(good, name="改名"), 200.0)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    ids = [c.get("id") for c in on_disk["courses"] if isinstance(c, dict)]
    check("c2" in ids, "读不出来的那条被原样保留在磁盘上（以前会被抹掉）", str(ids))

    # (c) 结构整个不对：要留档，不能静默清空之后再被覆盖
    for f in path.parent.glob("custom_workouts.json*"):
        f.unlink()
    path.write_text(json.dumps({"version": 1, "sessions": [good]}), encoding="utf-8")
    store = CustomStore()
    backups = list(path.parent.glob("custom_workouts.json.corrupt*"))
    check(len(backups) == 1, "结构不对时留下了备份", str([b.name for b in backups]))
    check(store.list() == [], "内存里从空列表开始")
    for b in backups:
        b.unlink()
    shutil.rmtree(path.parent, ignore_errors=True)


def test_broken_settings_does_not_block_startup() -> None:
    print("\n[7] settings.json 里写着 1e400：不能因此起不来")
    import shutil
    from ibike.settings import Settings
    from ibike.paths import data_dir

    path = data_dir() / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"ftp": 1e400, "free_resistance": 1e400}', encoding="utf-8")
    try:
        values = Settings().values
        ok = True
    except Exception as exc:                        # noqa: BLE001
        ok, values = False, str(exc)
    check(ok, "能正常构造 Settings", str(values))
    if ok:
        check(values["ftp"] == 200 and values["free_resistance"] == 90,
              "非法值退回默认", str(values))
    shutil.rmtree(path.parent, ignore_errors=True)


# ======================================================================


def main() -> int:
    print("=" * 70)
    print("服务端加固回归测试")
    print("=" * 70)
    test_ws_broadcast_really_pushes()
    test_disconnect_saves_the_ride()
    test_cross_origin_is_rejected()
    test_malformed_input_never_500s()
    test_start_race_is_a_clean_400()
    test_broken_custom_file_does_not_block_startup()
    test_broken_settings_does_not_block_startup()

    print("\n" + "=" * 70)
    if _failures:
        print("失败 {} 项：".format(len(_failures)))
        for f in _failures:
            print("   - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
