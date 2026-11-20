#!/bin/bash
set -eu
cd "$(dirname "$0")/.."
root="$PWD"
version="$(cat VERSION)"
arch="$(uname -m)"
python="${BUILD_PYTHON:-$root/.venv-build/bin/python}"
test -x "$python" || { echo "请先按 docs/PACKAGING.md 准备打包环境。"; exit 1; }
"$python" app/build_android.py
mkdir -p dist
stage="$(mktemp -d "$root/dist/.stage.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
bundle="$stage/Rokid-Allen-Unbricked.app"
content="$bundle/Contents"
resources="$content/Resources"
mkdir -p "$content/MacOS" "$resources/app/host" "$resources/design"
"$python" -m PyInstaller --noconfirm --clean --onedir --name rokidctl --paths app/host \
    --collect-submodules backends --collect-submodules miniapps --collect-submodules homeassistant \
    --distpath "$stage/helpers" --workpath "$stage/work" --specpath "$stage" macos/rokidctl.py
mv "$stage/helpers/rokidctl" "$resources/engine"
cp app/build/rokid-bridge.apk "$resources/"
# The index is the allowlist. Local configs, accounts, and ignored artwork never enter the bundle.
git ls-files -z -- app/host/web design/avatars | while IFS= read -r -d '' path; do
    mkdir -p "$resources/$(dirname "$path")"
    cp "$path" "$resources/$path"
done
mkdir -p "$resources/app/host/miniapps/certs"
cp app/host/miniapps/certs/bambu-lan.cert "$resources/app/host/miniapps/certs/"
cp LICENSE docs/THIRD-PARTY.md "$resources/"
cp app/android/assets/ha-mdi-license.txt "$resources/"
"$python" - "$resources/Licenses" <<'PYLICENSE'
import importlib.metadata as metadata
from pathlib import Path
import shutil, sys, sysconfig
target = Path(sys.argv[1])
target.mkdir()
shutil.copyfile(Path(sysconfig.get_path("stdlib")) / "LICENSE.txt", target / "CPython.txt")
for package in ("paho-mqtt", "pyinstaller"):
    dist = metadata.distribution(package)
    for path in dist.files:
        if path.name.startswith(("LICENSE", "COPYING")):
            shutil.copyfile(dist.locate_file(path), target / (package + "-" + path.name))
PYLICENSE
xcrun swiftc -O macos/main.swift -framework AppKit -o "$content/MacOS/RokidAllen"
cat > "$content/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>RokidAllen</string>
<key>CFBundleName</key><string>Rokid-Allen-Unbricked</string>
<key>CFBundleIdentifier</key><string>io.github.super480.rokid-allen-unbricked</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>207</string>
<key>CFBundleShortVersionString</key><string>$version</string>
<key>NSLocalNetworkUsageDescription</key><string>连接你指定的若琪音箱以验证 ADB、配对和提供语音卡片。</string>
</dict></plist>
PLIST
plutil -lint "$content/Info.plist"
codesign --force --sign - "$bundle"
codesign --verify --deep --strict "$bundle"
"$resources/engine/rokidctl" --help >/dev/null
target="$root/dist/Rokid-Allen-Unbricked-$version-$arch-local"
# Verify the new bundle before replacing the previous local build.
mkdir -p "$stage/image"
ditto "$bundle" "$stage/image/Rokid-Allen-Unbricked.app"
ln -s /Applications "$stage/image/Applications"
hdiutil create -quiet -volname Rokid-Allen-Unbricked -srcfolder "$stage/image" -format UDZO "$stage/install.dmg"
hdiutil verify "$stage/install.dmg" >/dev/null
mkdir -p "$target"
if [ -d "$target/Rokid-Allen-Unbricked.app" ]; then mv "$target/Rokid-Allen-Unbricked.app" "$stage/previous.app"; fi
mv "$bundle" "$target/"
mv "$stage/install.dmg" "$target.dmg"
shasum -a 256 "$target.dmg"
