# 在 Mac 上打包 Rokid-Allen-Unbricked

此指南生成本机架构的、自用测试的 App 与 DMG。没有 Mac 开发者账号也可完成本地构建。使用者以后打开打好的 App 时需要 adb，运行 Host 所需的 Python 已随包嵌入。

## 准备一次

1. 安装 Apple Command Line Tools（终端运行 `xcode-select --install`）。确认 `xcrun swiftc --version`、`hdiutil help` 可用。
2. 安装 Python 3.12 或更高版本、JDK 17。Android SDK Manager 中安装 **Android SDK Platform 19**、**Build-Tools 35.0.0**、**Platform-Tools**。如使用命令行 SDK Manager，可用 `sdkmanager "platforms;android-19" "build-tools;35.0.0" "platform-tools"`。从 Android 官方来源取得 SDK 并接受其许可。[Android 工具](https://developer.android.com/tools)
3. 打开终端，进入此 Git 工作副本。打包脚本只复制 Git 索引中的 Web 和头像资源；应当使用完整 clone。以下路径适用于标准 SDK 布局：

   ```sh
   export JAVA_HOME="$(/usr/libexec/java_home -v 17)"
   export ANDROID_HOME="$HOME/Library/Android/sdk"
   python3 --version
   python3 -m venv .venv-build
   .venv-build/bin/python -m pip install -r macos/requirements-build.txt
   ```

   Python 版本过低时，用已安装的 python3.12 或更新版本创建 venv。如果用 uv，也可运行 `uv venv --python 3.12 .venv-build`，再运行 `uv pip install --python .venv-build/bin/python -r macos/requirements-build.txt`。Android 非标准布局可通过 `ANDROID_BUILD_TOOLS` 指向含 aapt、apksigner、zipalign 的目录，`ANDROID_JAR` 指向 API 19 的 android.jar。

## 生成与检验

```sh
./macos/build-app.sh
```

脚本编译 Java Bridge、校验 API 19 签名、冻结 Python Host、编译 Swift 启动窗口，嵌入同一份 APK，完成本地 ad-hoc 签名，制作并校验 DMG。可以在不连接音箱的情况下打包。产物在当前仓库的 `dist/`，Apple Silicon 对应 `Rokid-Allen-Unbricked-0.3.2-arm64-local.dmg`。同名目录内也有可直接打开的 App。拖入“应用程序”即可按 README 输入 IP。请只在自己构建或已验证来源时打开。

```sh
APP="dist/Rokid-Allen-Unbricked-0.3.2-$(uname -m)-local/Rokid-Allen-Unbricked.app"
codesign --verify --deep --strict "$APP"
"$APP/Contents/Resources/engine/rokidctl" --help
shasum -a 256 "dist/Rokid-Allen-Unbricked-0.3.2-$(uname -m)-local.dmg"
.venv-build/bin/python -m unittest discover -s app/tests -p '*_test.py'
```

已有项目签名不同会导致 Android 拒绝覆盖。自用构建会在 app/build/ 留下本地开发 keystore，只供此电脑后续同签测试，请保留。需要发布长期可升级 APK 时请自行提供 ROKID_KEYSTORE、ROKID_KEY_ALIAS、ROKID_STORE_PASSWORD、ROKID_KEY_PASSWORD 环境变量；通过安全输入或钥匙串取值，勿将值写入仓库、命令历史或聊天。这四项由 Android 构建脚本读取。

## 通知播报读取器与辅助功能权限

v0.3.0 起控制台提供「设置 → 通知播报」：接收端、过滤去重、播报、圆形屏幕卡片以及 macOS 辅助功能（AX）通知读取器均已随主 `.app` 一起提供，默认全部关闭，本机不保存真实通知正文。

- **权限归属**：AX 读取器直接编入 `Rokid-Allen-Unbricked.app` 主进程，应用启动时自动运行并在退出时随之停止，不拉起外部独立读取器进程。
- **首次启用步骤**：
  1. 启动 `Rokid-Allen-Unbricked.app` 并开启控制台（若尚未授予辅助功能权限，读取器会优雅降级并在控制台上报未授权状态）。
  2. 打开「系统设置 → 隐私与安全性 → 辅助功能」，点击 `+` 添加或勾选 `Rokid-Allen-Unbricked.app`。授权后约 2 秒内自动恢复连接，无需重启 App。
  3. 在控制台「设置 → 通知播报」打开总开关并按需勾选来源。
- **重打包升级提示**：本机 `build-app.sh` 使用 ad-hoc 签名，每次重新构建 `.app` 后签名哈希（cdhash）会变化；若升级后控制台显示未授权，请在「辅助功能」列表中先移除旧条目再重新勾选新 `.app`。
- **隐私与静默保证**：日志不记录任何真实通知正文；锁屏、未授权、专注模式或启动前的历史通知均保持静默。读取器与 Host 默认共用 `~/Library/Application Support/Rokid-Allen-Unbricked/notify.sock`（路径 UTF-8 长度超过 96 字节时双方一致回退到用户私有临时目录 `rokid-notify-<uid>/notify.sock`）。

## 实机与公开发行

- 用具有恢复条件的机主自有 RH01CW 完成首次授权试装。先记录机型、固件及 ADB 固化回读，再单独记录正常重启后的回连、实际显卡和自然语音。未经机主确认不触发重启、卸载异签包或原厂家居动作。
- 可选 Roon：另行安装 Node.js，把 app/roon-bridge 复制到用户数据目录下同名子目录，在其中执行 npm ci。启动后在控制台设置自己的 Core。独立猫娘插件在 app/neko-plugin/；时钟和天气的原厂系统改造均不属于基础安装。
- 普通用户从互联网下载的正式二进制还需 Apple Developer ID Application 签名和公证。使用自己的开发者证书由内而外签署动态库、PyInstaller helper、主程序与 App（Hardened Runtime、时间戳），重制并签名 DMG，使用 notarytool 提交，成功后 staple 并检查 Gatekeeper。在另一台干净 Mac 从实际下载的 DMG 验证。当前脚本交付本机 ad-hoc 构建；公开仓库以源码起步。Intel、最低支持系统和额外固件需要分别实测再声明支持。[Apple Developer ID](https://developer.apple.com/developer-id/) · [公证指南](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)
