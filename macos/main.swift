import AppKit
import Foundation

final class SetupWindow: NSObject, NSApplicationDelegate, NSTextFieldDelegate {
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 550, height: 340),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
    let ip = NSTextField()
    let guardButton = NSButton(title: "连接并固化 ADB", target: nil, action: nil)
    let runButton = NSButton(title: "安装 Bridge 并打开控制台", target: nil, action: nil)
    let downloadButton = NSButton(title: "下载 Mac 版 Platform-Tools", target: nil, action: nil)
    let adbButton = NSButton(title: "选择 adb…", target: nil, action: nil)
    let message = NSTextField(wrappingLabelWithString: "请使用可信家庭局域网。音箱需已开启网络 ADB。固化会在目标设备上保存调试端口 5555。")
    var verifiedIP: String?
    var child: Process?
    var notifyReader: NotifyReader?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let reader = NotifyReader()
        reader.start()
        notifyReader = reader
        window.title = "Rokid-Allen-Unbricked"
        let group = NSStackView()
        group.orientation = .vertical
        group.alignment = .leading
        group.spacing = 14
        group.translatesAutoresizingMaskIntoConstraints = false
        let content = window.contentView!
        content.addSubview(group)
        let title = NSTextField(labelWithString: "输入若琪的 IP 地址")
        title.font = .boldSystemFont(ofSize: 20)
        ip.placeholderString = "音箱的局域网 IPv4 地址"
        ip.delegate = self
        ip.stringValue = UserDefaults.standard.string(forKey: "deviceIP") ?? ""
        let buttons = NSStackView(views: [guardButton, runButton])
        buttons.spacing = 8
        let adbButtons = NSStackView(views: [downloadButton, adbButton])
        adbButtons.spacing = 8
        let adbHint = NSTextField(wrappingLabelWithString:
            "首次使用：在官方页面选「Download SDK Platform-Tools for Mac」。解压后点「选择 adb…」，选 platform-tools 文件夹或里面的 adb 文件。")
        [title, ip, buttons, adbButtons, adbHint, message].forEach { group.addArrangedSubview($0) }
        NSLayoutConstraint.activate([
            group.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 24),
            group.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24),
            group.topAnchor.constraint(equalTo: content.topAnchor, constant: 22),
            adbHint.widthAnchor.constraint(equalTo: group.widthAnchor),
            message.widthAnchor.constraint(equalTo: group.widthAnchor),
            ip.widthAnchor.constraint(equalTo: group.widthAnchor)])
        guardButton.target = self; guardButton.action = #selector(guardADB)
        runButton.target = self; runButton.action = #selector(startBridge)
        downloadButton.target = self; downloadButton.action = #selector(downloadADB)
        adbButton.target = self; adbButton.action = #selector(selectADB)
        runButton.isEnabled = false
        window.center()
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(ip)
        NSApp.activate(ignoringOtherApps: true)
    }

    func controlTextDidChange(_ obj: Notification) {
        verifiedIP = nil
        runButton.isEnabled = false
    }

    @objc func downloadADB() {
        if let url = URL(string: "https://developer.android.com/tools/releases/platform-tools#downloads") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc func selectADB() {
        let panel = NSOpenPanel()
        panel.message = "选解压后的 platform-tools 文件夹，或里面的 adb 文件。"
        panel.canChooseDirectories = true
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let isDirectory = (try? url.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true
        let executable = isDirectory ? url.appendingPathComponent("adb") : url
        guard executable.lastPathComponent == "adb",
              (try? executable.resourceValues(forKeys: [.isRegularFileKey]))?.isRegularFile == true,
              FileManager.default.isExecutableFile(atPath: executable.path) else {
            message.stringValue = "这里没有可运行的 adb。请先解压 Mac 版 ZIP，再选择其中的 platform-tools 文件夹。"
            return
        }
        UserDefaults.standard.set(executable.path, forKey: "adbPath")
        verifiedIP = nil
        runButton.isEnabled = false
        message.stringValue = "已选择 adb。请输入若琪 IP，再连接并固化 ADB。"
    }
    @objc func guardADB() { launch("guard") }
    @objc func startBridge() { if verifiedIP == ip.stringValue { launch("run") } }

    func launch(_ action: String) {
        guard child == nil, let root = Bundle.main.resourceURL else { return }
        let targetIP = ip.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        ip.stringValue = targetIP
        let process = Process()
        process.executableURL = root.appendingPathComponent("engine/rokidctl")
        process.arguments = [action, "--ip", targetIP]
        var env = ProcessInfo.processInfo.environment
        if let adb = UserDefaults.standard.string(forKey: "adbPath") { env["ROKID_ADB"] = adb }
        env["ROKID_NOTIFY_EMBEDDED"] = "1"
        if let exe = Bundle.main.executablePath { env["ROKID_ICON_HELPER"] = exe }
        env["PATH"] = NSHomeDirectory() + "/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        process.environment = env
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        ip.isEnabled = false; guardButton.isEnabled = false; runButton.isEnabled = false; adbButton.isEnabled = false
        message.stringValue = action == "guard" ? "正在检查机型和 ADB 持久属性…" : "正在核对安装包、配对并启动控制台…"
        // Only a few status lines are emitted. Stream them while the supervisor is alive.
        output.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            let value = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            DispatchQueue.main.async { self.message.stringValue = value }
        }
        process.terminationHandler = { finished in
            DispatchQueue.main.async {
                self.child = nil
                self.ip.isEnabled = true; self.guardButton.isEnabled = true; self.adbButton.isEnabled = true
                if action == "guard" && finished.terminationStatus == 0 {
                    self.verifiedIP = targetIP
                    UserDefaults.standard.set(targetIP, forKey: "deviceIP")
                } else if action == "guard" { self.verifiedIP = nil }
                self.runButton.isEnabled = self.verifiedIP == targetIP
            }
        }
        do { try process.run(); child = process }
        catch {
            message.stringValue = error.localizedDescription
            ip.isEnabled = true; guardButton.isEnabled = true; adbButton.isEnabled = true
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) {
        notifyReader?.stop()
        notifyReader = nil
        if let child = child, child.isRunning { child.terminate(); child.waitUntilExit() }
    }
}

let cliArgs = Array(CommandLine.arguments.dropFirst())
if let subcommand = cliArgs.first {
    switch subcommand {
    case "extract-icon":
        exit(NotifyReader.extractIcon(arguments: Array(cliArgs.dropFirst())))
    case "notify-check":
        let info = NotifyReader.envInfo()
        if let data = try? JSONSerialization.data(withJSONObject: info, options: [.sortedKeys]),
           let line = String(data: data, encoding: .utf8) {
            print(line)
        }
        exit(0)
    case "notify-reader":
        var sockArg: String?
        var duration: Double = 0
        var idx = 1
        while idx < cliArgs.count {
            let a = cliArgs[idx]
            let v = idx + 1 < cliArgs.count ? cliArgs[idx + 1] : ""
            if a == "--socket" { sockArg = v; idx += 1 }
            else if a == "--seconds" { duration = Double(v) ?? 0; idx += 1 }
            idx += 1
        }
        let reader = NotifyReader(socketPath: sockArg)
        reader.start()
        signal(SIGTERM, SIG_IGN)
        signal(SIGINT, SIG_IGN)
        let sigTerm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        sigTerm.setEventHandler {
            reader.stop()
            exit(0)
        }
        sigTerm.resume()
        let sigInt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        sigInt.setEventHandler {
            reader.stop()
            exit(0)
        }
        sigInt.resume()
        if duration > 0 {
            Timer.scheduledTimer(withTimeInterval: duration, repeats: false) { _ in
                reader.stop()
                exit(0)
            }
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        app.run()
        exit(0)
    default:
        break
    }
}

let app = NSApplication.shared
let delegate = SetupWindow()
app.setActivationPolicy(.regular)
app.delegate = delegate
app.run()
