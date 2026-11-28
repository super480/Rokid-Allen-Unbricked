// NotifyReader.swift — Rokid-Allen-Unbricked 主进程内置 Mac 通知 AX 读取器
//
// 直接编入 Rokid-Allen-Unbricked.app 主可执行文件，使 macOS 辅助功能（Accessibility）
// 权限归属主 .app 进程本身，避免 fork/exec 外部 ad-hoc 二进制导致 AXIsProcessTrusted 返回 false。
//
// 隐私与安全约束：
//   - 仅读取 com.apple.notificationcenterui 的 AX 树，不发声、不点击、不修改通知；
//   - 不在日志中输出任何真实通知标题或正文（100% 脱敏，等价于 --marker ""）；
//   - 仅通过当前用户私有目录（0700/0600）Unix socket 向本机 Host 发送事件与心跳；
//   - 权限未给或被撤销时优雅降级上报 trusted: false，获权后自动恢复；通知中心重启自动重连。

import AppKit
import ApplicationServices
import CryptoKit
import Foundation

final class NotifyReader {
    private struct Banner {
        let sig: String
        let relayPayload: [String: Any]
    }

    private static let textIDs: Set<String> = ["title", "subtitle", "body", "appName", "date"]
    private static let watchedNotifications: [String] = [
        kAXWindowCreatedNotification,
        kAXUIElementDestroyedNotification,
        kAXCreatedNotification,
        kAXLayoutChangedNotification,
        kAXValueChangedNotification,
        kAXTitleChangedNotification,
        kAXFocusedWindowChangedNotification,
        kAXFocusedUIElementChangedNotification,
        kAXWindowMovedNotification,
        kAXWindowResizedNotification,
        "AXAnnouncementRequested",
    ]

    let socketPath: String
    private var running = false
    private var trustedNow = false
    private var observer: AXObserver?
    private var appEl: AXUIElement?
    private var attachedPid: pid_t = -1
    private var baselined = false
    private var current: [String: Banner] = [:]
    private var lastScan = Date.distantPast
    private var scanGen = 0
    private var lastHeartbeat = Date.distantPast
    private var lastSendOK = false
    private var pollTimer: Timer?
    private var workspaceObservers: [NSObjectProtocol] = []

    init(socketPath: String? = nil) {
        if let explicit = socketPath, !explicit.isEmpty {
            self.socketPath = explicit
        } else {
            self.socketPath = Self.resolveDefaultSocketPath()
        }
    }

    deinit {
        stop()
    }

    /// 与 app/host/notify_relay.py 的 default_socket_path() 保持一致（含 96 字节上限回退逻辑）。
    static func resolveDefaultSocketPath() -> String {
        if let envPath = ProcessInfo.processInfo.environment["ROKID_NOTIFY_SOCKET"], !envPath.isEmpty {
            return envPath
        }
        let home = NSHomeDirectory()
        let candidate = (home as NSString)
            .appendingPathComponent("Library/Application Support/Rokid-Allen-Unbricked/notify.sock")
        if candidate.utf8.count <= 96 {
            return candidate
        }
        let tmpDir = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
        return tmpDir
            .appendingPathComponent("rokid-notify-\(getuid())", isDirectory: true)
            .appendingPathComponent("notify.sock")
            .path
    }

    static func screenLocked() -> Any {
        guard let dict = CGSessionCopyCurrentDictionary() as? [String: Any] else { return NSNull() }
        return (dict["CGSSessionScreenIsLocked"] as? Bool) ?? false
    }

    static func ncApp() -> NSRunningApplication? {
        NSWorkspace.shared.runningApplications.first {
            $0.bundleIdentifier == "com.apple.notificationcenterui"
        }
    }

    static func envInfo(socketPath: String = resolveDefaultSocketPath()) -> [String: Any] {
        [
            "trusted": AXIsProcessTrusted(),
            "macOS": ProcessInfo.processInfo.operatingSystemVersionString,
            "pid": Int(getpid()),
            "ppid": Int(getppid()),
            "ncPid": ncApp().map { Int($0.processIdentifier) } ?? -1,
            "locked": screenLocked(),
            "socketPath": socketPath,
        ]
    }

    /// 供主进程子命令 `extract-icon --app <AppPath> --out <PngPath>` 调用（无需 AX 权限）。
    static func extractIcon(arguments: [String]) -> Int32 {
        var appPath: String?
        var outPath: String?
        var i = 0
        while i < arguments.count {
            let a = arguments[i]
            let v = i + 1 < arguments.count ? arguments[i + 1] : ""
            switch a {
            case "--app":
                appPath = v
                i += 1
            case "--out":
                outPath = v
                i += 1
            default:
                break
            }
            i += 1
        }
        guard let src = appPath, !src.isEmpty, let dst = outPath, !dst.isEmpty else {
            fputs("usage: RokidAllen extract-icon --app /Applications/Foo.app --out /path/to/icon.png\n", stderr)
            return 2
        }
        let img = NSWorkspace.shared.icon(forFile: src)
        let size = NSSize(width: 128, height: 128)
        let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil,
            pixelsWide: 128,
            pixelsHigh: 128,
            bitsPerSample: 8,
            samplesPerPixel: 4,
            hasAlpha: true,
            isPlanar: false,
            colorSpaceName: .deviceRGB,
            bytesPerRow: 0,
            bitsPerPixel: 0
        )
        guard let r = rep, let ctx = NSGraphicsContext(bitmapImageRep: r) else { return 3 }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = ctx
        ctx.imageInterpolation = .high
        img.draw(in: NSRect(origin: .zero, size: size), from: .zero, operation: .copy, fraction: 1.0)
        NSGraphicsContext.restoreGraphicsState()
        guard let png = r.representation(using: .png, properties: [:]) else { return 4 }
        let url = URL(fileURLWithPath: dst)
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        do {
            try png.write(to: url)
            print("ok \(png.count)")
            return 0
        } catch {
            fputs("write failed: \(error)\n", stderr)
            return 5
        }
    }

    // MARK: - 生命周期

    func start() {
        guard !running else { return }
        running = true
        trustedNow = AXIsProcessTrusted()
        lastSendOK = sendHeartbeat(type: "heartbeat")
        lastHeartbeat = Date()
        if trustedNow {
            attach()
        }

        let wsCenter = NSWorkspace.shared.notificationCenter
        let termObs = wsCenter.addObserver(
            forName: NSWorkspace.didTerminateApplicationNotification,
            object: nil,
            queue: .main
        ) { [weak self] note in
            guard let self = self, self.running else { return }
            if let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
               app.bundleIdentifier == "com.apple.notificationcenterui" {
                self.detach()
            }
        }
        let launchObs = wsCenter.addObserver(
            forName: NSWorkspace.didLaunchApplicationNotification,
            object: nil,
            queue: .main
        ) { [weak self] note in
            guard let self = self, self.running else { return }
            if let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
               app.bundleIdentifier == "com.apple.notificationcenterui" {
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                    guard let self = self, self.running else { return }
                    self.attach()
                }
            }
        }
        workspaceObservers = [termObs, launchObs]

        // 2 秒定时巡检：权限变化恢复/降级、通知中心重启重连、以及 10 秒周期心跳（未连上 Host 时 2 秒快速重试）。
        let timer = Timer(timeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.tick()
        }
        RunLoop.main.add(timer, forMode: .common)
        pollTimer = timer
    }

    func stop() {
        guard running else { return }
        running = false
        pollTimer?.invalidate()
        pollTimer = nil
        let wsCenter = NSWorkspace.shared.notificationCenter
        for obs in workspaceObservers {
            wsCenter.removeObserver(obs)
        }
        workspaceObservers.removeAll()
        detach()
        current.removeAll()
        baselined = false
    }

    private func tick() {
        guard running else { return }
        let trusted = AXIsProcessTrusted()
        if trusted != trustedNow {
            trustedNow = trusted
            lastSendOK = sendHeartbeat(type: trusted ? "trust_granted" : "trust_lost")
            lastHeartbeat = Date()
            if trusted {
                attach()
            } else {
                detach()
            }
            return
        }
        if trusted, let nc = Self.ncApp(), nc.processIdentifier != attachedPid {
            attach()
        }
        let interval: TimeInterval = lastSendOK ? 10.0 : 2.0
        if Date().timeIntervalSince(lastHeartbeat) >= interval {
            lastHeartbeat = Date()
            lastSendOK = sendHeartbeat(type: "heartbeat")
        }
    }

    @discardableResult
    private func sendHeartbeat(type: String) -> Bool {
        sendSocket([
            "type": type,
            "trusted": trustedNow,
            "locked": Self.screenLocked(),
            "pid": Int(getpid()),
            "macOS": ProcessInfo.processInfo.operatingSystemVersionString,
        ])
    }

    // MARK: - Unix Socket 通信

    @discardableResult
    private func sendSocket(_ fields: [String: Any]) -> Bool {
        guard !socketPath.isEmpty else { return false }
        guard JSONSerialization.isValidJSONObject(fields),
              var data = try? JSONSerialization.data(withJSONObject: fields, options: [])
        else { return false }
        data.append(0x0A)
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var noSigPipe: Int32 = 1
        _ = setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(socketPath.utf8)
        let maxLen = MemoryLayout.size(ofValue: addr.sun_path)
        guard pathBytes.count < maxLen else { return false }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            for (i, b) in pathBytes.enumerated() { raw[i] = b }
            raw[pathBytes.count] = 0
        }
        let len = socklen_t(MemoryLayout<sa_family_t>.size + pathBytes.count + 1)
        let rc = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, len) }
        }
        guard rc == 0 else { return false }
        return data.withUnsafeBytes { ptr in
            guard let base = ptr.baseAddress else { return false }
            return write(fd, base, data.count) == data.count
        }
    }

    // MARK: - AXObserver 挂载与扫描

    private static let axCallback: AXObserverCallbackWithInfo = { _, element, notif, _, refcon in
        guard let refcon = refcon else { return }
        let reader = Unmanaged<NotifyReader>.fromOpaque(refcon).takeUnretainedValue()
        reader.handleAXEvent(element: element, notification: notif as String)
    }

    private func handleAXEvent(element: AXUIElement, notification: String) {
        guard running else { return }
        if notification == (kAXWindowCreatedNotification as String), let obs = observer {
            let refcon = Unmanaged.passUnretained(self).toOpaque()
            for n in [kAXUIElementDestroyedNotification, kAXLayoutChangedNotification, kAXCreatedNotification] {
                AXObserverAddNotification(obs, element, n as CFString, refcon)
            }
        }
        scheduleScan()
    }

    private func detach() {
        if let obs = observer {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(obs), .defaultMode)
        }
        observer = nil
        appEl = nil
        attachedPid = -1
        baselined = false
        current.removeAll()
    }

    private func attach() {
        guard running, trustedNow, let nc = Self.ncApp() else { return }
        let pid = nc.processIdentifier
        if pid == attachedPid { return }
        detach()
        var obs: AXObserver?
        let err = AXObserverCreateWithInfoCallback(pid, Self.axCallback, &obs)
        guard err == .success, let o = obs else { return }
        let el = AXUIElementCreateApplication(pid)
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        for n in Self.watchedNotifications {
            AXObserverAddNotification(o, el, n as CFString, refcon)
        }
        for w in (axAttr(el, kAXWindowsAttribute as String) as? [AXUIElement]) ?? [] {
            for n in [kAXUIElementDestroyedNotification, kAXLayoutChangedNotification, kAXCreatedNotification] {
                AXObserverAddNotification(o, w, n as CFString, refcon)
            }
        }
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(o), .defaultMode)
        observer = o
        appEl = el
        attachedPid = pid
        baselined = false
        scan()
    }

    private func scheduleScan() {
        guard running else { return }
        scanGen += 1
        let g = scanGen
        if Date().timeIntervalSince(lastScan) > 0.6 {
            scan()
        } else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak self] in
                guard let self = self, self.running, g == self.scanGen else { return }
                self.scan()
            }
        }
        // 横幅文字可能晚于窗口创建才填充：1 秒后补扫一次。
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            guard let self = self, self.running else { return }
            self.scan()
        }
    }

    private func scan() {
        guard running, let root = appEl else { return }
        let list = findBanners(root)
        lastScan = Date()
        var next: [String: Banner] = [:]
        for b in list { next[b.sig] = b }
        if !baselined {
            baselined = true
            current = next
            return
        }
        let added = next.keys.filter { current[$0] == nil }
        let removed = current.keys.filter { next[$0] == nil }
        if added.isEmpty && removed.isEmpty { return }
        for k in added {
            if let b = next[k] {
                _ = sendSocket(b.relayPayload.merging(["type": "banner_added"]) { a, _ in a })
            }
        }
        for k in removed {
            if let b = current[k] {
                _ = sendSocket(b.relayPayload.merging(["type": "banner_removed"]) { a, _ in a })
            }
        }
        current = next
    }

    // MARK: - AX 树读取与横幅解析（与 NotifyProbe.swift 保持一致，零真实内容落盘）

    private func axAttr(_ e: AXUIElement, _ n: String) -> CFTypeRef? {
        var v: CFTypeRef?
        return AXUIElementCopyAttributeValue(e, n as CFString, &v) == .success ? v : nil
    }

    private func axStr(_ e: AXUIElement, _ n: String) -> String? {
        guard let v = axAttr(e, n) else { return nil }
        if let s = v as? String { return s }
        if let a = v as? NSAttributedString { return a.string }
        return nil
    }

    private func axKids(_ e: AXUIElement) -> [AXUIElement] {
        (axAttr(e, kAXChildrenAttribute as String) as? [AXUIElement]) ?? []
    }

    private func sha(_ s: String) -> String {
        SHA256.hash(data: Data(s.utf8)).prefix(5).map { String(format: "%02x", $0) }.joined()
    }

    private func collectTexts(_ e: AXUIElement, depth: Int, into out: inout [(id: String?, raw: String)]) {
        if depth > 6 || out.count > 24 { return }
        for k in axKids(e) {
            let role = axStr(k, "AXRole")
            if role == "AXStaticText" || role == "AXTextField" || role == "AXTextArea" {
                let raw = axStr(k, "AXValue") ?? axStr(k, "AXDescription") ?? axStr(k, "AXTitle") ?? ""
                out.append((axStr(k, "AXIdentifier"), raw))
            }
            collectTexts(k, depth: depth + 1, into: &out)
        }
    }

    private func candidateRule(_ e: AXUIElement) -> String? {
        let ident = axStr(e, "AXIdentifier") ?? ""
        if ident == "AXNotificationListItems" { return nil }
        let sr = axStr(e, "AXSubrole") ?? ""
        if sr.range(of: "notification", options: .caseInsensitive) != nil { return "subrole" }
        if ident.range(of: "notification", options: .caseInsensitive) != nil { return "identifier" }
        if axStr(e, "AXRole") == "AXGroup" {
            for k in axKids(e) where Self.textIDs.contains(axStr(k, "AXIdentifier") ?? "") {
                return "text-ids"
            }
        }
        return nil
    }

    private func makeBanner(_ e: AXUIElement, rule: String) -> Banner {
        var texts: [(id: String?, raw: String)] = []
        collectTexts(e, depth: 0, into: &texts)
        let desc = axStr(e, "AXDescription") ?? ""
        let title = axStr(e, "AXTitle") ?? ""
        let rawAll = ([desc, title] + texts.map { $0.raw }).joined(separator: "\u{1F}")
        let parts = desc.components(separatedBy: ", ")
        let firstTitle = texts.first(where: { $0.id == "title" })?.raw ?? ""
        let subText = texts.first(where: { $0.id == "subtitle" })?.raw ?? ""
        let bodyText = texts.first(where: { $0.id == "body" })?.raw ?? ""
        let anchor = !firstTitle.isEmpty ? firstTitle : (!bodyText.isEmpty ? bodyText : subText)
        var extractedSource: String?
        if !desc.isEmpty && !anchor.isEmpty, let r = desc.range(of: anchor), r.lowerBound > desc.startIndex {
            let prefix = String(desc[..<r.lowerBound])
                .trimmingCharacters(in: CharacterSet(charactersIn: " \t\n，,：:"))
            if !prefix.isEmpty && prefix.count <= 32 {
                extractedSource = prefix
            }
        }
        if extractedSource == nil, let p0 = parts.first, !p0.isEmpty, firstTitle.isEmpty {
            if let sp = p0.firstIndex(where: { $0 == " " || $0 == "，" }) {
                extractedSource = String(p0[..<sp])
            }
        }
        let relayPayload: [String: Any] = [
            "source_name": extractedSource ?? "",
            "event_id": axStr(e, "AXIdentifier") ?? "",
            "title": firstTitle,
            "subtitle": subText,
            "body": bodyText,
            "locked": Self.screenLocked(),
            "trusted": AXIsProcessTrusted(),
        ]
        return Banner(sig: sha(rawAll + "|" + rule), relayPayload: relayPayload)
    }

    private func findBanners(_ root: AXUIElement) -> [Banner] {
        var res: [Banner] = []
        var nodes = 0
        func walk(_ e: AXUIElement, _ depth: Int) {
            nodes += 1
            if nodes > 800 || depth > 16 { return }
            if let rule = candidateRule(e) {
                res.append(makeBanner(e, rule: rule))
            }
            for k in axKids(e) {
                walk(k, depth + 1)
            }
        }
        walk(root, 0)
        return res
    }
}
