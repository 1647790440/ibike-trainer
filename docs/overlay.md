# 悬浮窗：骑台子看视频不用切窗口

在「1 连接设备」页点 **「悬浮显示」**，屏幕右上角就会出现一个小窗，把功率、剩余
时间、心率、踏频和状态浮在画面上；再点一下关掉。**关掉不影响训练**。

```
138W                      ← 功率（偏离目标 >5% 变黄、>12% 变红）
剩余 32:18                ← 剩余时间（间歇课自动变成"本段"）
♥148 Z4 · 78rpm · / 140W  ← 心率+区间 / 踏频 / 目标
● 进行中 · 原生 ERG        ← 状态：已暂停 / 自动暂停 / 骑行台掉线·正在自动重连
```

## 命令行用法（可选）

一般不用敲命令，网页上的按钮就够了。想单独控制、或者换位置/字号时用：

```bash
./run-overlay.sh                      # 默认贴屏幕右上角
./run-overlay.sh --anchor top-left    # 换角：top-right / top-left / bottom-right / bottom-left
./run-overlay.sh --pos 24,60          # 距所选那个角的边距（默认 24,24）
./run-overlay.sh --alpha 0.8          # 整体透明度（默认 0.92）
./run-overlay.sh --size 1.3           # 字号缩放（默认 1.0）
./run-overlay.sh --width 260 --height 130   # 想放更多字就一起放大
./run-overlay.sh --port 8765          # 服务端口（默认 8765）
./run-overlay.sh --check              # 不开窗口，只打印它将要显示的内容
./run-overlay.sh --selftest           # 用假数据检查取数/配色/位置计算

# 命令行开关同一个窗口（等价于点按钮）
curl -X POST -H 'Content-Type: application/json' \
     -d '{"action":"toggle"}' http://127.0.0.1:8765/api/overlay
```

## 怎么实现的（以及为什么这么做）

**窗口**（`overlay.py`，约 380 行，纯 pyobjc，零新增依赖——`bleak` 早就把
`pyobjc` 装进 venv 了）：

| 属性 | 作用 |
|---|---|
| 无边框 + `opaque=0` + 背景 clear | 只有文字浮着，不是一块半透明方块 |
| `level = NSFloatingWindowLevel` | 永远在最上层 |
| `collectionBehavior` 带 `FullScreenAuxiliary` | 能盖在**全屏视频**上面（普通置顶窗口在全屏空间里看不见） |
| `ignoresMouseEvents = True` | **点击穿透**，不挡你点下面的视频 |
| `activationPolicy = Accessory` | 不进 Dock、不抢焦点、不打断视频 |
| 文字带阴影 | 视频是白底时也看得清 |
| 贴 `visibleFrame` 而不是屏幕本身 | 系统已经把菜单栏和 Dock 扣掉了，所以贴右上角不会被菜单栏压住 |

**进程管理**（`ibike/server.py` 里的 `/api/overlay`）：

- 按钮打 `POST /api/overlay {"action": "toggle"}`，服务据此 `Popen` 一个独立的
  `overlay.py`（参数带上自己的端口，端口从请求上取）。
- 独立进程 + 只读 `/api/state`：它崩了碰不到训练循环。
- `start_new_session=True`：终端里的 Ctrl+C 不会在悬浮窗那边冒一堆
  `KeyboardInterrupt`；服务退出时由 `cleanup()` 显式收掉，不留孤儿窗口。
- 启动后等 0.8 秒看它有没有立刻退出；退出了就把退出码和日志尾巴当作错误返回，
  前端直接弹出来（不是静默失败）。
- `_overlay_running()` 每次都 `poll()`：进程自己没了（崩了/被关）就报"没开"，
  按钮能重新点，不会卡在"显示已开启但其实没有"。
- 状态存在服务端（`/api/state` 的 `overlay_running`），所以刷新页面、换标签页
  按钮状态都是对的。

## 踩过的坑（都修掉了）

- **按钮看得见、按下去没反应**：`index.html` 和 `app.js` 是分别缓存的，浏览器
  可能把新的 HTML 和旧的 JS 混着用——旧 JS 里根本没有这个按钮的点击处理。
  现在服务发首页时会给静态资源带上 `?v=<文件 mtime>`，前端一改浏览器就必须重取。
- **"我改了代码怎么没生效"**：前端文件是每次请求现读的（刷新即可），
  **Python 代码是进程启动时加载的（必须重启服务）**。前端遇到 404 会直接提示
  "服务还是旧代码，请重启"。
- 定位时踩的坑：`--anchor` 靠 `visibleFrame` 计算，四个角的坐标有自检
  （`--selftest`），不会算错。

## 已知限制

- **位置固定、不能用鼠标拖**：点击穿透和拖动天生矛盾（穿透后鼠标事件根本不进
  窗口）。想挪位置改 `--anchor` / `--pos` 重启；想要拖拽的话得加一个"编辑位置
  模式"。
- **只在主屏幕**：多显示器时固定显示在主屏。
- **全屏视频**：`FullScreenAuxiliary` 能盖住标准全屏空间，但有些播放器用的是
  非标准全屏（比如原生全屏的 QuickTime/TV），需要实测。
- **服务没起来时**显示「iBike 未运行」；短暂网络抖动不会闪（保留上一帧，超过
  5 秒才算断）。
- 它**不写任何数据**：不碰报告、不碰设置，纯显示。

## 测试

```bash
.venv/bin/python tests/test_overlay_toggle.py   # 开关接口（不真开窗口）
node tests/test_frontend_smoke.js               # [10c] 节：按钮文案/状态同步/点击真的 POST
.venv/bin/python overlay.py --selftest          # 排版、配色、位置计算
```
