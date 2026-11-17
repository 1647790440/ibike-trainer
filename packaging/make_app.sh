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
</dict>
</plist>
PLIST

# ---- 可执行文件：直接 exec 项目里的 python + 启动器 ----
cat > "$OUT/Contents/MacOS/iBike" <<EOF
#!/bin/bash
# 由 packaging/make_app.sh 生成（项目: ${PROJECT}）
# 把 -psn_* 这种 Finder 传进来的参数滤掉，其余照常透传
args=()
for a in "\$@"; do
  case "\$a" in -psn_*) ;; *) args+=("\$a");; esac
done
exec "$PYTHON" "$PROJECT/packaging/launcher.py" "\${args[@]}"
EOF
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

· 启动后菜单栏会出现「iBike · 待机」，点开可以看状态、打开控制台、显示日志、退出
· 数据（报告/设置）还是项目目录下的 data/，和 ./run.sh 看到的是同一份
· 改代码**不用重新打包**：退出 App 再双击就生效
· 项目目录搬家之后要重新跑一次这个脚本；命令行调试照旧用 ./run.sh
TIP
