#!/bin/zsh
# 构建 P2-12 静音 AX 通知探针。产物在 gitignored 的 app/build/notify-probe/。
#   build.sh            只重编译探针 CLI（不影响已授予启动器的辅助功能权限）
#   build.sh --launcher 同时（重新）构建并 ad-hoc 签名授权宿主 RokidNotifyProbe.app
#                       ——会改变 cdhash，机主需在「隐私与安全性 → 辅助功能」重新勾选。
set -euo pipefail
HERE=${0:A:h}
ROOT=${HERE:h:h:h}
OUT=$ROOT/app/build/notify-probe
APP=$OUT/RokidNotifyProbe.app
mkdir -p "$OUT"

swiftc -O "$HERE/NotifyProbe.swift" -o "$OUT/notify-probe-cli"
codesign --force --sign - --identifier cn.rokid.allen.notifyprobe.cli "$OUT/notify-probe-cli" >/dev/null 2>&1
echo "cli: $OUT/notify-probe-cli"

if [[ "${1:-}" == "--launcher" || ! -d "$APP" ]]; then
  mkdir -p "$APP/Contents/MacOS"
  swiftc -O "$HERE/Launcher.swift" -o "$APP/Contents/MacOS/RokidNotifyProbe"
  cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
  codesign --force --sign - "$APP"
  echo "app: $APP（新签名，需要重新授权辅助功能）"
fi
