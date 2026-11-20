# Codex 与 WorkBuddy 协作说明

公开基线来自 v0.2.7（594be2e）。原项目工作区和会话由机主留在本地，继续工作时将此目录作为目标。两种开发 agent 均可修改 app/；编辑前查看 git status 并重读共享文件，同一文件避免并行写，勿改写历史。用户认定里程碑后由版本维护者更新版本、标签和发布说明。

- app/host/ 为 Python 主机、助手适配、HA 和小应用；web/console.html 是控制台。app/android/ 为 API 19 设备端和 Canvas 卡片；app/clock/ 是单独的待机时钟。design/avatars/ 只留控制台使用的角色图片。
- macos/main.swift 提供 IP 向导；macos/rokidctl.py 负责设备校验、持久 ADB、配对与服务生命周期；macos/build-app.sh 生成本地 App 和 DMG。
- 首次设备写入顺序必须经过机型与 ADB 检查、持久端口回读，再进入 Bridge 部署。安装器持有用户级设备锁。调试时避免与旧项目共用音箱；在未获机主授权时用模拟设备，HA 验证使用模拟数据。
- 凭据通过本地控制台或运行环境提供。保持 Android 资源白名单；公开提交及发版前检查暂存内容、APK、App。禁止提交会话、账号、私钥、签名文件、原厂制品及私人诊断。
- 使用 Python 3.12+；运行 `python -m unittest discover -s app/tests -p '*_test.py'`。设置 JAVA_HOME 可同时运行设备端 Java 规则测试。打包和真机验收各自留证；区分模拟结果、属性回读和真实跨重启验证。
