# 让 agent 安装公开源码版

可以直接复制给本机的 Codex 或 WorkBuddy：

> 请从 https://github.com/super480/Rokid-Allen-Unbricked 获取公开源码，阅读 README.md、AGENTS.md 和 docs/PACKAGING.md，帮我在 Mac 构建本地 App。用我提供的音箱 IP 核对设备，先执行 ADB 固化并回读，再安装 Bridge、打开控制台，最后请我在若琪上验证系统资源卡。遇到异签、未授权、需重启或缺少恢复通道时告诉我具体原因。请在本机控制台输入账号，不要把凭据写入聊天。

1. 在使用者的 Mac 上按打包指南准备工具，先完成本地构建和签名校验。ADB 由用户安装或选择。保留构建目录内的本地 Android 签名以便以后同签升级。
2. 使用 App 的 IP 输入框，或使用相同的 CLI。CLI 在生成的 App 的 `Contents/Resources/engine/rokidctl`。将下面两个变量替换为用户实际选取的值：

   ```sh
   CTL="/Applications/Rokid-Allen-Unbricked.app/Contents/Resources/engine/rokidctl"
   read -r "DEVICE?若琪局域网 IPv4："
   "$CTL" guard --ip "$DEVICE" && "$CTL" run --ip "$DEVICE"
   ```

   以上交互输入适用于 macOS 默认 zsh。若 adb 不在可发现位置，可设置环境变量 ROKID_ADB 为其完整路径。运行命令会持有前台服务，Ctrl-C 停止。
3. 输出中分别记录型号兼容、ADB 持久端口回读、是否已观察跨重启、APK 安装／配对、控制台可达。让机主确认实体屏幕显示卡片。只有机主实际完成重启和回连才报告跨重启实测。
4. ADB 不可连接时检查音箱 IP、供电、调试授权和网络隔离。固化不能写入时报告固件／权限条件。同包异签时保留原包；安装器没有自动卸载或降级操作。端口 18765 被其他实例占用时先核实归属，仅退出用户确认的实例。
