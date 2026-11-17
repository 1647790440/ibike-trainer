#!/usr/bin/env bash
# 打一个双击即用的 iBike.app（macOS）。菜单栏常驻，退出会优雅收尾。
#
#   packaging/make_app.sh                       # 装到 ~/Applications/iBike.app
#   OUT=/some/where/iBike.app packaging/make_app.sh
#
# 设计取舍：**不把 Python 和依赖打进包里**，App 只是个壳——
#   · 改代码不用重新打包，重启 App 就生效（`.venv` 和项目目录是你的正经开发环境）
#   · 体积几十 KB，不是上百 MB
#   · 代价：项目目录不能随便搬；搬了之后重新跑一次这个脚本即可
# 真要分发给别人（自包含 + 签名 + 公证）那是另一档，见 docs/packaging-options.md。
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT="$(pwd -P)"
OUT="${OUT:-$HOME/Applications/iBike.app}"

if [ ! -f "$PROJECT/main.py" ]; then
  echo "看起来不在项目根目录：$PROJECT" >&2
  exit 1
fi

PYTHON="$PROJECT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "找不到 $PYTHON —— 先跑一次 ./run.sh 把虚拟环境建起来。" >&2
  exit 1
fi
if ! "$PYTHON" -c "import AppKit" 2>/dev/null; then
  echo "这个 venv 里没有 pyobjc（AppKit），菜单栏用不了。跑一次 ./run.sh 会装上。" >&2
  exit 1
fi

echo "打包目录: $OUT"
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"

# ---- Info.plist ----
# 注意 NSBluetoothAlwaysUsageDescription：少了这一条，macOS 会在碰蓝牙时**直接
# 干掉进程**（不是弹窗拒绝），排查起来非常不直观。
cat > "$OUT/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>iBike</string>
  <key>CFBundleDisplayName</key><string>iBike</string>
  <key>CFBundleIdentifier</key><string>local.ibike.trainer</string>
  <key>CFBundleExecutable</key><string>iBike</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>10.15</string>
  <!-- 只驻留菜单栏：不占 Dock、不抢焦点 -->
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <!-- 蓝牙权限说明：没有它，一碰蓝牙就被系统杀掉 -->
  <key>NSBluetoothAlwaysUsageDescription</key>
  <string>iBike 需要用蓝牙连接你的骑行台和心率带。</string>
  <key>NSBluetoothPeripheralUsageDescription</key>
  <string>iBike 需要用蓝牙连接你的骑行台和心率带。</string>
  <!-- 项目目录在「桌面」下，而 macOS 默认禁止 App 读桌面。没有这些说明，
       系统会**静默拒绝**（连授权弹窗都不弹）——双击后就是"完全没反应"。 -->
  <key>NSDesktopFolderUsageDescription</key>
  <string>iBike 要读取桌面上的项目目录（程序本体、虚拟环境和训练数据都在那里）。</string>
  <key>NSDocumentsFolderUsageDescription</key>
  <string>iBike 需要读取训练数据目录（如果你把项目放在了「文稿」里）。</string>
  <key>NSDownloadsFolderUsageDescription</key>
  <string>iBike 需要读取训练数据目录（如果你把项目放在了「下载」里）。</string>
</dict>
</plist>
PLIST

# ---- 可执行文件：必须编译成真正的 Mach-O ----
# 用 shell 脚本当 CFBundleExecutable 时，LaunchServices 把「主进程」算成 /bin/bash，
# TCC 的权限归属也跟着算到 bash 头上——bash 没有用途说明字符串，于是访问「桌面」
# 时系统会**静默拒绝**（表现为"双击完全没反应"）。编译版才有正常的授权弹窗。

if ! command -v cc >/dev/null 2>&1; then
  echo "找不到 cc 编译器（需要 Xcode Command Line Tools：xcode-select --install）" >&2
  exit 1
fi
echo "编译启动器…"
cc -O2 -Wall \
   -DIB_PYTHON="\"$PYTHON\"" \
   -DIB_LAUNCHER="\"$PROJECT/packaging/launcher.py\"" \
   -DIB_PYVENV_CFG="\"$PROJECT/.venv/pyvenv.cfg\"" \
   -o "$OUT/Contents/MacOS/iBike" packaging/app_main.c
chmod +x "$OUT/Contents/MacOS/iBike"

# ---- 启动器里读的配置 ----
cat > "$OUT/Contents/Resources/app-config.json" <<EOF
{"project_root": "$PROJECT", "python": "$PYTHON"}
EOF

# ---- 校验 plist ----
plutil -lint "$OUT/Contents/Info.plist" >/dev/null

# ---- 临时签名（本机运行足够；ad-hoc 签名变化会重新弹蓝牙授权） ----
if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - "$OUT" >/dev/null 2>&1 \
    && echo "已用 ad-hoc 签名（本机可运行）" \
    || echo "签名失败——不影响本机运行，只是可能每次都要重新授权蓝牙"
fi

cat <<TIP

完成 ✓  双击这里启动：$OUT

· **第一次打开会问「iBike 想访问你的桌面文件夹」→ 点允许**（项目就在桌面上，
  不给权限的话 App 读不到程序本体，表现就是"双击没反应"）
· 启动后菜单栏会出现「iBike · 待机」，点开可以看状态、打开控制台、显示日志、退出
· 数据（报告/设置）还是项目目录下的 data/，和 ./run.sh 看到的是同一份
· 改代码**不用重新打包**：退出 App 再双击就生效
· 项目目录搬家之后要重新跑一次这个脚本；命令行调试照旧用 ./run.sh
TIP
