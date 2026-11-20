import AppKit
import Foundation

final class SetupWindow: NSObject, NSApplicationDelegate, NSTextFieldDelegate {
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 530, height: 300),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
    let ip = NSTextField()
    let guardButton = NSButton(title: "连接并固化 ADB", target: nil, action: nil)
    let runButton = NSButton(title: "安装 Bridge 并打开控制台", target: nil, action: nil)
    let adbButton = NSButton(title: "选择 adb…", target: nil, action: nil)
    let message = NSTextField(wrappingLabelWithString: "请使用可信家庭局域网。音箱需已开启网络 ADB。固化会在目标设备上保存调试端口 5555。")
    var verifiedIP: String?
    var child: Process?

    func applicationDidFinishLaunching(_ notification: Notification) {
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
        [title, ip, buttons, adbButton, message].forEach { group.addArrangedSubview($0) }
        NSLayoutConstraint.activate([
            group.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 24),
            group.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24),
            group.topAnchor.constraint(equalTo: content.topAnchor, constant: 22),
            message.widthAnchor.constraint(equalTo: group.widthAnchor),
            ip.widthAnchor.constraint(equalTo: group.widthAnchor)])
        guardButton.target = self; guardButton.action = #selector(guardADB)
        runButton.target = self; runButton.action = #selector(startBridge)
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

    @objc func selectADB() {
        let panel = NSOpenPanel()
        panel.message = "选择 Android SDK Platform-Tools 的 adb 可执行文件"
        if panel.runModal() == .OK, let url = panel.url, url.lastPathComponent == "adb",
           FileManager.default.isExecutableFile(atPath: url.path) {
            UserDefaults.standard.set(url.path, forKey: "adbPath")
            verifiedIP = nil
            runButton.isEnabled = false
            message.stringValue = "已选择 adb。请连接并校验设备。"
        }
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
        if let child = child, child.isRunning { child.terminate(); child.waitUntilExit() }
    }
}

let app = NSApplication.shared
let delegate = SetupWindow()
app.setActivationPolicy(.regular)
app.delegate = delegate
app.run()
