import Foundation
import AppKit
import ApplicationServices

let fcpBundleID = "com.apple.FinalCut"

struct AXFailure: Error, CustomStringConvertible {
    let description: String
}

func log(_ message: String) {
    FileHandle.standardError.write(("[vclip-fcp] \(message)\n").data(using: .utf8)!)
}

func axAttribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, name as CFString, &value)
    return error == .success ? value : nil
}

func axString(_ element: AXUIElement, _ name: String) -> String {
    guard let value = axAttribute(element, name) else { return "" }
    if let text = value as? String { return text }
    if CFGetTypeID(value) == AXValueGetTypeID() { return "" }
    return String(describing: value)
}

func axBool(_ element: AXUIElement, _ name: String) -> Bool? {
    axAttribute(element, name) as? Bool
}

func axChildren(_ element: AXUIElement) -> [AXUIElement] {
    axAttribute(element, kAXChildrenAttribute) as? [AXUIElement] ?? []
}

func axActions(_ element: AXUIElement) -> [String] {
    var values: CFArray?
    let error = AXUIElementCopyActionNames(element, &values)
    return error == .success ? (values as? [String] ?? []) : []
}

func axPress(_ element: AXUIElement) throws {
    let error = AXUIElementPerformAction(element, kAXPressAction as CFString)
    guard error == .success else {
        throw AXFailure(description: "AXPress failed: \(error.rawValue)")
    }
}

func axPressAllowingModalTransition(
    _ element: AXUIElement,
    description: String
) throws {
    let error = AXUIElementPerformAction(element, kAXPressAction as CFString)
    if error == .success {
        return
    }
    if error.rawValue == -25204 { // kAXErrorCannotComplete
        log("\(description): AX returned -25204 during modal transition; verifying resulting UI")
        return
    }
    throw AXFailure(
        description: "\(description) AXPress failed: \(error.rawValue)"
    )
}

func axSetSelected(_ element: AXUIElement) -> Bool {
    AXUIElementSetAttributeValue(element, kAXSelectedAttribute as CFString, kCFBooleanTrue) == .success
}

func axSetStringValue(_ element: AXUIElement, _ value: String) -> Bool {
    AXUIElementSetAttributeValue(
        element,
        kAXValueAttribute as CFString,
        value as CFString
    ) == .success
}

func axParent(_ element: AXUIElement) -> AXUIElement? {
    guard let value = axAttribute(element, kAXParentAttribute) else { return nil }
    guard CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    return unsafeBitCast(value, to: AXUIElement.self)
}

func texts(_ element: AXUIElement) -> [String] {
    [
        axString(element, kAXTitleAttribute),
        axString(element, kAXValueAttribute),
        axString(element, kAXDescriptionAttribute),
        axString(element, kAXHelpAttribute),
        axString(element, kAXIdentifierAttribute),
    ].filter { !$0.isEmpty }
}

func elementSummary(_ element: AXUIElement) -> String {
    let role = axString(element, kAXRoleAttribute)
    let subrole = axString(element, kAXSubroleAttribute)
    return "role=\(role) subrole=\(subrole) text=\(texts(element))"
}

func descendants(
    of root: AXUIElement,
    maxDepth: Int = 18,
    maxNodes: Int = 60_000
) -> [AXUIElement] {
    var result: [AXUIElement] = []
    var queue: [(AXUIElement, Int)] = [(root, 0)]
    var cursor = 0
    while cursor < queue.count && result.count < maxNodes {
        let (element, depth) = queue[cursor]
        cursor += 1
        result.append(element)
        if depth >= maxDepth { continue }
        for child in axChildren(element) {
            queue.append((child, depth + 1))
        }
    }
    return result
}

func waitUntil<T>(
    timeout: TimeInterval,
    interval: TimeInterval = 0.20,
    description: String,
    _ body: () -> T?
) throws -> T {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if let value = body() { return value }
        RunLoop.current.run(until: Date().addingTimeInterval(interval))
    }
    throw AXFailure(description: "Timed out waiting for \(description)")
}

func runningFinalCut() throws -> NSRunningApplication {
    if let app = NSRunningApplication.runningApplications(withBundleIdentifier: fcpBundleID).first {
        return app
    }
    guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: fcpBundleID) else {
        throw AXFailure(description: "Final Cut Pro is not installed")
    }
    let config = NSWorkspace.OpenConfiguration()
    config.activates = true
    let semaphore = DispatchSemaphore(value: 0)
    var launched: NSRunningApplication?
    var launchError: Error?
    NSWorkspace.shared.openApplication(at: url, configuration: config) { app, error in
        launched = app
        launchError = error
        semaphore.signal()
    }
    semaphore.wait()
    if let launchError { throw launchError }
    guard let launched else { throw AXFailure(description: "Could not launch Final Cut Pro") }
    return launched
}

func trustedAccessibility() -> Bool {
    let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    return AXIsProcessTrustedWithOptions(options)
}

func keyEvent(_ keyCode: CGKeyCode, flags: CGEventFlags = []) {
    guard let source = CGEventSource(stateID: .hidSystemState) else { return }
    let down = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: true)
    let up = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: false)
    down?.flags = flags
    up?.flags = flags
    down?.post(tap: .cghidEventTap)
    up?.post(tap: .cghidEventTap)
}

func commandKey(_ keyCode: CGKeyCode, shift: Bool = false) {
    var flags: CGEventFlags = [.maskCommand]
    if shift { flags.insert(.maskShift) }
    keyEvent(keyCode, flags: flags)
}

func paste(_ value: String) {
    let board = NSPasteboard.general
    board.clearContents()
    board.setString(value, forType: .string)
    commandKey(9) // V
}

func activate(_ app: NSRunningApplication) {
    app.activate(options: [.activateAllWindows])
}

func findExactText(_ root: AXUIElement, _ value: String) -> [AXUIElement] {
    descendants(of: root).filter { element in
        texts(element).contains { $0 == value }
    }
}

func selectNamedElement(_ appAX: AXUIElement, name: String) throws {
    let match = try waitUntil(timeout: 180, description: "event \(name)") { () -> AXUIElement? in
        let matches = findExactText(appAX, name)
        if matches.isEmpty { return nil }
        // Prefer a row, outline item, or pressable ancestor.
        for element in matches {
            var current: AXUIElement? = element
            for _ in 0..<8 {
                guard let candidate = current else { break }
                let role = axString(candidate, kAXRoleAttribute)
                let actions = axActions(candidate)
                if role == kAXRowRole || role == kAXOutlineRole {
                    if axSetSelected(candidate) { return candidate }
                }
                if actions.contains(kAXPressAction) {
                    return candidate
                }
                current = axParent(candidate)
            }
        }
        return matches[0]
    }
    if axActions(match).contains(kAXPressAction) {
        try axPress(match)
    } else if !axSetSelected(match) {
        throw AXFailure(description: "Found event text but could not select it: \(elementSummary(match))")
    }
}

func menuBar(_ appAX: AXUIElement) -> AXUIElement? {
    guard let value = axAttribute(appAX, kAXMenuBarAttribute) else { return nil }
    guard CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    return unsafeBitCast(value, to: AXUIElement.self)
}

func openFileMenu(_ appAX: AXUIElement) throws -> AXUIElement {
    guard let bar = menuBar(appAX) else {
        throw AXFailure(description: "Final Cut has no AX menu bar")
    }
    guard let fileItem = axChildren(bar).first(where: { axString($0, kAXTitleAttribute) == "File" }) else {
        throw AXFailure(description: "Could not find File menu")
    }
    try axPress(fileItem)
    return try waitUntil(timeout: 5, description: "File menu") {
        axChildren(fileItem).first(where: { axString($0, kAXRoleAttribute) == kAXMenuRole })
    }
}

func shareCount(from title: String, expected: Int) -> Int? {
    if title == "Share" { return expected == 1 ? 1 : nil }
    let pattern = #"^Share\s+(\d+)\s+Projects?$"#
    guard let regex = try? NSRegularExpression(pattern: pattern),
          let match = regex.firstMatch(in: title, range: NSRange(title.startIndex..., in: title)),
          let range = Range(match.range(at: 1), in: title) else { return nil }
    return Int(title[range])
}

func invokeShareDestination(
    appAX: AXUIElement,
    expectedCount: Int,
    destinationName: String
) throws -> String {
    let menu = try openFileMenu(appAX)
    let shareItem = try waitUntil(timeout: 5, description: "dynamic Share menu item") {
        axChildren(menu).first { item in
            let title = axString(item, kAXTitleAttribute)
            return title == "Share" || title.hasPrefix("Share ")
        }
    }
    let title = axString(shareItem, kAXTitleAttribute)
    guard let selectedCount = shareCount(from: title, expected: expectedCount) else {
        keyEvent(53) // Escape
        throw AXFailure(description: "Could not parse Share selection count from \(title)")
    }
    guard selectedCount == expectedCount else {
        keyEvent(53)
        throw AXFailure(description: "Selection mismatch: File menu says \(title), expected \(expectedCount)")
    }

    if axActions(shareItem).contains(kAXPressAction) {
        try axPress(shareItem)
    }
    let submenu = try waitUntil(timeout: 5, description: "Share submenu") {
        axChildren(shareItem).first(where: { axString($0, kAXRoleAttribute) == kAXMenuRole })
    }
    let destination = try waitUntil(timeout: 5, description: "share destination \(destinationName)") {
        axChildren(submenu).first { item in
            let title = axString(item, kAXTitleAttribute)
            return title == destinationName
                || title.replacingOccurrences(of: "...", with: "…") == destinationName
                || title.replacingOccurrences(of: "…", with: "...") == destinationName
        }
    }
    try axPress(destination)
    return title
}

func findButton(_ appAX: AXUIElement, names: [String]) -> AXUIElement? {
    let set = Set(names)
    return descendants(of: appAX, maxDepth: 20).first { element in
        axString(element, kAXRoleAttribute) == kAXButtonRole
            && texts(element).contains(where: { set.contains($0) })
    }
}

func clickNext(_ appAX: AXUIElement) throws {
    let button = try waitUntil(timeout: 120, description: "Share Next button") {
        findButton(appAX, names: ["Next…", "Next...", "Next"])
    }
    try axPress(button)
}

func chooseSaveDirectory(_ appAX: AXUIElement, directory: String) throws {
    _ = try waitUntil(timeout: 120, description: "save panel") {
        findButton(appAX, names: ["Share", "Save", "Choose", "Open", "Export"])
    }
    commandKey(5, shift: true) // Command-Shift-G
    RunLoop.current.run(until: Date().addingTimeInterval(0.35))
    paste(directory)
    RunLoop.current.run(until: Date().addingTimeInterval(0.25))
    keyEvent(36) // Return in Go to Folder
    RunLoop.current.run(until: Date().addingTimeInterval(0.8))
    let save = try waitUntil(timeout: 30, description: "Save/Choose button") {
        findButton(appAX, names: ["Share", "Save", "Choose", "Open", "Export"])
    }
    try axPress(save)
    // The panel disappearing is stronger evidence than a blind delay.
    _ = try waitUntil(timeout: 60, description: "save panel dismissal") {
        findButton(appAX, names: ["Share", "Save", "Choose", "Open", "Export"]) == nil ? true : nil
    }
}


extension String {
    var casefolded: String {
        folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)
            .lowercased()
    }
}

func nearestDialogAncestor(_ element: AXUIElement) -> AXUIElement? {
    var current: AXUIElement? = element
    for _ in 0..<12 {
        guard let candidate = current else { return nil }
        let role = axString(candidate, kAXRoleAttribute)
        let subrole = axString(candidate, kAXSubroleAttribute)
        if role == "AXSheet" || role == "AXWindow" || subrole == "AXDialog" {
            return candidate
        }
        current = axParent(candidate)
    }
    return nil
}

func findImportLibraryDialog(_ appAX: AXUIElement) -> AXUIElement? {
    guard let choose = findButton(appAX, names: ["Choose"]) else { return nil }
    guard let dialog = nearestDialogAncestor(choose) else { return nil }
    let hasNew = findButton(dialog, names: ["New…", "New..."]) != nil
    let text = descendants(of: dialog, maxDepth: 12)
        .flatMap { texts($0) }
        .joined(separator: " ")
        .casefolded
    return hasNew && (text.contains("library") || text.contains("import")) ? dialog : nil
}

func selectNamedElement(in root: AXUIElement, name: String) -> Bool {
    let matches = findExactText(root, name)
    for element in matches {
        var current: AXUIElement? = element
        for _ in 0..<8 {
            guard let candidate = current else { break }
            let role = axString(candidate, kAXRoleAttribute)
            let actions = axActions(candidate)
            if role == kAXRowRole || role == kAXOutlineRole {
                if axSetSelected(candidate) { return true }
            }
            if actions.contains(kAXPressAction) {
                do {
                    try axPress(candidate)
                    return true
                } catch {
                }
            }
            current = axParent(candidate)
        }
        if axSetSelected(element) { return true }
    }
    return false
}

func editableSaveNameField(_ root: AXUIElement) -> AXUIElement? {
    descendants(of: root, maxDepth: 16).first { element in
        let role = axString(element, kAXRoleAttribute)
        let subrole = axString(element, kAXSubroleAttribute)
        guard role == kAXTextFieldRole else { return false }
        guard subrole != "AXSearchField" else { return false }
        return axBool(element, kAXEnabledAttribute) ?? true
    }
}

func findNativeSavePanel(_ appAX: AXUIElement) -> AXUIElement? {
    for element in descendants(of: appAX, maxDepth: 20) {
        guard axString(element, kAXRoleAttribute) == kAXButtonRole else {
            continue
        }
        guard texts(element).contains("Save") else {
            continue
        }
        guard let panel = nearestDialogAncestor(element) else {
            continue
        }
        if editableSaveNameField(panel) != nil {
            return panel
        }
    }
    return nil
}

func createImportLibrary(
    appAX: AXUIElement,
    dialog: AXUIElement,
    rootDirectory: String,
    libraryName: String
) throws {
    try FileManager.default.createDirectory(
        atPath: rootDirectory,
        withIntermediateDirectories: true,
        attributes: nil
    )

    guard let newButton = findButton(dialog, names: ["New…", "New..."]) else {
        throw AXFailure(description: "Import-library dialog has no New button")
    }

    log("Creating Final Cut staging library: \(rootDirectory)/\(libraryName).fcpbundle")
    try axPressAllowingModalTransition(
        newButton,
        description: "Open New Library save panel"
    )

    var savePanel = try waitUntil(
        timeout: 30,
        description: "New Library save panel"
    ) {
        findNativeSavePanel(appAX)
    }

    guard let nameField = editableSaveNameField(savePanel) else {
        throw AXFailure(description: "Could not find New Library name field")
    }
    guard axSetStringValue(nameField, libraryName) else {
        throw AXFailure(
            description: "Could not set New Library name field to \(libraryName)"
        )
    }
    log("Set staging library name: \(libraryName)")

    commandKey(5, shift: true) // Command-Shift-G
    RunLoop.current.run(until: Date().addingTimeInterval(0.45))
    paste(rootDirectory)
    RunLoop.current.run(until: Date().addingTimeInterval(0.30))
    keyEvent(36)
    RunLoop.current.run(until: Date().addingTimeInterval(0.90))

    savePanel = try waitUntil(
        timeout: 20,
        description: "New Library save panel after folder navigation"
    ) {
        findNativeSavePanel(appAX)
    }

    let create = try waitUntil(
        timeout: 20,
        description: "New Library Save/Create button"
    ) {
        findButton(savePanel, names: ["Save", "Create"])
    }
    try axPressAllowingModalTransition(
        create,
        description: "Create New Library"
    )
}

func handleImportLibraryDialog(
    appAX: AXUIElement,
    libraryRoot: String,
    libraryName: String
) throws {
    let dialog = try waitUntil(timeout: 30, description: "FCPXML import library dialog") {
        findImportLibraryDialog(appAX)
    }

    if !findExactText(dialog, libraryName).isEmpty {
        log("Selecting existing Final Cut staging library: \(libraryName)")
        guard selectNamedElement(in: dialog, name: libraryName) else {
            throw AXFailure(description: "Found staging library \(libraryName) but could not select it")
        }
        let choose = try waitUntil(timeout: 10, description: "Import Choose button") {
            findButton(dialog, names: ["Choose"])
        }
        try axPress(choose)
        return
    }

    try createImportLibrary(
        appAX: appAX,
        dialog: dialog,
        rootDirectory: libraryRoot,
        libraryName: libraryName
    )
}

func openXML(_ path: String) throws {
    let url = URL(fileURLWithPath: path)
    guard FileManager.default.fileExists(atPath: path) else {
        throw AXFailure(description: "FCPXML does not exist: \(path)")
    }
    if !NSWorkspace.shared.open(url) {
        throw AXFailure(description: "macOS could not open FCPXML: \(path)")
    }
}

func jsonPrint(_ payload: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys]) {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
    }
}

func probe(appAX: AXUIElement) throws {
    let windows = (axAttribute(appAX, kAXWindowsAttribute) as? [AXUIElement] ?? []).map {
        [
            "title": axString($0, kAXTitleAttribute),
            "role": axString($0, kAXRoleAttribute),
            "subrole": axString($0, kAXSubroleAttribute),
        ]
    }
    let menu = try? openFileMenu(appAX)
    let fileItems = menu.map { axChildren($0).map { axString($0, kAXTitleAttribute) } } ?? []
    keyEvent(53)
    jsonPrint(["windows": windows, "file_menu_items": fileItems])
}

struct Arguments {
    var command: String = ""
    var xml: String?
    var event: String?
    var expected: Int?
    var output: String?
    var destination: String = "Export File (default)…"
    var libraryRoot: String?
    var libraryName: String?
}

func parseArguments() throws -> Arguments {
    var result = Arguments()
    var values = Array(CommandLine.arguments.dropFirst())
    guard !values.isEmpty else {
        throw AXFailure(description: "Usage: vclip-fcp-ax probe | export-batch --xml PATH --event NAME --expected N --output DIR [--destination NAME] [--library-root DIR --library-name NAME]")
    }
    result.command = values.removeFirst()
    var index = 0
    while index < values.count {
        let key = values[index]
        guard index + 1 < values.count else {
            throw AXFailure(description: "Missing value for \(key)")
        }
        let value = values[index + 1]
        switch key {
        case "--xml": result.xml = value
        case "--event": result.event = value
        case "--expected": result.expected = Int(value)
        case "--output": result.output = value
        case "--destination": result.destination = value
        case "--library-root": result.libraryRoot = value
        case "--library-name": result.libraryName = value
        default: throw AXFailure(description: "Unknown option: \(key)")
        }
        index += 2
    }
    return result
}

do {
    let args = try parseArguments()
    guard trustedAccessibility() else {
        throw AXFailure(description: "Accessibility permission is required. Grant it to this binary in System Settings > Privacy & Security > Accessibility, then rerun.")
    }
    let app = try runningFinalCut()
    activate(app)
    let appAX = AXUIElementCreateApplication(app.processIdentifier)

    switch args.command {
    case "probe":
        try probe(appAX: appAX)
    case "export-batch":
        guard let xml = args.xml,
              let event = args.event,
              let expected = args.expected,
              let output = args.output else {
            throw AXFailure(description: "export-batch requires --xml, --event, --expected and --output")
        }
        log("Opening \(xml)")
        try openXML(xml)
        activate(app)

        if let libraryRoot = args.libraryRoot, let libraryName = args.libraryName {
            log("Automating FCPXML import-library selection")
            try handleImportLibraryDialog(
                appAX: appAX,
                libraryRoot: libraryRoot,
                libraryName: libraryName
            )
            activate(app)
        } else if args.libraryRoot != nil || args.libraryName != nil {
            throw AXFailure(description: "--library-root and --library-name must be provided together")
        }

        log("Waiting for imported event: \(event)")
        try selectNamedElement(appAX, name: event)
        activate(app)
        commandKey(18) // Command-1: focus Browser in Final Cut
        RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        commandKey(0)  // Command-A
        RunLoop.current.run(until: Date().addingTimeInterval(0.35))
        log("Invoking batch Share for \(expected) project(s)")
        let shareTitle = try invokeShareDestination(
            appAX: appAX,
            expectedCount: expected,
            destinationName: args.destination
        )
        try clickNext(appAX)
        try chooseSaveDirectory(appAX, directory: output)
        jsonPrint([
            "status": "share_started",
            "xml": xml,
            "event": event,
            "expected_count": expected,
            "output_directory": output,
            "share_destination": args.destination,
            "share_menu_title": shareTitle,
            "fcp_pid": app.processIdentifier,
            "library_root": args.libraryRoot ?? "",
            "library_name": args.libraryName ?? "",
        ])
    default:
        throw AXFailure(description: "Unknown command: \(args.command)")
    }
} catch {
    log("ERROR: \(error)")
    jsonPrint(["status": "failed", "error": String(describing: error)])
    exit(1)
}
