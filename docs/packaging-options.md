# 打包与移动端：可行性评估

> **状态：只做了调研，没有实施。** 写在这里是为了以后真要做的时候不用重新查一遍。
> 结论、难度、坑、参考链接都在下面；每一条结论后面都注明了依据。

---

## 结论速览

| 目标 | 能不能 | 难度 | 建议 |
|---|---|---|---|
| **Mac 上变成双击即用的 App** | 能 | **1~3 天** | 值得做。先花几小时做个"菜单栏启动器"验证手感，再决定要不要正经打包 |
| **安卓 App（手机独立运行）** | 能 | **1~2 周**（自用）/ 3~6 周（正式） | 先考虑下面第 0 条那条 0 代码的路 |
| **原生重写**（SwiftUI / Kotlin） | 能 | **3~6 周** | 一般不值得——核心价值在训练逻辑，换语言重写不会更好 |
| **手机当客户端（现状）** | 已经能用 | 0 | 手机连同一 Wi-Fi 打开 `192.168.x.x` 即可，缺的只是"一台常开的机器" |

---

## 现状：为什么这件事比一般情况容易

现在的运行方式是 `./run.sh` → `aiohttp` 服务 → 浏览器打开界面。这个架构有两个特点，
让"打包到别的壳里"变得简单：

1. **前端是纯静态资源 + JSON over HTTP/WebSocket 契约**（零依赖原生 JS）。
   塞进任何 WebView 都行，**前端一行都不用改**——只要让窗口指向 `http://127.0.0.1:<port>`。
2. **蓝牙走的是标准协议**（FTMS `0x1826`、标准心率服务 `0x180D`），
   不依赖任何厂商 SDK。`bleak` 已经把三个平台的差异封在各自的后端里。

真正要重做的只有两件事：**权限声明**和**进程存活**。

---

## 四条路（按工作量排序）

### 第 0 条：服务跑在一台常开的小设备上（0 行代码）

手机已经是客户端了，缺的只是"服务器跑在哪"。把现在这份程序原样跑在一台常开的设备上即可：

- 树莓派 Zero 2W / Pi 4、旧安卓手机（Linux 侧）、NAS、旧迷你主机
- 电脑完全不用开；手机浏览器打开局域网地址就能用
- **零代码、零风险**，缺点是多个设备要供电

如果目标只是"不想为了骑车开着电脑"，这条最划算。

### 第 1 条：Mac 应用

**怎么做**：`pywebview` 提供一个原生窗口（macOS 上是 WKWebView），把现有的 aiohttp 服务
跑在后台线程，窗口指向 `http://127.0.0.1:8765`。[pywebview 官方就有 HTTP server 示例](https://pywebview.flowrl.com/examples/http_server.html)，
正是为这种用法写的。打包用 `py2app`（和 `pyobjc` 同一个作者，对这类依赖最顺）或 PyInstaller。

**坑，按危险程度排**：

1. **必须在 Info.plist 里声明 `NSBluetoothAlwaysUsageDescription`**，否则 macOS 会直接杀掉进程，
   报错很不直观。这不是理论风险，[dotnet/maui-labs](https://github.com/dotnet/maui-labs/issues/253)
   和 [claude-code](https://github.com/anthropics/claude-code/issues/58385) 都栽在这一条上。
2. **数据必须搬出 `.app` 包**。`.app` 升级时是整个被替换的，包内只读——训练报告要是还在包里的
   `data/`，一次升级历史全没。要改成 `~/Library/Application Support/iBike/`。
   现有代码的 `IBIKE_DATA_DIR` 机制正好能做这件事，但**忘了就是数据丢失**。
3. **签名与权限（TCC）**。好处是权限从此属于"这个 App"而不是"终端"——README 里那段
   "要去隐私设置里给终端勾蓝牙"可以删掉。麻烦是未签名的 App 每次重新打包签名都会变，
   macOS 可能重新弹窗甚至静默拒绝，要反复试几次。
4. **打包工具链 + 开发循环变慢**。捆绑 CPython + pyobjc + aiohttp，第一次跑通要一两天。
   副作用：现在"改代码 → 刷新页面"会变成"改代码 → 重新打包 → 重启 App"。
5. **体积** 约 60~120 MB（现在整个项目不含 venv 才 1 MB）。
6. **分发给别人要花钱**：未签名的 App 在别人机器上会被 Gatekeeper 拦（"无法验证开发者"，
   更糟会显示"已损坏"）。正经分发需要 Apple 开发者账号 **$99/年** + 公证。这是流程和钱的问题。

**分级**：

| 级别 | 做什么 | 工作量 |
|---|---|---|
| 0 | 菜单栏启动器 / `.app` 包装脚本（`rumps`、Platypus），双击启动不弹终端 | 几小时 |
| 1 | pywebview 窗口 + py2app 打包 + Info.plist 权限 + 数据搬到 Application Support | 1~3 天 |
| 2 | 图标、DMG、代码签名、公证、开机自启 | +1~2 天 + $99/年 |
| 3 | SwiftUI 原生重写（CoreBluetooth 重写蓝牙层，WKWebView 复用前端） | 3~6 周 |

**两个细节**：① App 得在骑行期间保持运行（窗口可以最小化，进程不能退），跟现在"终端要开着"
是一回事，只是不用盯着终端；② 建议**保留 `./run.sh` 作为开发入口**，App 只是给日常骑行用的壳，
否则改一行都要重新打包会很难受。

### 第 2 条：安卓 App

**好消息：`bleak` 官方支持安卓了。** 蓝牙这一层已经不是障碍：

- [`bleak.backends.android`](https://bleak.readthedocs.io/en/latest/backends/android.html)
  —— 用 BeeWare/Briefcase 打包（底层是 Chaquopy），**蓝牙权限由 bleak 自己弹窗申请**，
  只要在 `pyproject.toml` 里声明 `permission.bluetooth`，并在 gradle 配置里挂上三个静态代理
  把 Java 回调转给 Python。
- 另有一个更老的 python-for-android 后端 `bleak.backends.p4android`（文档注明"只在 Kivy 框架下测过"）。

也就是说 `trainer.py` / `heartrate.py` 这类**标准 bleak 用法基本能原样搬过去**。

**坑**：

1. **Python 版本**：官方安卓后端要求 **Python 3.13+**（[PEP 738](https://peps.python.org/pep-0738/)
   之后安卓才算官方平台），而本项目现在最低支持 3.9。代码本身大概率能跑 3.13，
   但要决定"同时支持两个版本"还是"把最低版本抬上去"。
2. **息屏 / 后台存活——最难，也最容易被低估**。60 分钟骑行里屏幕会锁、系统会省电杀进程。
   必须做**前台服务 + `PARTIAL_WAKE_LOCK` + 常驻通知**。这块跟蓝牙无关，是纯安卓工程活，
   而且只能真机反复试。
3. **打包工具链**：buildozer 或 briefcase + Android SDK/NDK + 签名。第一次跑通通常要一两天。
4. **扫描限流**：安卓限制 **30 秒内最多 5 次扫描启停**，超了会直接抛错
   （bleak 自己记账并给出"还要等多久"）。"扫描"按钮要加节流和友好提示。
5. **重连更挑**：安卓 GATT 栈在断线重连上比 macOS 敏感，会话层要重测一遍。
6. **数据导出**：数据目录变成应用私有空间，想把报告 JSON 导出来要额外做系统文件选择器。

**已经被排除的一条路**：**Termux 里直接跑不行**。安卓的蓝牙栈不是 BlueZ（Termux 里装不到 bluez），
而 bleak 的 Linux 后端依赖 BlueZ/D-Bus，所以手机终端里跑不通 BLE
（可参考 [这个提问](https://stackoverflow.com/questions/72500811/ble-in-python-script-in-termux)）。

**分级**：自用（Briefcase 打包 + WebView + 前台服务）1~2 周；正式发布（图标、通知、
可上架、可分发）3~6 周。

### 第 3 条：原生重写

SwiftUI（macOS）或 Kotlin（安卓）重写后端，前端 WebView 复用。
唯一"完全像原生 App"的路，也是唯一能上架应用商店的路。约 4,000 行 Python 要移植。

**除非要做成产品发布，否则不建议**——这个项目的价值在训练逻辑（计划引擎、两条控功率路径、
心率分析、报告），换语言重写一遍不会让它更好。

---

## 不管走哪条路都躲不掉的三件事

1. **蓝牙权限声明**：macOS 要 Info.plist 里的 usage description；安卓要 manifest + 运行时请求。
   少了都是"能编译、能启动、一碰蓝牙就死"。
2. **进程存活**：安卓必须前台服务 + wakelock；macOS 宽松些，但窗口/进程不能退出，
   否则骑行台连接会断。
3. **数据目录脱离程序本体**：`.app` / APK 都会在升级时被整体替换，
   数据必须放到用户目录或应用私有目录。

---

## 参考

- [bleak Android 后端（BeeWare/Briefcase + Chaquopy）](https://bleak.readthedocs.io/en/latest/backends/android.html)
  —— 含权限配置、静态代理、扫描限流说明
- [bleak macOS 后端（CoreBluetooth）](https://bleak.readthedocs.io/en/stable/backends/macos.html)
- [pywebview：本地 HTTP server + 原生窗口的官方示例](https://pywebview.flowrl.com/examples/http_server.html)
- [python-for-android 文档](https://python-for-android.readthedocs.io/en/latest/)（含 `webview` bootstrap）
- 缺少 `NSBluetoothAlwaysUsageDescription` 导致进程被杀的实例：
  [dotnet/maui-labs#253](https://github.com/dotnet/maui-labs/issues/253)、
  [anthropics/claude-code#58385](https://github.com/anthropics/claude-code/issues/58385)
- [Termux 里跑 BLE 的讨论](https://stackoverflow.com/questions/72500811/ble-in-python-script-in-termux)（结论：走不通）
