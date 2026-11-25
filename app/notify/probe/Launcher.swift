// Launcher.swift — RokidNotifyProbe.app 的稳定授权宿主（开发工具）
//
// ad-hoc 签名的 App 在 TCC 中按 cdhash 识别，每次重编译都要重新授权。
// 所以授权只给这个几乎不变的启动器：它把参数原样转给同级目录下的
// notify-probe-cli，子进程继承本 App 的「责任进程」身份与辅助功能授权，
// 探针逻辑可随意重编译而无需机主重复授权。只启动固定路径，不接受任意命令。
import Foundation

let exe = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
// …/notify-probe/RokidNotifyProbe.app/Contents/MacOS/RokidNotifyProbe
let appDir = exe.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
let cli = appDir.deletingLastPathComponent().appendingPathComponent("notify-probe-cli")

let p = Process()
p.executableURL = cli
p.arguments = Array(CommandLine.arguments.dropFirst())
do {
    try p.run()
    p.waitUntilExit()
    exit(p.terminationStatus)
} catch {
    fputs("RokidNotifyProbe: cannot start \(cli.path): \(error)\n", stderr)
    exit(2)
}
