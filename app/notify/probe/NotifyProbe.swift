// NotifyProbe.swift — Rokid-Allen P2-12「Mac 通知播报」静音 AX 探针（开发工具）
//
// 只读 NotificationCenter 进程的辅助功能（AX）树，观察通知横幅的新增/移除。
// 不发声、不点击、不修改任何通知。输出 JSONL：
//   * 文本一律脱敏为 <redacted len=N>，除非包含合成标记（默认 ROKIDTEST）
//     或属于来源显示名 / 系统按钮白名单——满足「不保存真实标题、正文、整棵 AX 树」。
//
// 用法（必须由已获辅助功能授权的进程启动，见 README.md）：
//   notify-probe-cli check [--prompt]
//   notify-probe-cli dump  [--out FILE]
//   notify-probe-cli observe [--seconds 120] [--poll-ms 0] [--marker ROKIDTEST] [--out FILE]
import AppKit
import ApplicationServices
import CryptoKit
import EventKit
import Foundation

setvbuf(stdout, nil, _IOLBF, 0)

// MARK: - 参数

var mode = "check"
var seconds: Double = 120
var marker = "ROKIDTEST"
var pollMs = 0
var outPath: String? = nil
var socketPath: String? = nil
var wantPrompt = false
var inSeconds: Double = 60
var sampleTitle = "取快递"
var appPathArg: String? = nil

do {
    var args = Array(CommandLine.arguments.dropFirst())
    if let first = args.first, !first.hasPrefix("-") { mode = first; args.removeFirst() }
    var i = 0
    while i < args.count {
        let a = args[i]
        let v = i + 1 < args.count ? args[i + 1] : ""
        switch a {
        case "--seconds": seconds = Double(v) ?? seconds; i += 1
        case "--marker": marker = v; i += 1
        case "--poll-ms": pollMs = Int(v) ?? 0; i += 1
        case "--out": outPath = v; i += 1
        case "--socket": socketPath = v; i += 1
        case "--prompt": wantPrompt = true
        case "--in": inSeconds = Double(v) ?? inSeconds; i += 1
        case "--title": sampleTitle = v; i += 1
        case "--app": appPathArg = v; i += 1
        default: fputs("unknown arg \(a)\n", stderr)
        }
        i += 1
    }
}

// MARK: - 输出

let iso: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f
}()
var outHandle: FileHandle? = nil
if let p = outPath {
    let url = URL(fileURLWithPath: p)
    try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    if !FileManager.default.fileExists(atPath: p) { FileManager.default.createFile(atPath: p, contents: nil) }
    outHandle = FileHandle(forWritingAtPath: p)
    outHandle?.seekToEndOfFile()
}

func emit(_ type: String, _ fields: [String: Any] = [:]) {
    var d = fields
    d["type"] = type
    d["ts"] = iso.string(from: Date())
    guard JSONSerialization.isValidJSONObject(d),
          var line = try? JSONSerialization.data(withJSONObject: d, options: [.sortedKeys])
    else {
        FileHandle.standardOutput.write("{\"type\":\"emit_error\",\"of\":\"\(type)\"}\n".data(using: .utf8)!)
        return
    }
    line.append(0x0A)
    FileHandle.standardOutput.write(line)
    outHandle?.write(line)
}

func sendSocket(_ fields: [String: Any]) {
    guard let sp = socketPath, !sp.isEmpty else { return }
    guard JSONSerialization.isValidJSONObject(fields),
          var data = try? JSONSerialization.data(withJSONObject: fields, options: [])
    else { return }
    data.append(0x0A)
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { return }
    defer { close(fd) }
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let pathBytes = Array(sp.utf8)
    let maxLen = MemoryLayout.size(ofValue: addr.sun_path)
    guard pathBytes.count < maxLen else { return }
    withUnsafeMutableBytes(of: &addr.sun_path) { raw in
        for (i, b) in pathBytes.enumerated() { raw[i] = b }
        raw[pathBytes.count] = 0
    }
    let len = socklen_t(MemoryLayout<sa_family_t>.size + pathBytes.count + 1)
    let rc = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, len) }
    }
    if rc == 0 {
        data.withUnsafeBytes { ptr in
            if let base = ptr.baseAddress { _ = write(fd, base, data.count) }
        }
    }
}

// MARK: - 脱敏

/// 可原样记录的来源显示名（App 名不是通知内容）。
let safeNames: [String] = [
    "提醒事项", "日历", "Reminders", "Calendar", "Claude", "ChatGPT", "Codex", "WorkBuddy",
    "WorkBuddy AI", "脚本编辑器", "Script Editor", "通知中心", "Notification Center",
    "Antigravity", "OpenCode", "终端", "Terminal", "Nuphos", "RokidNotifyProbe",
]
/// 系统按钮 / 结构性文案。
let safeUI: Set<String> = [
    "关闭", "选项", "显示", "清除", "全部清除", "稍后提醒", "完成", "回复", "打开", "显示更少", "显示更多",
    "Close", "Options", "Show", "Clear", "Clear All", "Snooze", "Complete", "Reply", "Open",
    "Show Less", "Show More", "通知", "Notification",
]

func red(_ s: String?) -> Any {
    guard let s = s else { return NSNull() }
    if s.isEmpty { return "" }
    if !marker.isEmpty && s.contains(marker) { return s }
    if safeNames.contains(s) || safeUI.contains(s) { return s }
    var r = "<redacted len=\(s.count)"
    if let p = safeNames.first(where: { s.hasPrefix($0 + ",") || s.hasPrefix($0 + "，") || s.hasPrefix($0 + " ") }) {
        r += " prefix=\(p)"
    }
    return r + ">"
}

func short(_ s: String?, _ n: Int = 80) -> Any {
    guard let s = s else { return NSNull() }
    return s.count > n ? String(s.prefix(n)) + "…" : s
}

func sha(_ s: String) -> String {
    SHA256.hash(data: Data(s.utf8)).prefix(5).map { String(format: "%02x", $0) }.joined()
}

// MARK: - AX 读取

func axAttr(_ e: AXUIElement, _ n: String) -> CFTypeRef? {
    var v: CFTypeRef?
    return AXUIElementCopyAttributeValue(e, n as CFString, &v) == .success ? v : nil
}

func axStr(_ e: AXUIElement, _ n: String) -> String? {
    guard let v = axAttr(e, n) else { return nil }
    if let s = v as? String { return s }
    if let a = v as? NSAttributedString { return a.string }
    return nil
}

func axKids(_ e: AXUIElement) -> [AXUIElement] {
    (axAttr(e, kAXChildrenAttribute as String) as? [AXUIElement]) ?? []
}

func axNames(_ e: AXUIElement) -> [String] {
    var a: CFArray?
    guard AXUIElementCopyAttributeNames(e, &a) == .success, let arr = a as? [String] else { return [] }
    return arr
}

func axActions(_ e: AXUIElement) -> [String] {
    var a: CFArray?
    guard AXUIElementCopyActionNames(e, &a) == .success, let arr = a as? [String] else { return [] }
    return arr.map { String($0.prefix(60)) }
}

func axFrame(_ e: AXUIElement) -> [Int]? {
    guard let pv = axAttr(e, kAXPositionAttribute as String), CFGetTypeID(pv) == AXValueGetTypeID(),
          let sv = axAttr(e, kAXSizeAttribute as String), CFGetTypeID(sv) == AXValueGetTypeID()
    else { return nil }
    var p = CGPoint.zero
    var s = CGSize.zero
    AXValueGetValue(unsafeBitCast(pv, to: AXValue.self), .cgPoint, &p)
    AXValueGetValue(unsafeBitCast(sv, to: AXValue.self), .cgSize, &s)
    return [Int(p.x), Int(p.y), Int(s.width), Int(s.height)]
}

func nodeInfo(_ e: AXUIElement, names: Bool) -> [String: Any] {
    var d: [String: Any] = [
        "role": short(axStr(e, "AXRole")),
        "subrole": short(axStr(e, "AXSubrole")),
        "id": short(axStr(e, "AXIdentifier")),
        "roleDesc": short(axStr(e, "AXRoleDescription")),
        "title": red(axStr(e, "AXTitle")),
        "desc": red(axStr(e, "AXDescription")),
        "value": red(axStr(e, "AXValue")),
        "help": red(axStr(e, "AXHelp")),
    ]
    if let f = axFrame(e) { d["frame"] = f }
    if names {
        d["attrs"] = axNames(e)
        d["actions"] = axActions(e)
    }
    return d
}

func dumpTree(_ e: AXUIElement, depth: Int, budget: inout Int) -> [String: Any] {
    var d = nodeInfo(e, names: true)
    budget -= 1
    if depth < 16 && budget > 0 {
        let kids = axKids(e)
        if !kids.isEmpty {
            var arr: [[String: Any]] = []
            for k in kids {
                if budget <= 0 { d["truncated"] = true; break }
                arr.append(dumpTree(k, depth: depth + 1, budget: &budget))
            }
            d["children"] = arr
        }
    }
    return d
}

// MARK: - 横幅候选

struct Banner {
    let sig: String
    let path: String
    let info: [String: Any]
    let markerHit: Bool
    let relayPayload: [String: Any]
}

let textIDs: Set<String> = ["title", "subtitle", "body", "appName", "date"]

func collectTexts(_ e: AXUIElement, depth: Int, into out: inout [(id: String?, role: String?, raw: String)]) {
    if depth > 6 || out.count > 24 { return }
    for k in axKids(e) {
        let role = axStr(k, "AXRole")
        if role == "AXStaticText" || role == "AXTextField" || role == "AXTextArea" {
            let raw = axStr(k, "AXValue") ?? axStr(k, "AXDescription") ?? axStr(k, "AXTitle") ?? ""
            out.append((axStr(k, "AXIdentifier"), role, raw))
        }
        collectTexts(k, depth: depth + 1, into: &out)
    }
}

func candidateRule(_ e: AXUIElement) -> String? {
    let ident = axStr(e, "AXIdentifier") ?? ""
    if ident == "AXNotificationListItems" { return nil }
    let sr = axStr(e, "AXSubrole") ?? ""
    if sr.range(of: "notification", options: .caseInsensitive) != nil { return "subrole" }
    if ident.range(of: "notification", options: .caseInsensitive) != nil { return "identifier" }
    if axStr(e, "AXRole") == "AXGroup" {
        for k in axKids(e) where textIDs.contains(axStr(k, "AXIdentifier") ?? "") { return "text-ids" }
    }
    return nil
}

func makeBanner(_ e: AXUIElement, rule: String, path: String) -> Banner {
    var texts: [(id: String?, role: String?, raw: String)] = []
    collectTexts(e, depth: 0, into: &texts)
    let desc = axStr(e, "AXDescription") ?? ""
    let title = axStr(e, "AXTitle") ?? ""
    let rawAll = ([desc, title] + texts.map { $0.raw }).joined(separator: "\u{1F}")
    var buttons: [Any] = []
    var images = 0
    func walk(_ x: AXUIElement, _ d: Int) {
        if d > 6 { return }
        for k in axKids(x) {
            let r = axStr(k, "AXRole")
            if r == "AXButton" { buttons.append(red(axStr(k, "AXDescription") ?? axStr(k, "AXTitle"))) }
            if r == "AXImage" { images += 1 }
            walk(k, d + 1)
        }
    }
    walk(e, 0)
    var info = nodeInfo(e, names: true)
    info["rule"] = rule
    info["path"] = path
    info["texts"] = texts.map { t -> [String: Any] in
        ["id": short(t.id), "role": short(t.role), "value": red(t.raw)]
    }
    info["buttons"] = buttons
    info["images"] = images
    // 描述格式包括：
    //   单条：「<来源显示名> <标题>, <正文>」
    //   叠放：「<来源显示名>，<标题>，<正文>，已叠放」（全中文逗号，descParts=1）
    // 直接取 title（或 body）在 desc 首次出现位置之前的前缀并剥离分隔符，通吃所有格式。
    let parts = desc.components(separatedBy: ", ")
    info["descParts"] = parts.count
    info["descFirst"] = red(parts.first)
    let firstTitle = texts.first(where: { $0.id == "title" })?.raw ?? ""
    let subText = texts.first(where: { $0.id == "subtitle" })?.raw ?? ""
    let bodyText = texts.first(where: { $0.id == "body" })?.raw ?? ""
    let anchor = !firstTitle.isEmpty ? firstTitle : (!bodyText.isEmpty ? bodyText : subText)
    var extractedSource: String? = nil
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
    info["sourceName"] = red(extractedSource)
    info["hasHeader"] = texts.contains { $0.id == "header" }
    info["hasSubtitle"] = texts.contains { $0.id == "subtitle" }
    info["hasBody"] = texts.contains { $0.id == "body" }
    let hit = !marker.isEmpty && rawAll.contains(marker)
    let relayPayload: [String: Any] = [
        "source_name": extractedSource ?? "",
        "event_id": axStr(e, "AXIdentifier") ?? "",
        "title": firstTitle,
        "subtitle": subText,
        "body": bodyText,
        "locked": screenLocked(),
        "trusted": AXIsProcessTrusted(),
    ]
    return Banner(sig: sha(rawAll + "|" + rule), path: path, info: info, markerHit: hit, relayPayload: relayPayload)
}

func findBanners(_ root: AXUIElement) -> (banners: [Banner], nodes: Int) {
    var res: [Banner] = []
    var nodes = 0
    func walk(_ e: AXUIElement, _ path: String, _ depth: Int) {
        nodes += 1
        if nodes > 800 || depth > 16 { return }
        if let rule = candidateRule(e) { res.append(makeBanner(e, rule: rule, path: path)) }
        for (i, k) in axKids(e).enumerated() { walk(k, path + "/\(i)", depth + 1) }
    }
    walk(root, "", 0)
    return (res, nodes)
}

// MARK: - 环境

func ncApp() -> NSRunningApplication? {
    NSWorkspace.shared.runningApplications.first { $0.bundleIdentifier == "com.apple.notificationcenterui" }
}

func screenLocked() -> Any {
    guard let d = CGSessionCopyCurrentDictionary() as? [String: Any] else { return NSNull() }
    return (d["CGSSessionScreenIsLocked"] as? Bool) ?? false
}

func envInfo() -> [String: Any] {
    [
        "trusted": AXIsProcessTrusted(),
        "macOS": ProcessInfo.processInfo.operatingSystemVersionString,
        "exe": CommandLine.arguments[0],
        "pid": Int(getpid()),
        "ppid": Int(getppid()),
        "ncPid": ncApp().map { Int($0.processIdentifier) } ?? -1,
        "locked": screenLocked(),
        "frontmost": NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? NSNull(),
    ]
}

// MARK: - 模式：check / dump

if mode == "check" {
    if wantPrompt {
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(opts)
    }
    emit("check", envInfo())
    exit(0)
}

if mode == "dump" {
    guard AXIsProcessTrusted() else { emit("error", ["reason": "not_trusted"]); exit(3) }
    guard let nc = ncApp() else { emit("error", ["reason": "no_notification_center"]); exit(4) }
    var budget = 600
    let tree = dumpTree(AXUIElementCreateApplication(nc.processIdentifier), depth: 0, budget: &budget)
    emit("dump", ["env": envInfo(), "tree": tree, "nodesLeft": budget])
    exit(0)
}

if mode == "extract-icon" {
    guard let src = appPathArg, !src.isEmpty, let dst = outPath, !dst.isEmpty else {
        fputs("usage: notify-probe-cli extract-icon --app /Applications/Foo.app --out /path/to/icon.png\n", stderr)
        exit(2)
    }
    let img = NSWorkspace.shared.icon(forFile: src)
    let size = NSSize(width: 128, height: 128)
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: 128, pixelsHigh: 128,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    )
    guard let r = rep, let ctx = NSGraphicsContext(bitmapImageRep: r) else { exit(3) }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = ctx
    ctx.imageInterpolation = .high
    img.draw(in: NSRect(origin: .zero, size: size), from: .zero, operation: .copy, fraction: 1.0)
    NSGraphicsContext.restoreGraphicsState()
    guard let png = r.representation(using: .png, properties: [:]) else { exit(4) }
    let url = URL(fileURLWithPath: dst)
    try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    do {
        try png.write(to: url)
        print("ok \(png.count)")
        exit(0)
    } catch {
        fputs("write failed: \(error)\n", stderr)
        exit(5)
    }
}

// MARK: - 模式：合成样本（EventKit，仅创建/删除标题含合成标记的条目）

if mode == "make-reminder" || mode == "make-event" || mode == "cleanup" || mode == "ek-status" {
    let store = EKEventStore()
    func wait(_ body: (@escaping () -> Void) -> Void) {
        let sem = DispatchSemaphore(value: 0)
        body { sem.signal() }
        _ = sem.wait(timeout: .now() + 60)
    }
    let needRem = mode != "make-event"
    let needCal = mode != "make-reminder"
    var remOK = false, calOK = false
    if mode == "ek-status" {
        emit("ek_status", ["reminders": EKEventStore.authorizationStatus(for: .reminder).rawValue,
                           "events": EKEventStore.authorizationStatus(for: .event).rawValue])
        exit(0)
    }
    if needRem { wait { done in store.requestFullAccessToReminders { g, _ in remOK = g; done() } } }
    if needCal { wait { done in store.requestFullAccessToEvents { g, _ in calOK = g; done() } } }
    let title = "\(marker) \(sampleTitle)"
    let due = Date().addingTimeInterval(inSeconds)
    if mode == "make-reminder" {
        guard remOK, let cal = store.defaultCalendarForNewReminders() else { emit("error", ["reason": "reminders_denied"]); exit(5) }
        let r = EKReminder(eventStore: store)
        r.title = title
        r.notes = "\(marker) 合成正文"
        r.calendar = cal
        var comps = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute], from: due)
        comps.calendar = Calendar.current
        comps.timeZone = TimeZone.current
        let exactDue = Calendar.current.date(from: comps) ?? due
        r.dueDateComponents = comps
        r.addAlarm(EKAlarm(absoluteDate: exactDue))
        do { try store.save(r, commit: true) } catch { emit("error", ["reason": "\(error)"]); exit(6) }
        emit("made", ["kind": "reminder", "due": iso.string(from: exactDue), "title": title])
    } else if mode == "make-event" {
        guard calOK, let cal = store.defaultCalendarForNewEvents else { emit("error", ["reason": "calendar_denied"]); exit(5) }
        let e = EKEvent(eventStore: store)
        e.title = title
        e.notes = "\(marker) 合成正文"
        e.calendar = cal
        e.startDate = due
        e.endDate = due.addingTimeInterval(600)
        e.addAlarm(EKAlarm(relativeOffset: 0))
        do { try store.save(e, span: .thisEvent, commit: true) } catch { emit("error", ["reason": "\(error)"]); exit(6) }
        emit("made", ["kind": "event", "start": iso.string(from: due), "title": title])
    } else {
        guard !marker.isEmpty else { exit(2) }
        var removed = ["reminders": 0, "events": 0]
        if remOK {
            var items: [EKReminder] = []
            wait { done in store.fetchReminders(matching: store.predicateForReminders(in: nil)) { l in items = l ?? []; done() } }
            for r in items where (r.title ?? "").contains(marker) {
                if (try? store.remove(r, commit: false)) != nil { removed["reminders"]! += 1 }
            }
        }
        if calOK {
            let pred = store.predicateForEvents(withStart: Date().addingTimeInterval(-3 * 86400),
                                                end: Date().addingTimeInterval(3 * 86400), calendars: nil)
            for e in store.events(matching: pred) where (e.title ?? "").contains(marker) {
                if (try? store.remove(e, span: .thisEvent, commit: false)) != nil { removed["events"]! += 1 }
            }
        }
        try? store.commit()
        emit("cleanup", ["removed": removed, "remindersAccess": remOK, "eventsAccess": calOK])
    }
    exit(0)
}

guard mode == "observe" else { fputs("unknown mode \(mode)\n", stderr); exit(2) }

// MARK: - 模式：observe

var current: [String: Banner] = [:]
var baselined = false
var scanCount = 0
var scanMsTotal = 0.0
var scanMsMax = 0.0
var lastScan = Date.distantPast
var scanGen = 0
var eventCounts: [String: Int] = [:]
var eventLogged = 0
var stats: [String: Int] = ["added": 0, "removed": 0, "markerAdded": 0]
var observer: AXObserver? = nil
var attachedPid: pid_t = -1
var appEl: AXUIElement? = nil
var trustedNow = AXIsProcessTrusted()

func scan(_ reason: String) {
    guard let root = appEl else { return }
    let t0 = Date()
    let (list, nodes) = findBanners(root)
    let ms = Date().timeIntervalSince(t0) * 1000
    scanCount += 1
    scanMsTotal += ms
    scanMsMax = max(scanMsMax, ms)
    lastScan = Date()
    var next: [String: Banner] = [:]
    for b in list { next[b.sig] = b }
    if !baselined {
        baselined = true
        current = next
        emit("baseline", ["count": next.count, "nodes": nodes, "ms": Int(ms),
                          "rules": Array(Set(list.map { $0.info["rule"] as? String ?? "?" }))])
        return
    }
    let added = next.keys.filter { current[$0] == nil }
    let removed = current.keys.filter { next[$0] == nil }
    if added.isEmpty && removed.isEmpty { return }
    let ctx: [String: Any] = ["reason": reason, "nodes": nodes, "ms": Int(ms), "locked": screenLocked(),
                              "frontmost": NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? NSNull(),
                              "visible": next.count]
    for k in added {
        let b = next[k]!
        stats["added"]! += 1
        if b.markerHit { stats["markerAdded"]! += 1 }
        emit("banner_added", ctx.merging(["sig": b.sig, "marker": b.markerHit, "banner": b.info]) { a, _ in a })
        sendSocket(b.relayPayload.merging(["type": "banner_added"]) { a, _ in a })
    }
    for k in removed {
        let b = current[k]!
        stats["removed"]! += 1
        emit("banner_removed", ctx.merging(["sig": b.sig, "marker": b.markerHit, "path": b.path]) { a, _ in a })
        sendSocket(b.relayPayload.merging(["type": "banner_removed"]) { a, _ in a })
    }
    current = next
}

func scheduleScan(_ reason: String) {
    scanGen += 1
    let g = scanGen
    if Date().timeIntervalSince(lastScan) > 0.6 {
        scan(reason)
    } else {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { if g == scanGen { scan(reason) } }
    }
    // 横幅文字可能晚于窗口创建才填充：1 秒后补扫一次。
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { scan(reason + "+1s") }
}

let watched: [String] = [
    kAXWindowCreatedNotification, kAXUIElementDestroyedNotification, kAXCreatedNotification,
    kAXLayoutChangedNotification, kAXValueChangedNotification, kAXTitleChangedNotification,
    kAXFocusedWindowChangedNotification, kAXFocusedUIElementChangedNotification,
    kAXWindowMovedNotification, kAXWindowResizedNotification, "AXAnnouncementRequested",
]

let callback: AXObserverCallbackWithInfo = { _, element, notif, info, _ in
    let name = notif as String
    eventCounts[name, default: 0] += 1
    if eventLogged < 40 {
        eventLogged += 1
        var f: [String: Any] = ["name": name, "role": short(axStr(element, "AXRole")),
                                "subrole": short(axStr(element, "AXSubrole"))]
        if let d = info as? [String: Any] { f["infoKeys"] = Array(d.keys) }
        emit("ax_event", f)
    }
    if name == (kAXWindowCreatedNotification as String), let obs = observer {
        for n in [kAXUIElementDestroyedNotification, kAXLayoutChangedNotification, kAXCreatedNotification] {
            AXObserverAddNotification(obs, element, n as CFString, nil)
        }
    }
    scheduleScan(name)
}

func detach() {
    if let obs = observer {
        CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(obs), .defaultMode)
    }
    observer = nil
    appEl = nil
    attachedPid = -1
}

func attach() {
    guard trustedNow, let nc = ncApp() else { return }
    let pid = nc.processIdentifier
    if pid == attachedPid { return }
    detach()
    var obs: AXObserver?
    let err = AXObserverCreateWithInfoCallback(pid, callback, &obs)
    guard err == .success, let o = obs else { emit("attach_error", ["code": Int(err.rawValue)]); return }
    let el = AXUIElementCreateApplication(pid)
    var results: [String: Int] = [:]
    for n in watched { results[n] = Int(AXObserverAddNotification(o, el, n as CFString, nil).rawValue) }
    // 已存在窗口也挂上销毁/布局事件。
    for w in (axAttr(el, kAXWindowsAttribute as String) as? [AXUIElement]) ?? [] {
        for n in [kAXUIElementDestroyedNotification, kAXLayoutChangedNotification, kAXCreatedNotification] {
            AXObserverAddNotification(o, w, n as CFString, nil)
        }
    }
    CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(o), .defaultMode)
    observer = o
    appEl = el
    attachedPid = pid
    baselined = false
    emit("attach", ["ncPid": Int(pid), "register": results])
    scan("baseline")
}

emit("start", envInfo().merging(["seconds": seconds, "pollMs": pollMs, "marker": marker]) { a, _ in a })
sendSocket(["type": "heartbeat", "trusted": trustedNow, "locked": screenLocked(),
            "pid": Int(getpid()), "macOS": ProcessInfo.processInfo.operatingSystemVersionString])
if !trustedNow { emit("waiting_trust", [:]) }
attach()

NSWorkspace.shared.notificationCenter.addObserver(
    forName: NSWorkspace.didTerminateApplicationNotification, object: nil, queue: .main
) { n in
    if let a = n.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
       a.bundleIdentifier == "com.apple.notificationcenterui" {
        emit("nc_terminated", ["pid": Int(a.processIdentifier)])
        detach()
    }
}
NSWorkspace.shared.notificationCenter.addObserver(
    forName: NSWorkspace.didLaunchApplicationNotification, object: nil, queue: .main
) { n in
    if let a = n.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
       a.bundleIdentifier == "com.apple.notificationcenterui" {
        emit("nc_launched", ["pid": Int(a.processIdentifier)])
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { attach() }
    }
}

// 权限撤销/恢复与通知中心重启的兜底检查。
var lastHeartbeat = Date()
Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { _ in
    let t = AXIsProcessTrusted()
    if t != trustedNow {
        trustedNow = t
        emit(t ? "trust_granted" : "trust_lost", [:])
        sendSocket(["type": t ? "trust_granted" : "trust_lost", "trusted": t,
                    "locked": screenLocked(), "pid": Int(getpid()),
                    "macOS": ProcessInfo.processInfo.operatingSystemVersionString])
        lastHeartbeat = Date()
        if t { attach() } else { detach() }
    } else if t, let nc = ncApp(), nc.processIdentifier != attachedPid {
        attach()
    }
    if Date().timeIntervalSince(lastHeartbeat) >= 10.0 {
        lastHeartbeat = Date()
        sendSocket(["type": "heartbeat", "trusted": trustedNow,
                    "locked": screenLocked(), "pid": Int(getpid()),
                    "macOS": ProcessInfo.processInfo.operatingSystemVersionString])
    }
}

if pollMs > 0 {
    Timer.scheduledTimer(withTimeInterval: Double(pollMs) / 1000.0, repeats: true) { _ in scan("poll") }
}

// --seconds 0 表示常驻运行（由 manage.py 随 host 启停），收到 SIGTERM/SIGINT 优雅退出。
signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
let sigTerm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigTerm.setEventHandler {
    emit("stopped", ["reason": "SIGTERM"])
    exit(0)
}
sigTerm.resume()
let sigInt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigInt.setEventHandler {
    emit("stopped", ["reason": "SIGINT"])
    exit(0)
}
sigInt.resume()

if seconds > 0 {
    Timer.scheduledTimer(withTimeInterval: seconds, repeats: false) { _ in
        emit("summary", [
            "scans": scanCount,
            "scanMsAvg": scanCount > 0 ? Int(scanMsTotal / Double(scanCount) * 10) / 10 : 0,
            "scanMsMax": Int(scanMsMax),
            "events": eventCounts,
            "stats": stats,
            "visibleAtEnd": current.count,
        ])
        exit(0)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
app.run()
