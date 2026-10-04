"""本地 Web 服务：把骑行台控制和训练会话包装成 HTTP + WebSocket 接口。

设计上刻意保持"单机单人"：同一时刻只维护一个训练会话，前端刷新页面不影响
正在进行的训练。
"""

from __future__ import annotations

import asyncio
import logging
import math
import socket
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from aiohttp import WSMsgType, web

from .custom import (CustomStore, build_custom_steps, course_stats, default_course,
                     normalize_course)
from .ble import BleScanError, scan_raw
from .heartrate import (HR_MODE_MAX, HR_MODE_RESERVE, HeartRateClient, HeartRateError,
                        SimulatedHeartRate, hr_zone_of, hr_zones)
from .ftptest import build_test_plan, get_test as get_ftptest, plan_stats as test_stats
from .ftptest import clamp_params as clamp_test_params, public_tests
from .reports import ReportStore
from .settings import Settings
from .session import (ERG_AUTO, STATE_IDLE, STATE_PAUSED, STATE_RUNNING,
                      WorkoutSession)
from .simulator import SimulatedTrainer
from .trainer import TrainerClient, TrainerError
from .workouts import (build_plan, clamp_params, get_template, plan_stats,
                       public_templates)

log = logging.getLogger("ibike.server")

WEB_DIR = Path(__file__).parent / "web"

# 状态推送节奏：最高 4Hz，空闲时 2 秒一次心跳
MIN_BROADCAST_INTERVAL_S = 0.25
IDLE_HEARTBEAT_S = 2.0

# ---- 骑行台自动重连 ----
# 真机实测：骑手停下来不踩之后，台子约 47 秒报"Stopped"、约 109 秒后自动关机
# （蓝牙断链）。以前断链之后必须"回设备页 → 扫描 → 点设备 → 回训练页 → 点继续"，
# 一次长休息就要走一遍。现在训练没结束时后台自己把它接回来。
#
# 为什么要重试这么久：台子是被踩醒的，骑手回来之前它根本不在广播——按心率带那种
# "重试几次就放弃"的做法会在骑手回来之前就放弃。
AUTO_RECONNECT_DELAYS_S = (4.0, 6.0, 10.0, 15.0, 20.0)
AUTO_RECONNECT_MAX_S = 20.0
AUTO_RECONNECT_CONNECT_TIMEOUT_S = 6.0   # 单次连接尝试的超时，别让尝试叠起来
AUTO_RECONNECT_SCAN_S = 4.0              # 每轮扫描的窗口（见 _reconnect_candidates）

# ---- 悬浮窗 ----
# 设备页那个「悬浮显示」按钮打的就是它：服务拉起/收掉一个独立的 overlay.py 进程。
# 独立进程的好处是它崩了也碰不到训练循环；窗口本身是 macOS 原生透明置顶层，
# 用 bleak 顺带装好的 pyobjc，零新增依赖。
OVERLAY_SCRIPT = Path(__file__).resolve().parent.parent / "overlay.py"
OVERLAY_LOG = Path(tempfile.gettempdir()) / "ibike-overlay.log"
OVERLAY_START_GRACE_S = 0.8              # 起完等这么久，看它有没有立刻挂掉


@web.middleware
async def same_origin_only(request: web.Request, handler: Any) -> web.StreamResponse:
    """挡掉"用户访问了别的网页、那个网页偷偷调用本程序接口"这类跨站请求。

    本程序是个没有登录态的本地服务，所有 POST 都能直接执行（清空报告、断开连接、
    开训练……）。浏览器对表单/`no-cors` 的跨站 POST 会带上 `Origin`，所以只要
    看到 `Origin` 且和自己的 Host 不一致就拒绝；不带 `Origin` 的（curl、脚本、
    同源请求的旧浏览器）一律放行，不影响正常使用。
    """
    if request.headers.get("Origin"):
        host = request.host
        allowed = {"http://" + host, "https://" + host}
        if request.headers["Origin"].rstrip("/") not in allowed:
            log.warning("拒绝了跨站请求：%s %s（Origin: %s）",
                        request.method, request.path, request.headers.get("Origin"))
            return web.json_response({"ok": False, "error": "跨站请求已被拒绝"},
                                     status=403)
    return await handler(request)


def lan_ip() -> str:
    """猜测本机在局域网里的地址，用于给出手机可访问的 URL。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


class Server:
    def __init__(self, use_simulator: bool = False,
                 simulator_responds: bool = True,
                 simulator_advertise: Optional[bool] = None) -> None:
        self.use_simulator = use_simulator
        self.simulator_responds = simulator_responds
        self.simulator_advertise = simulator_advertise
        self.trainer: Optional[Any] = None
        self.session: Optional[WorkoutSession] = None
        # 独立心率带：和骑行台是两个互不相干的连接，可以只连一个、也可以先后连
        self.hr: Optional[Any] = None
        self.hr_devices: list = []
        self.scan_results: list = []
        self.scanning = False
        self.last_start_body: Dict[str, Any] = {}
        self.custom = CustomStore()
        self.reports = ReportStore()
        self.settings = Settings()
        self._last_saved_finish: Optional[float] = None

        self.sockets: Set[web.WebSocketResponse] = set()
        self.events: Deque[Dict[str, Any]] = deque(maxlen=60)
        self._snapshot: Dict[str, Any] = {"state": STATE_IDLE}
        # 这两个**必须**在运行中的事件循环里创建，所以先在 __init__ 里留空、
        # 到 start_background() 才真正建出来。
        #
        # 原因是 Server 在 web.run_app() 之前就构造好了，而 aiohttp 的 run_app
        # 会自己 asyncio.new_event_loop() 另起一个循环。Python 3.9 的
        # asyncio.Event()/Lock() 会把自己绑到"构造时"的那个循环上，于是：
        #   · 广播循环第一次 await self._dirty.wait() 就抛
        #     "got Future attached to a different loop"，实时推送整个失效，
        #     界面只剩 3 秒一次的轮询兜底；
        #   · 一旦有两个请求同时争 _busy（比如连点两次设备），后到的那个 500。
        self._dirty: Optional[asyncio.Event] = None
        self._busy: Optional[asyncio.Lock] = None
        self._broadcast_task: Optional[asyncio.Task] = None
        # 自动重连：任务句柄 + 对外的"正在重连"标志（前端据此写提示）。
        # 重试节奏做成实例属性，测试可以压到毫秒级。
        self._reconnect_task: Optional[asyncio.Task] = None
        self.auto_reconnecting = False
        self.auto_reconnect_delays = AUTO_RECONNECT_DELAYS_S
        # 悬浮窗子进程（测试里可以换掉 overlay_command，免得真开窗口）
        self._overlay_proc: Optional[subprocess.Popen] = None
        self.overlay_command: Optional[List[str]] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start_background(self, app: web.Application) -> None:
        # 见 __init__ 里的说明：这两个原语一定要在这里建，才绑在当前运行的循环上
        self._dirty = asyncio.Event()
        self._busy = asyncio.Lock()
        self._broadcast_task = asyncio.ensure_future(self._broadcast_loop())
        if self.use_simulator:
            await self._connect_trainer(simulator=True)
        else:
            # 上次连过的那根心率带，开机后台试着接回来。不阻塞启动、失败也只是
            # 在日志里留一句——心率带没戴的时候本来就连不上，不该影响程序启动。
            saved = str(self.settings.get("hr_strap_address", "") or "")
            if saved:
                asyncio.ensure_future(self._auto_connect_hr(saved))

    async def cleanup(self, app: web.Application) -> None:
        task = self._broadcast_task
        self._broadcast_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task          # 等它真的停下，避免退出时留一条未完成任务的告警
            except (asyncio.CancelledError, Exception):
                pass
        # _disconnect_trainer 里会先把没结束的训练收尾存成报告，这里不要再
        # 单独 aclose 一次——那会把会话任务提前掐掉。
        await self._stop_overlay()         # 别留一个孤零零的悬浮窗
        await self._disconnect_hr()
        await self._disconnect_trainer()   # 内部会先停掉自动重连

    async def _broadcast_loop(self) -> None:
        """按固定节奏推送状态。

        采样是 10Hz，但没必要原样灌给前端：4Hz 的功率数字已经足够顺滑，
        也能避免手机端被无谓的流量拖慢。空闲时改为低频心跳——训练总结里带着
        整段功率曲线，每 0.5 秒重发一次纯属浪费。
        """
        try:
            while True:
                try:
                    await asyncio.wait_for(self._dirty.wait(), timeout=IDLE_HEARTBEAT_S)
                except asyncio.TimeoutError:
                    pass
                self._dirty.clear()
                # 每一次推送单独兜异常：任何一次出问题都只丢这一拍，
                # 不能让整个广播循环退出——那会让实时界面**永久**退化成 3 秒轮询，
                # 而用户完全看不出发生了什么（实测被一个取数的小 bug 触发过）。
                try:
                    # 空闲时也读一次设备数据：设备页要在开始训练**之前**就能看到
                    # 功率/踏频/心率在动，否则没法确认到底连上没有（见 monitor_tick）
                    if self.session is not None:
                        self.session.monitor_tick()
                    await self._broadcast({"type": "state", "data": self._full_snapshot()})
                except Exception:
                    log.exception("推送状态失败（已跳过这一拍，广播循环继续）")
                await asyncio.sleep(MIN_BROADCAST_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("广播循环异常")

    async def _broadcast(self, message: Dict[str, Any]) -> None:
        if not self.sockets:
            return
        dead = []
        for ws in list(self.sockets):
            try:
                # 加超时：一个不读数据的客户端不该把所有人的状态推送都拖住
                await asyncio.wait_for(ws.send_json(message), timeout=2.0)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.sockets.discard(ws)

    def _mark_dirty(self) -> None:
        """有状态变化，让广播循环赶紧推一次。启动前调用直接忽略。"""
        if self._dirty is not None:
            self._dirty.set()

    def _on_session_update(self, snapshot: Dict[str, Any]) -> None:
        self._snapshot = snapshot
        self._mark_dirty()

    def _hr_state(self) -> Optional[Dict[str, Any]]:
        if self.hr is None:
            return None
        state = self.hr.state() if hasattr(self.hr, "state") else {}
        state["saved"] = {
            "address": self.settings.get("hr_strap_address", ""),
            "name": self.settings.get("hr_strap_name", ""),
        }
        return state

    def _full_snapshot(self) -> Dict[str, Any]:
        """给前端的完整状态：会话快照 + 心率带状态。

        心率带是独立外设，所以在**没有训练会话**的时候（设定页）也要能看到它连没连上。
        """
        snap = dict(self._snapshot)
        # 正在自动重连骑行台（前端据此把"去设备页手动连"改成"等着就行"）
        snap["auto_reconnecting"] = bool(self.auto_reconnecting)
        # 悬浮窗开着没有（设备页那个按钮据此显示"开启/关闭"）
        snap["overlay_running"] = self._overlay_running()
        hr_state = self._hr_state()
        snap["hr_client"] = hr_state
        # 没在训练的时候，心率的实时读数直接取**心率带自己**的最新一帧，
        # 而不是等训练会话喂。
        #
        # 心率带是独立外设，骑行台和它可以各自接断。以前心率的取数只发生在训练
        # 会话里，而会话是跟着骑行台一起创建/销毁的——于是"断开骑行台"会把会话
        # 一起清掉，心率带的数字也跟着消失，可是状态还写着"已连接"。
        state = snap.get("state")
        if state not in (STATE_RUNNING, STATE_PAUSED):
            if hr_state and hr_state.get("heart_rate") is not None:
                snap["heart_rate"] = hr_state["heart_rate"]
                snap["hr_source"] = "strap"
                snap["hr_contact"] = hr_state.get("contact")
            elif not (hr_state or {}).get("connected"):
                snap["heart_rate"] = None
                snap["hr_contact"] = None
            snap["hr_stale"] = bool((hr_state or {}).get("stale"))
            # 心率区间用**当前设置**算：设备页要在开始训练之前就能看到
            # "现在这个心率属于哪个区"。训练中则用开训时冻结的那一套
            # （改设置不该影响正在进行的这一场），所以这里只在空闲时覆盖。
            cfg = self._hr_config()
            snap["hr_zones"] = hr_zones(cfg.get("max_hr"), cfg.get("rest_hr"),
                                        cfg.get("zone_mode") or HR_MODE_MAX)
            snap["hr_zone"] = hr_zone_of(snap.get("heart_rate"), cfg.get("max_hr"),
                                        cfg.get("rest_hr"),
                                        cfg.get("zone_mode") or HR_MODE_MAX)
            snap["hr_limit"] = {"enabled": bool(cfg.get("hr_limit_enabled")),
                                "bpm": cfg.get("hr_limit_bpm"), "note": ""}
        return snap

    def _on_event(self, kind: str, message: str) -> None:
        entry = {"kind": kind, "message": message, "t": time.time()}
        self.events.append(entry)
        # 训练一结束就自动存一份报告。走事件而不是 /api/stop 处理器，是因为
        # 跑满时长自然结束不会经过接口。
        if kind == "stopped":
            self._save_report()
        elif kind == "disconnected":
            # 骑行台掉线 **不等于** 训练结束。休息的时候台子会自动关机，用户接着
            # 做的是"回设备页把它重新连上"——那是继续的意思。所以这里只把训练
            # 挂起（计时冻结、数据留着），等重连时接回去。
            # 以前这里什么都不做，而重连走的是 _connect_trainer →
            # _disconnect_trainer，会把还没骑完的训练收尾存成报告：60 分钟的计划
            # 在 21 分钟处变成一份"提前结束"的报告，而且再也接不回去。
            session = self.session
            if session is not None and session.handle_trainer_lost(message):
                log.warning("骑行台掉线，训练已挂起（未结束）：%s", message)
                self._snapshot = session.snapshot()
                self._mark_dirty()
        elif kind == "trainer-lost":
            # 训练还挂在那儿等骑行台回来：起一个后台任务自己把它接回去，
            # 不用用户跑到设备页手动连（台子是被踩醒的，所以会一直重试）
            self._start_auto_reconnect()
        asyncio.ensure_future(self._broadcast({"type": "event", "data": entry}))

    def _save_report(self) -> None:
        session = self.session
        summary = getattr(session, "summary", None) if session is not None else None
        if not summary:
            return
        # 同一次训练只存一份（stop() 与收尾逻辑挨得很近，事件可能不止一次）
        key = summary.get("finished_at")
        if key is not None and key == self._last_saved_finish:
            return
        # 把这一场训练期间的事件一起写进报告。事后复盘最缺的恰恰不是数字，而是
        # "报告里那两段空白，到底是用户按了暂停，还是骑行台掉线挂起了"——只看
        # 曲线和统计答不出来，只能去问用户。事件里写得清清楚楚。
        started = float(summary.get("started_at") or 0.0)
        events = [e for e in self.events if float(e.get("t") or 0.0) >= started]
        payload = dict(summary)
        payload["events"] = [
            # 相对时刻（秒）比绝对时间戳好读，也和 trace 的 t 同一把尺子
            {"at": round(float(e.get("t") or 0.0) - started, 1),
             "kind": e.get("kind"), "message": e.get("message")}
            for e in events[-80:]
        ]
        report = self.reports.save(payload)
        if report is None:
            return
        self._last_saved_finish = key
        log.info("训练报告已保存：%s（%s）", report["id"], report.get("plan_name"))

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 悬浮窗：拉起/关掉那个独立的 overlay.py 进程
    # ------------------------------------------------------------------

    def _overlay_running(self) -> bool:
        """它还活着吗？自己退出了（或崩了）就当没开。"""
        proc = self._overlay_proc
        if proc is None:
            return False
        if proc.poll() is None:
            return True
        log.info("悬浮窗进程已退出（退出码 %s）", proc.returncode)
        self._overlay_proc = None
        return False

    def _overlay_log_tail(self, lines: int = 3) -> str:
        try:
            text = OVERLAY_LOG.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return " / ".join(t for t in text.strip().splitlines()[-lines:] if t)

    async def _start_overlay(self, port: int) -> None:
        if self._overlay_running():
            return
        if self.overlay_command:
            cmd = list(self.overlay_command)
        else:
            if not OVERLAY_SCRIPT.exists():
                raise RuntimeError(
                    "找不到 {} —— overlay.py 应该和 ibike/ 放在同一个目录"
                    "（从项目根目录启动服务即可）".format(OVERLAY_SCRIPT))
            cmd = [sys.executable, str(OVERLAY_SCRIPT), "--port", str(port)]
        try:
            log_file = open(OVERLAY_LOG, "wb")   # 每次启动重写，tail 反映这一次
        except OSError:
            log_file = subprocess.DEVNULL
        log.info("启动悬浮窗：%s", " ".join(cmd))
        # start_new_session：给它单独一个进程组，终端里的 Ctrl+C 不会在悬浮窗那边
        # 打出一堆 KeyboardInterrupt；服务退出时我们自己把它收掉（见 cleanup）
        self._overlay_proc = subprocess.Popen(
            cmd, stdout=log_file, stderr=subprocess.STDOUT,
            cwd=str(OVERLAY_SCRIPT.parent), start_new_session=True)
        if log_file is not subprocess.DEVNULL:
            log_file.close()          # 子进程已经继承了 fd，父进程这边可以关
        await asyncio.sleep(OVERLAY_START_GRACE_S)
        if self._overlay_proc.poll() is not None:
            code = self._overlay_proc.returncode
            self._overlay_proc = None
            raise RuntimeError("悬浮窗没能启动（退出码 {}）：{}{}".format(
                code, self._overlay_log_tail(),
                "，详细日志见 {}".format(OVERLAY_LOG)))

    async def _stop_overlay(self) -> None:
        proc, self._overlay_proc = self._overlay_proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        for _ in range(30):              # 最多等 3 秒，别卡住事件循环
            if proc.poll() is not None:
                return
            await asyncio.sleep(0.1)
        log.warning("悬浮窗没有响应 SIGTERM，直接杀掉")
        proc.kill()
        await asyncio.sleep(0.1)

    async def handle_overlay(self, request: web.Request) -> web.Response:
        """设备页那个「悬浮显示」按钮打这里：开启/关闭悬浮窗。"""
        body = await self._body(request)
        action = str(body.get("action") or "toggle").lower()
        running = self._overlay_running()
        want = (not running) if action == "toggle" else (action == "start")
        try:
            if want:
                # 悬浮窗要连着同一个服务的同一端口，端口从这次请求上取最省事
                await self._start_overlay(request.url.port or 8765)
            else:
                await self._stop_overlay()
        except Exception as exc:          # noqa: BLE001 - 起不来要说清原因
            log.warning("悬浮窗操作失败：%s", exc)
            return web.json_response({"ok": False, "error": str(exc),
                                      "overlay_running": self._overlay_running()},
                                     status=400)
        state = self._overlay_running()
        self._mark_dirty()
        return web.json_response({"ok": True, "overlay_running": state})

    # ------------------------------------------------------------------
    # 骑行台自动重连（只在"训练还没结束"时工作）
    # ------------------------------------------------------------------

    def _start_auto_reconnect(self) -> None:
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self.auto_reconnecting = True
        self._on_event("trainer-reconnecting",
                       "骑行台掉线了，正在后台自动重连（接上就会自动继续，不用手动去连）")
        self._reconnect_task = asyncio.ensure_future(self._auto_reconnect_loop())

    def _stop_auto_reconnect(self) -> None:
        task = self._reconnect_task
        # 自动重连是**这个任务自己**在跑 _connect_trainer，而它开头会调
        # _disconnect_trainer（里面就调到这里）。这时候什么都不能动：
        #   · cancel() 会在下一个 await 点抛 CancelledError，把刚建好的连接毁掉；
        #   · 把 auto_reconnecting 抹成 False，循环下一轮就会以为"被取消了"直接退出，
        #     一次失败之后就再也不重试了（台子还没被踩醒的时候必然发生）。
        if task is not None and task is asyncio.current_task():
            return
        self.auto_reconnecting = False
        self._reconnect_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _auto_reconnect_loop(self) -> None:
        """断链之后自己把骑行台接回来，接上之后由会话那边"骑起来就自动继续"。

        退出条件：连上了 / 训练结束了 / 用户主动断开了。台子断电之后要等骑手
        踩一下才会重新广播，所以这里不设"重试几次就放弃"，只把间隔退避到 30 秒。
        """
        delays = list(self.auto_reconnect_delays) or [AUTO_RECONNECT_MAX_S]
        attempt = 0
        last_error = ""
        try:
            while True:
                await asyncio.sleep(delays[min(attempt, len(delays) - 1)])
                attempt += 1
                if not self.auto_reconnecting:
                    return
                session = self.session
                if session is None or not session.is_unfinished():
                    return
                if self.trainer is not None and getattr(self.trainer, "connected", False):
                    return
                candidates, note = await self._reconnect_candidates(use_scan=attempt > 1)
                connected = False
                error = ""
                if candidates:
                    async with self._busy:
                        if not self.auto_reconnecting:
                            return
                        session = self.session
                        if session is None or not session.is_unfinished():
                            return
                        if (self.trainer is not None
                                and getattr(self.trainer, "connected", False)):
                            return
                        for address in candidates:
                            try:
                                await self._connect_trainer(address=address)
                                connected = True
                                break
                            except Exception as exc:    # noqa: BLE001 - 换下一个候选
                                error = str(exc)
                                log.info("自动重连第 %d 次尝试失败（%s）：%s",
                                         attempt, address, exc)
                if connected:
                    log.warning("骑行台已自动重连（第 %d 次尝试）", attempt)
                    self._on_event("trainer-reconnected", "骑行台已自动重连"
                                   "（第 {} 次尝试）{}".format(attempt, note))
                    return
                last_error = error or last_error or "还没找到骑行台"
                # 前两次各留一条，之后每 5 次留一条：既能看懂它一直在试，
                # 又不会把事件列表刷满
                if attempt <= 2 or attempt % 5 == 0:
                    self._on_event("trainer-reconnecting",
                                   "自动重连还在试（第 {} 次）：{}{}".format(
                                       attempt, last_error, note))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("自动重连循环异常")
        finally:
            self.auto_reconnecting = False
            self._mark_dirty()

    async def _reconnect_candidates(self, use_scan: bool) -> Tuple[List[str], str]:
        """这一轮要试哪些地址：记住的那个 + （需要时）扫描找到的。

        为什么要扫描、而且不能只在"没存过地址"时才扫：**骑行台断电重连之后，
        macOS 眼里的 UUID 会变**（BLE 随机地址），存下来的那个从此连不上。只试它
        就等于永远连不上——真机实测就是这么翻车的：程序一直在重试旧地址，
        从头到尾没扫描过一次，用户只能手动去设备页扫描连接。

        代价是扫描比较占蓝牙（4 秒窗口），所以第一轮先只试记住的地址（快、且
        短暂断链时地址还是有效的），之后每轮都先扫一遍、把**刚扫到的**地址排在
        前面（它才是当前有效的那个），记住的地址放在最后兜底。
        """
        saved = str(self.settings.get("trainer_address") or "")
        scanned: List[str] = []
        note = ""
        if use_scan and not self.scanning:
            found: List[Dict[str, Any]] = []
            try:
                raw = await scan_raw(timeout=AUTO_RECONNECT_SCAN_S)
                found = [d.to_dict() for d in TrainerClient.classify_trainers(raw)]
            except BleScanError as exc:
                log.info("自动重连扫描失败：%s", exc)
                found, note = [], "（扫描失败：{}）".format(exc)
            if not note:
                wanted = str(self.settings.get("trainer_name") or "")
                by_name = [d for d in found
                           if wanted and str(d.get("name") or "") == wanted]
                # 名字对不上（固件改名/没存过名字）时，只有一台候选才敢用
                pool = by_name or (found if len(found) == 1 else [])
                scanned = [str(d.get("address") or "") for d in pool
                           if d.get("address")]
                note = "（扫描到 {} 台骑行台候选）".format(len(scanned))
        out = [a for a in scanned if a]
        if saved and saved not in out:
            out.append(saved)
        return out, note

    async def _disconnect_trainer(self, keep_session: bool = False) -> None:
        """断开骑行台。

        ``keep_session=True`` 用于"重连之前先把旧的设备对象摘掉"：这时训练只是
        **挂起**，不 stop（stop 会收尾并写报告），数据留在会话里等新设备接回来。
        默认的 False 是用户的明确意图（点「断开」、关服务），那就照旧把没骑完的
        训练收尾存成报告。
        """
        # 用户主动断开（或关服务）时别再自动连回来
        self._stop_auto_reconnect()
        session = self.session
        kept = False
        if session is not None:
            if keep_session and session.is_unfinished():
                kept = True
                session.handle_trainer_lost("骑行台已断开")
            else:
                # 训练还没结束就断开（点「断开」、Ctrl+C 关服务、或者改连另一台设备），
                # 不能把这次训练直接扔掉：aclose() 只取消主循环，不生成总结，
                # 于是报告不会落盘，骑了半小时的记录就这么没了。
                # stop() 对 idle/finished 是空操作，重复调用也安全。
                if session.state in (STATE_RUNNING, STATE_PAUSED) or session.elapsed_s > 1.0:
                    try:
                        await session.stop(reason="断开连接")
                    except Exception:
                        log.exception("断开前保存训练失败")
                await session.aclose()
                self.session = None
        trainer = self.trainer
        self.trainer = None
        if trainer is not None:
            try:
                await trainer.disconnect()
            except Exception:
                log.exception("断开骑行台时出错")
        # 模拟心率带的心率是从骑行台功率推出来的。骑行台断开之后功率就没人喂了，
        # 归零让它自己慢慢回落到静息心率，否则会停在一个"刚才那个功率"上不动。
        if isinstance(self.hr, SimulatedHeartRate):
            self.hr.set_power(0.0)
        # 保住训练的时候快照要反映"挂起"，不能顺手清成 idle
        self._snapshot = session.snapshot() if kept else {"state": STATE_IDLE}
        self._mark_dirty()

    async def _connect_trainer(self, address: Optional[str] = None,
                               simulator: bool = False,
                               simulator_responds: Optional[bool] = None
                               ) -> Dict[str, Any]:
        # 还没骑完的训练必须保住：骑行台断电/断链之后重新连接走的正是这条路，
        # 用户要的是"接着骑"，不是把这场比赛收尾掉。
        keep = self.session if (self.session is not None
                                and self.session.is_unfinished()) else None
        await self._disconnect_trainer(keep_session=keep is not None)

        if simulator:
            responds = (self.simulator_responds if simulator_responds is None
                        else bool(simulator_responds))
            advertise = self.simulator_advertise
            if advertise is None:
                # 想演示"固件收下目标功率却不执行"时，得让它照常声明支持，
                # 否则程序一眼就看出它不支持，压根不会走原生 ERG 那条路
                advertise = True if not responds else None
            trainer: Any = SimulatedTrainer(
                on_event=self._on_event,
                responds_to_target_power=responds,
                advertise_power_target=advertise,
            )
            # 模拟模式下顺带接一根模拟心率带：这样界面上心率格子、心率曲线、
            # 心率区间都能在没有硬件时先看一遍。模拟台的功率喂给它是为了让
            # 心率跟着功率上下——真实心率就是这个脾气（有滞后）。
            sim_hr = SimulatedHeartRate(
                rest_hr=float(self.settings.get("hr_rest", 60) or 60),
                max_hr=float(self.settings.get("hr_max", 180) or 180),
            )
            trainer.on_data = lambda d: sim_hr.set_power(d.get("power_w") or 0)
            await sim_hr.connect()
            self.hr = sim_hr
            self.hr_devices = [{"address": sim_hr.address, "name": sim_hr.name,
                                "rssi": -40}]
        else:
            if not address:
                raise TrainerError("缺少设备地址")
            trainer = TrainerClient(on_event=self._on_event)

        self.trainer = trainer
        if keep is None:
            self.session = WorkoutSession(trainer, on_update=self._on_session_update,
                                          on_event=self._on_event,
                                          heart_rate=self.hr)
        try:
            await trainer.connect(address or "simulator")
        except Exception:
            # 连接失败必须把半成品清干净。否则会留下一个"看起来连上了、其实不能用"
            # 的状态：trainer/session 都还在，但快照里没有 trainer 字段，
            # 前端因此认为未连接，开始按钮永远是灰的。
            self.trainer = None
            if keep is None:
                self.session = None
                self._snapshot = {"state": STATE_IDLE}
            else:
                # 重连失败不能把挂起的训练弄丢：只是没接上新台子，训练本体的
                # 数据还在 session 里，用户可以再试一次。
                self._snapshot = keep.snapshot()
            self._mark_dirty()
            try:
                await trainer.disconnect()
            except Exception:
                pass
            raise
        if keep is not None:
            await keep.rebind_trainer(trainer)
            # 心率带是独立外设，但"当前该用哪一根"由服务端持有（模拟模式会换成
            # 模拟带子）。接回来的会话要用当前这根，别继续用旧的。
            keep.heart_rate = self.hr
        if not getattr(trainer, "is_simulator", False):
            # 记住这台骑行台：掉线之后要按地址直接连回去（模拟设备没有地址）
            try:
                self.settings.update({"trainer_address": getattr(trainer, "address", "") or "",
                                      "trainer_name": trainer.name or ""})
            except OSError:
                log.exception("保存骑行台地址失败")
        self._snapshot = self.session.snapshot()
        self._mark_dirty()
        return trainer.state()

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[same_origin_only])
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/api/state", self.handle_state)
        app.router.add_post("/api/scan", self.handle_scan)
        app.router.add_post("/api/connect", self.handle_connect)
        app.router.add_post("/api/disconnect", self.handle_disconnect)
        app.router.add_post("/api/overlay", self.handle_overlay)
        app.router.add_post("/api/hr/connect", self.handle_hr_connect)
        app.router.add_post("/api/hr/disconnect", self.handle_hr_disconnect)
        app.router.add_post("/api/start", self.handle_start)
        app.router.add_post("/api/repeat", self.handle_repeat)
        app.router.add_get("/api/templates", self.handle_templates)
        app.router.add_post("/api/plan", self.handle_plan)
        app.router.add_get("/api/custom", self.handle_custom_list)
        app.router.add_post("/api/custom", self.handle_custom_save)
        app.router.add_post("/api/custom/delete", self.handle_custom_delete)
        app.router.add_get("/api/custom/new", self.handle_custom_new)
        app.router.add_get("/api/tests", self.handle_test_list)
        app.router.add_get("/api/settings", self.handle_settings_get)
        app.router.add_post("/api/settings", self.handle_settings_save)
        app.router.add_post("/api/free-resistance", self.handle_free_resistance)
        app.router.add_get("/api/reports", self.handle_report_list)
        app.router.add_get("/api/reports/{report_id}", self.handle_report_get)
        app.router.add_post("/api/reports/delete", self.handle_report_delete)
        app.router.add_post("/api/reports/clear", self.handle_report_clear)
        app.router.add_post("/api/interval/skip", self.handle_interval_skip)
        app.router.add_post("/api/pause", self.handle_pause)
        app.router.add_post("/api/resume", self.handle_resume)
        app.router.add_post("/api/stop", self.handle_stop)
        app.router.add_post("/api/dismiss", self.handle_dismiss)
        app.router.add_post("/api/target", self.handle_target)
        app.router.add_post("/api/resistance", self.handle_resistance)
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_static("/static/", str(WEB_DIR), name="static")
        app.on_startup.append(self.start_background)
        app.on_cleanup.append(self.cleanup)
        return app

    async def handle_index(self, request: web.Request) -> web.StreamResponse:
        """发首页时给静态资源带上"版本号"（用文件 mtime）。

        为什么需要：index.html 和 app.js 是**分别**缓存的，浏览器完全可能把新的
        HTML 和旧的 JS 混着用。症状很隐蔽——按钮看得见，但按下去毫无反应
        （旧 app.js 里没有这个按钮的点击处理，实测踩过一次）。带上 ?v=mtime 之后
        前端一改浏览器就必须重新取，这两个文件永远配套。
        """
        try:
            html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            raise web.HTTPNotFound()
        for name in ("app.js", "style.css"):
            try:
                stamp = str(int((WEB_DIR / name).stat().st_mtime))
            except OSError:
                continue
            html = html.replace('/static/{}"'.format(name),
                                '/static/{}?v={}"'.format(name, stamp))
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def handle_state(self, request: web.Request) -> web.Response:
        payload = self._full_snapshot()
        payload["scan_results"] = self.scan_results
        payload["scanning"] = self.scanning
        payload["events"] = list(self.events)
        payload["simulator_available"] = True
        payload["hr_devices"] = self.hr_devices
        return web.json_response(payload)

    async def handle_ws(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)
        self.sockets.add(ws)
        try:
            await ws.send_json({"type": "state", "data": self._full_snapshot()})
            await ws.send_json({"type": "events", "data": list(self.events)})
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.sockets.discard(ws)
        return ws

    # -- 具体动作 ------------------------------------------------------

    async def _body(self, request: web.Request) -> Dict[str, Any]:
        try:
            data = await request.json()
        except web.HTTPException:
            # 请求体超过 aiohttp 的 client_max_size 时会抛 413。以前这里
            # 一并吞掉当成"没有 body"，于是接口照样回 ok:true，用户以为存上了。
            raise
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    async def handle_scan(self, request: web.Request) -> web.Response:
        """扫一次，同时给出骑行台和心率带两份候选。

        骑行台和心率带是同一批广播里的两类设备，扫一次就够——扫两遍既慢一倍，
        界面上还要逼用户点两次按钮。筛选用的是同一份原始结果，见 ibike/ble.py。
        """
        if self.scanning:
            return web.json_response({"ok": False, "error": "正在扫描中，请稍候"}, status=409)
        body = await self._body(request)
        timeout = max(1.0, min(30.0, self._number(body.get("timeout"), 8.0)))
        self.scanning = True
        try:
            raw = await scan_raw(timeout=timeout)
            self.scan_results = [d.to_dict() for d in TrainerClient.classify_trainers(raw)]
            self.hr_devices = [d.to_dict() for d in HeartRateClient.classify_straps(raw)]
            return web.json_response({"ok": True, "devices": self.scan_results,
                                      "hr_devices": self.hr_devices})
        except BleScanError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        finally:
            self.scanning = False

    async def handle_connect(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        async with self._busy:
            try:
                info = await self._connect_trainer(
                    address=body.get("address"),
                    simulator=bool(body.get("simulator")),
                    simulator_responds=body.get("simulator_responds"),
                )
            except (TrainerError, Exception) as exc:
                # 连接失败是用户可预期的结果（设备没唤醒、被别的 App 占用、
                # 不是 FTMS 设备……），打一条警告就够了，不需要整篇堆栈
                log.warning("连接失败：%s", exc)
                return web.json_response({"ok": False, "error": str(exc)}, status=400)
        # 一并把快照返回，前端可以立刻刷新界面，不必干等 WebSocket
        return web.json_response({"ok": True, "trainer": info, "state": self._snapshot})

    async def handle_disconnect(self, request: web.Request) -> web.Response:
        async with self._busy:
            await self._disconnect_trainer()
        return web.json_response({"ok": True})
    # -- 心率带 ----------------------------------------------------------

    async def handle_hr_connect(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        address = str(body.get("address") or "").strip()
        if not address:
            # 没指定就捡信号最强的那根（扫描结果里的第一条）；
            # 没扫描过就退回上次连过的那根——那是最常见的用法："又拿出来骑了"
            if self.hr_devices:
                address = self.hr_devices[0]["address"]
            else:
                address = str(self.settings.get("hr_strap_address", "") or "")
            if not address:
                return web.json_response(
                    {"ok": False, "error": "还没扫描到心率带，先在上面点一次「扫描蓝牙设备」"}, status=400)
        name = ""
        for d in self.hr_devices:
            if str(d.get("address", "")).lower() == address.lower():
                name = str(d.get("name") or "")
                break
        if not name:
            name = str(self.settings.get("hr_strap_name", "") or "")
        async with self._busy:
            await self._disconnect_hr()
            client = HeartRateClient(on_event=self._on_event,
                                     on_data=self._on_hr_data)
            try:
                await client.connect(address, name=name)
            except HeartRateError as exc:
                log.warning("连接心率带失败：%s", exc)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return web.json_response({"ok": False, "error": str(exc)}, status=400)
            self.hr = client
        # 顺手记住这一根，下次可以一键重连
        try:
            self.settings.update({"hr_strap_address": client.address,
                                  "hr_strap_name": client.name or ""})
        except OSError:
            log.exception("保存心率带地址失败")
        if self.session is not None:
            self.session.heart_rate = client
        self._mark_dirty()
        return web.json_response({"ok": True, "hr": self._hr_state()})

    async def handle_hr_disconnect(self, request: web.Request) -> web.Response:
        async with self._busy:
            await self._disconnect_hr()
        self._mark_dirty()
        return web.json_response({"ok": True})

    async def _auto_connect_hr(self, address: str) -> None:
        try:
            async with self._busy:
                await self._disconnect_hr()
                client = HeartRateClient(on_event=self._on_event,
                                         on_data=self._on_hr_data)
                await client.connect(address, name=str(
                    self.settings.get("hr_strap_name", "") or ""))
                self.hr = client
                if self.session is not None:
                    self.session.heart_rate = client
            log.info("已自动接回上次的心率带：%s", client.name or address)
            self._mark_dirty()
        except Exception as exc:                # noqa: BLE001
            log.info("自动连接心率带失败（正常，可能是还没戴）：%s", exc)

    async def _disconnect_hr(self) -> None:
        client = self.hr
        if self.session is not None:
            self.session.heart_rate = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log.exception("断开心率带时出错")
        # 故意**保留**这个对象（只是断开）：界面要能区分"没配过心率带"和
        # "配过、但这会儿没连上"，后者应该显示一个可以重连的状态。

    def _on_hr_data(self, _data: Dict[str, Any]) -> None:
        # 心率自己有 1Hz 左右的推送，这里只负责让界面别等下一次轮询。
        # 真正的取数在训练会话里（它每拍读 hr.latest），所以这里不需要搬数据。
        self._mark_dirty()

    async def handle_start(self, request: web.Request) -> web.Response:
        if self.session is None:
            return web.json_response({"ok": False, "error": "还没连接骑行台"}, status=400)
        body = await self._body(request)
        resp = await self._start_from_body(body)
        if resp.status == 200:
            # 记下这次是怎么配的，「再来一次」直接复用它
            self.last_start_body = body
        return resp

    async def handle_repeat(self, request: web.Request) -> web.Response:
        """用上一次的配置再开一次——包括间歇方案的模板和参数原样复现。"""
        if self.session is None:
            return web.json_response({"ok": False, "error": "还没连接骑行台"}, status=400)
        if not self.last_start_body:
            return web.json_response(
                {"ok": False, "error": "还没有可重复的训练，请先设一次目标"}, status=400)
        return await self._start_from_body(dict(self.last_start_body))

    async def _start_from_body(self, body: Dict[str, Any]) -> web.Response:
        if self.session is None:
            return web.json_response({"ok": False, "error": "还没连接骑行台"}, status=400)
        mode = str(body.get("mode") or ("interval" if body.get("template_id") else "constant"))
        if mode not in ("constant", "interval", "custom", "test"):
            # 以前未知的 mode 会掉进 else 分支，悄悄开一场 100W/60分钟 的默认训练，
            # 用户完全不知道发生了什么。
            return web.json_response(
                {"ok": False, "error": "不认识的训练类型：{}".format(mode)}, status=400)
        erg_mode = str(body.get("erg_mode", ERG_AUTO))
        params = self._params(body.get("params"))

        plan = None
        plan_name = ""
        config: Dict[str, Any] = {"mode": mode, "erg_mode": erg_mode}
        # 心率设置在这一刻冻结进 config：训练中途改设置不该影响正在进行的这一场，
        # 报告里也要能回放出"当时用的是哪套区间"。
        config["heart_rate"] = self._hr_config()
        if mode == "interval":
            try:
                ftp = float(body.get("ftp", 200))
            except (TypeError, ValueError):
                return web.json_response(
                    {"ok": False, "error": "FTP 请填一个数字"}, status=400)
            if not (50 <= ftp <= 600):
                return web.json_response(
                    {"ok": False, "error": "FTP 请填 50-600W 之间的值"}, status=400)
            try:
                plan = build_plan(body.get("template_id"), params, ftp)
            except ValueError as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=400)
            if not plan:
                return web.json_response(
                    {"ok": False, "error": "这个方案没有生成任何步骤，检查一下组数"}, status=400)
            total_s = sum(s["duration_s"] for s in plan)
            if not (0 < total_s <= 6 * 3600):
                return web.json_response(
                    {"ok": False, "error": "方案总时长超出 6 小时，检查一下参数"}, status=400)
            template = get_template(body.get("template_id")) or {}
            plan_name = template.get("name", "间歇训练")
            target_power = float(plan[0]["target_power"])
            duration_min = total_s / 60.0
            config.update({
                "template_id": body.get("template_id"),
                "template_name": plan_name,
                "ftp": ftp,
                "params": params,
            })
        elif mode == "test":
            test_id = str(body.get("test_id") or "")
            proto = get_ftptest(test_id)
            if proto is None:
                return web.json_response(
                    {"ok": False, "error": "未知的测试方案"}, status=400)
            ftp = max(50.0, min(600.0, self._number(body.get("ftp"), 200.0)))
            plan = build_test_plan(test_id, params, ftp)
            total_s = sum(s["duration_s"] for s in plan)
            if not (0 < total_s <= 6 * 3600):
                return web.json_response(
                    {"ok": False, "error": "测试课表时长异常"}, status=400)
            plan_name = proto["name"]
            target_power = float(plan[0]["target_power"])
            duration_min = total_s / 60.0
            # 控制方式由协议决定，不听前端的：坡道测试必须用 ERG 才能逐级加载，
            # 20/8 分钟测试必须自由骑行——用 ERG 跑的话功率被锁死在设定值上，
            # 测出来的是"你设定的数"而不是你的能力，整个测试就废了。
            erg_mode = proto["erg_mode"]
            result = proto["result"]
            config.update({
                "mode": "test",
                "erg_mode": erg_mode,
                "ftp": ftp,
                "test_id": test_id,
                "free_resistance": self.settings.get("free_resistance", 90),
                "test": {
                    "test_id": test_id,
                    "result_kind": result["kind"],
                    "multiplier": result["multiplier"],
                    "source_label": result.get("source_label", ""),
                    "self_paced": bool(proto.get("self_paced")),
                    "note": proto.get("desc", ""),
                },
            })
        elif mode == "custom":
            try:
                ftp = float(body.get("ftp", 200))
            except (TypeError, ValueError):
                return web.json_response(
                    {"ok": False, "error": "FTP 请填一个数字"}, status=400)
            if not (50 <= ftp <= 600):
                return web.json_response(
                    {"ok": False, "error": "FTP 请填 50-600W 之间的值"}, status=400)
            course = self.custom.get(str(body.get("custom_id") or ""))
            if course is None:
                return web.json_response(
                    {"ok": False, "error": "找不到这个自定义课程，可能已经被删掉了"},
                    status=400)
            plan = build_custom_steps(course, ftp)
            total_s = sum(s["duration_s"] for s in plan)
            if not (0 < total_s <= 6 * 3600):
                return web.json_response(
                    {"ok": False, "error": "课程总时长超出 6 小时"}, status=400)
            plan_name = course.get("name") or "自定义课程"
            target_power = float(plan[0]["target_power"])
            duration_min = total_s / 60.0
            config.update({
                "custom_id": course["id"],
                "custom_name": plan_name,
                "power_mode": course.get("power_mode"),
                "ftp": ftp,
            })
        else:
            try:
                target_power = float(body.get("target_power", 100))
                duration_min = float(body.get("duration_min", 60))
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": "参数不合法"}, status=400)
            if not (0 < target_power <= 2000):
                return web.json_response(
                    {"ok": False, "error": "目标功率请填 1-2000W"}, status=400)
            if not (0 < duration_min <= 600):
                return web.json_response(
                    {"ok": False, "error": "时长请填 1-600 分钟"}, status=400)
            config.update({"target_power": target_power, "duration_min": duration_min})

        async with self._busy:
            # 等锁的这段时间里可能已经被 /api/disconnect 置空了，
            # 再读 self.session 就是一个内部报错，而不是"还没连接骑行台"
            if self.session is None:
                return web.json_response({"ok": False, "error": "还没连接骑行台"},
                                         status=400)
            try:
                await self.session.start(target_power, duration_min, erg_mode,
                                         plan=plan, plan_name=plan_name,
                                         config=config)
            except (TrainerError, RuntimeError, Exception) as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True})

    async def handle_templates(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "templates": public_templates()})

    async def handle_plan(self, request: web.Request) -> web.Response:
        """预览：按当前参数生成步骤列表，让用户在开始前就看清整条课表。

        三种来源：内置 HIIT 方案、已保存的自定义课程、编辑器里还没保存的草稿。
        都走同一个出口，前端只需要一套渲染逻辑。
        """
        body = await self._body(request)
        ftp = max(50.0, min(600.0, self._number(body.get("ftp"), 200.0)))
        params = self._params(body.get("params"))
        try:
            if body.get("test_id"):
                test_id = str(body["test_id"])
                # 必须用夹取过的 ftp：以前这里用原始值，于是 FTP 填 5000 时
                # 预览画出一份峰值 575W 的课表，而真开训练时按 600W 夹取，
                # 同一套参数预览和实际是两回事。另外 inf 会让 json 输出非法的
                # Infinity，前端 JSON.parse 直接失败、预览变空白。
                steps = build_test_plan(test_id, params, ftp)
                return web.json_response({"ok": True, "steps": steps,
                                          "stats": test_stats(steps),
                                          "params": clamp_test_params(test_id, params),
                                          "ftp": ftp})

            if isinstance(body.get("course"), dict):
                # 编辑器草稿：过一遍校验就直接展开，不落盘
                steps = build_custom_steps(normalize_course(body["course"], ftp), ftp)
                return web.json_response({"ok": True, "steps": steps,
                                          "stats": plan_stats(steps), "ftp": ftp})

            if body.get("custom_id"):
                course = self.custom.get(str(body["custom_id"]))
                if course is None:
                    return web.json_response(
                        {"ok": False, "error": "找不到这个自定义课程"}, status=404)
                steps = build_custom_steps(course, ftp)
                return web.json_response({"ok": True, "steps": steps,
                                          "stats": plan_stats(steps), "ftp": ftp})

            template_id = body.get("template_id")
            steps = build_plan(template_id, params, ftp)
            resolved = clamp_params(template_id, params)
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        # 把夹取后的参数一并返回，前端输入框可以据此纠正用户填的越界值
        return web.json_response({"ok": True, "steps": steps,
                                  "stats": plan_stats(steps),
                                  "params": resolved, "ftp": ftp})

    # ------------------------------------------------------------------
    # 自定义课程
    # ------------------------------------------------------------------

    async def handle_custom_list(self, request: web.Request) -> web.Response:
        ftp = max(50.0, min(600.0, self._number(request.query.get("ftp"), 200.0)))
        items = []
        for course in self.custom.list():
            entry = dict(course)
            try:
                entry["stats"] = course_stats(course, ftp)
            except ValueError:
                entry["stats"] = {}
            items.append(entry)
        # 不要把数据目录的绝对路径暴露给局域网里的每个客户端
        return web.json_response({"ok": True, "courses": items,
                                  "default": default_course()})

    async def handle_custom_new(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "course": default_course()})

    async def handle_custom_save(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        ftp = max(50.0, min(600.0, self._number(body.get("ftp"), 200.0)))
        try:
            course = self.custom.upsert(body, ftp)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except OSError as exc:
            log.exception("保存自定义课程失败")
            return web.json_response(
                {"ok": False, "error": "课程没能写进磁盘：{}".format(exc)}, status=500)
        return web.json_response({"ok": True, "course": course,
                                  "courses": self.custom.list()})

    async def handle_custom_delete(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        course_id = str(body.get("id") or "")
        try:
            removed = self.custom.delete(course_id)
        except OSError as exc:
            log.exception("删除自定义课程失败")
            return web.json_response(
                {"ok": False, "error": "删除没能写入磁盘：{}".format(exc)}, status=500)
        if not removed:
            return web.json_response({"ok": False, "error": "找不到这个课程"}, status=404)
        return web.json_response({"ok": True, "courses": self.custom.list()})

    def _hr_config(self) -> Dict[str, Any]:
        """这次训练用的心率参数（来自当前设置）。"""
        mode = str(self.settings.get("hr_zone_mode", HR_MODE_MAX) or HR_MODE_MAX)
        if mode not in (HR_MODE_MAX, HR_MODE_RESERVE):
            mode = HR_MODE_MAX
        return {
            "max_hr": self.settings.get("hr_max"),
            "rest_hr": self.settings.get("hr_rest"),
            "zone_mode": mode,
            "hr_limit_enabled": bool(self.settings.get("hr_limit_enabled")),
            "hr_limit_bpm": self.settings.get("hr_limit_bpm"),
        }

    @staticmethod
    def _number(raw: Any, fallback: float) -> float:
        """宽松地把入参转成数字，转不动或 NaN 就用兜底值。"""
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return fallback
        # NaN 和 ±Infinity 都要挡住。JSON 里 1e400 是合法的，会解析成 inf，
        # 后面 int(inf) 会抛 OverflowError ——那是 500，不是干净的 400。
        if not math.isfinite(value):
            return fallback
        return value

    @staticmethod
    def _params(raw: Any) -> Dict[str, Any]:
        """params 必须是对象；传数组/字符串/数字时按"没传"处理，而不是崩掉。"""
        return raw if isinstance(raw, dict) else {}

    # ------------------------------------------------------------------
    # 训练报告
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # FTP 测试 / 设置
    # ------------------------------------------------------------------

    async def handle_test_list(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "tests": public_tests()})

    async def handle_settings_get(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "settings": self.settings.values})

    async def handle_settings_save(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        try:
            values = self.settings.update(body)
        except OSError as exc:
            log.exception("保存设置失败")
            return web.json_response(
                {"ok": False, "error": "设置没能写进磁盘：{}".format(exc)}, status=500)
        return web.json_response({"ok": True, "settings": values})

    async def handle_free_resistance(self, request: web.Request) -> web.Response:
        """自由骑行中微调阻力档位。"""
        session = self.session
        if session is None:
            return web.json_response({"ok": False, "error": "未连接"}, status=400)
        body = await self._body(request)
        # 夹取之后再回显。以前回显的是客户端原值（比如 9999），而实际生效的是 255。
        raw = max(0, min(255, int(self._number(body.get("raw"), 90))))
        await session.set_free_resistance(raw)
        try:
            self.settings.update({"free_resistance": raw})
        except OSError:
            # 阻力已经调到骑行台上了，只是没记住。如实告知，但不算请求失败。
            log.exception("自由骑行阻力保存失败")
            return web.json_response({"ok": True, "raw": raw,
                                      "warning": "阻力已生效，但没能写入设置文件"})
        return web.json_response({"ok": True, "raw": raw})

    async def handle_report_list(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "reports": self.reports.list()})

    async def handle_report_get(self, request: web.Request) -> web.Response:
        report_id = request.match_info.get("report_id", "")
        report = self.reports.get(report_id)
        if report is None:
            return web.json_response({"ok": False, "error": "找不到这份训练报告"},
                                     status=404)
        return web.json_response({"ok": True, "report": report})

    async def handle_report_delete(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        if not self.reports.delete(str(body.get("id") or "")):
            return web.json_response({"ok": False, "error": "找不到这份训练报告"},
                                     status=404)
        return web.json_response({"ok": True, "reports": self.reports.list()})

    async def handle_report_clear(self, request: web.Request) -> web.Response:
        removed = self.reports.clear()
        return web.json_response({"ok": True, "removed": removed, "reports": []})

    async def handle_interval_skip(self, request: web.Request) -> web.Response:
        # 先把引用抓到本地：下面 await 读 body 的时候，另一个请求可能已经把
        # self.session 置空了，之后再读就是 AttributeError → 500。
        session = self.session
        if session is None:
            return web.json_response({"ok": False, "error": "未连接"}, status=400)
        body = await self._body(request)
        try:
            delta = int(self._number(body.get("delta"), 1))
        except (TypeError, ValueError):
            delta = 1
        # 和 停止/断开 共用一把锁：不然"跳过"可能和收尾并发，
        # 在会话已被置空/已结束时去动它
        async with self._busy:
            await session.skip_step(delta)
        return web.json_response({"ok": True})

    async def handle_pause(self, request: web.Request) -> web.Response:
        session = self.session
        if session is None:
            return web.json_response({"ok": False, "error": "未连接"}, status=400)
        async with self._busy:
            await session.pause()
        return web.json_response({"ok": True})

    async def handle_resume(self, request: web.Request) -> web.Response:
        session = self.session
        if session is None:
            return web.json_response({"ok": False, "error": "未连接"}, status=400)
        # 必须和 stop() 串行：以前并行时，"继续"可以在 stop() 收尾之后把状态又置回
        # running，留下一个"状态在跑、主循环已经没了"的僵尸会话（计时不动、也不发指令）
        async with self._busy:
            await session.resume()
        return web.json_response({"ok": True})

    async def handle_stop(self, request: web.Request) -> web.Response:
        # 先把会话引用抓到本地：stop() 内部要等骑行台应答（可能 2.5 秒），
        # 这个窗口里 /api/disconnect 可能已经把 self.session 置空了，
        # 之后再读 self.session.summary 就是 AttributeError → 500。
        session = self.session
        if session is None:
            return web.json_response({"ok": False, "error": "未连接"}, status=400)
        async with self._busy:
            await session.stop()
        return web.json_response({"ok": True, "summary": session.summary})

    async def handle_dismiss(self, request: web.Request) -> web.Response:
        """关掉训练总结，回到可以重新设定的状态。"""
        session = self.session
        if session is None:
            return web.json_response({"ok": False, "error": "未连接"}, status=400)
        async with self._busy:
            await session.dismiss()
        return web.json_response({"ok": True})

    async def handle_target(self, request: web.Request) -> web.Response:
        session = self.session
        if session is None:
            return web.json_response({"ok": False, "error": "未连接"}, status=400)
        body = await self._body(request)
        # 注意不能用 min/max 直接夹：Python 里 min(2000.0, nan) 得到的是 2000.0，
        # 于是 NaN 会被"夹"成 2000W 这个合法值而悄悄生效。
        target = self._number(body.get("target_power"), -1.0)
        if not (0 < target <= 2000):
            return web.json_response(
                {"ok": False, "error": "目标功率请填 1-2000W"}, status=400)
        async with self._busy:
            await session.set_target_power(target)
        return web.json_response({"ok": True, "target_power": target})

    async def handle_resistance(self, request: web.Request) -> web.Response:
        session = self.session
        if session is None:
            return web.json_response({"ok": False, "error": "未连接"}, status=400)
        body = await self._body(request)
        raw = int(self._number(body.get("raw"), -1))
        # 骑行台那边只认 0-255；不夹取的话界面和报告会显示一个根本没生效的值
        if not (0 <= raw <= 255):
            return web.json_response({"ok": False, "error": "阻力请填 0-255"}, status=400)
        try:
            async with self._busy:
                await session.set_manual_resistance(raw)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True})


def run(host: str = "0.0.0.0", port: int = 8765, use_simulator: bool = False,
        simulator_responds: bool = True, simulator_advertise: Optional[bool] = None,
        open_browser: bool = True) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    server = Server(use_simulator=use_simulator, simulator_responds=simulator_responds,
                    simulator_advertise=simulator_advertise)
    app = server.build_app()

    url_local = "http://127.0.0.1:{}".format(port)
    url_lan = "http://{}:{}".format(lan_ip(), port)

    print()
    print("  iBike 智能骑行控制台已启动")
    print("  " + "-" * 46)
    print("  本机访问 :  {}".format(url_local))
    print("  手机访问 :  {}".format(url_lan))
    print("  " + "-" * 46)

    try:
        import qrcode  # type: ignore

        qr = qrcode.QRCode(border=1)
        qr.add_data(url_lan)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        print("  用手机相机扫描上面的二维码即可打开控制台")
    except Exception:
        pass

    print("  按 Ctrl+C 退出")
    print()

    if open_browser:
        try:
            import threading
            import webbrowser

            threading.Timer(1.0, lambda: webbrowser.open(url_local)).start()
        except Exception:
            pass

    web.run_app(app, host=host, port=port, print=None)
