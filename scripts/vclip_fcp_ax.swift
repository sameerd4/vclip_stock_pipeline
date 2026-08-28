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
    destinationName: String,
    eventName: String,
    selectionTimeout: TimeInterval = 60
) throws -> String {
    let deadline = Date().addingTimeInterval(selectionTimeout)
    var attempt = 0
    var lastTitle = ""

    while Date() < deadline {
        attempt += 1

        let menu = try openFileMenu(appAX)
        let shareSearchStarted = Date()

        let shareItem = try waitUntil(
            timeout: 5,
            description: "dynamic Share menu item"
        ) {
            // Search recursively because Final Cut's dynamic Share item is not
            // always exposed as the same direct AX child on every invocation.
            let items =
                axChildren(menu)
                + descendants(
                    of: menu,
                    maxDepth: 6,
                    maxNodes: 1_000
                )

            // Prefer a Share item whose title/text proves the exact selection.
            if let counted = items.first(where: { item in
                let candidates =
                    [axString(item, kAXTitleAttribute)]
                    + texts(item)
                    + descendants(
                        of: item,
                        maxDepth: 4,
                        maxNodes: 200
                    ).flatMap { texts($0) }

                return candidates.contains {
                    shareCount(
                        from: $0,
                        expected: expectedCount
                    ) == expectedCount
                }
            }) {
                return counted
            }

            // Give Final Cut a moment to publish "Share N Projects". If it
            // remains generic, return "Share" so the existing recovery path
            // can Escape, refocus the Browser, Command-A, and retry.
            if Date().timeIntervalSince(shareSearchStarted) >= 1.0 {
                return items.first { item in
                    let candidates =
                        [axString(item, kAXTitleAttribute)]
                        + texts(item)

                    return candidates.contains { $0 == "Share" }
                }
            }

            return nil
        }

        let titleCandidates =
            [axString(shareItem, kAXTitleAttribute)]
            + texts(shareItem)
            + descendants(
                of: shareItem,
                maxDepth: 4,
                maxNodes: 200
            ).flatMap { texts($0) }

        let title = titleCandidates.first {
            shareCount(
                from: $0,
                expected: expectedCount
            ) == expectedCount
        } ?? axString(shareItem, kAXTitleAttribute)

        lastTitle = title

        let selectedCount = shareCount(
            from: title,
            expected: expectedCount
        )

        if selectedCount == expectedCount {
            if attempt > 1 {
                log(
                    "Share selection stabilized after "
                    + "\(attempt) attempt(s): \(title)"
                )
            }

            if axActions(shareItem).contains(kAXPressAction) {
                try axPress(shareItem)
            }

            let submenu = try waitUntil(
                timeout: 5,
                description: "Share submenu"
            ) {
                axChildren(shareItem).first {
                    axString($0, kAXRoleAttribute) == kAXMenuRole
                }
            }

            let destination = try waitUntil(
                timeout: 5,
                description: "share destination \(destinationName)"
            ) {
                axChildren(submenu).first { item in
                    let itemTitle = axString(
                        item,
                        kAXTitleAttribute
                    )

                    return itemTitle == destinationName
                        || itemTitle.replacingOccurrences(
                            of: "...",
                            with: "…"
                        ) == destinationName
                        || itemTitle.replacingOccurrences(
                            of: "…",
                            with: "..."
                        ) == destinationName
                }
            }

            try axPress(destination)
            return title
        }

        keyEvent(53) // Escape File menu.

        let observed = selectedCount.map(String.init) ?? "unparsed"
        log(
            "Share selection not ready "
            + "(attempt \(attempt)): "
            + "menu='\(title)', "
            + "count=\(observed), "
            + "expected=\(expectedCount); "
            + "refocusing Browser and reselecting"
        )

        // Final Cut sometimes exposes the generic "Share" menu item before
        // its Browser selection state has propagated to the File menu.
        //
        // Browser focus + Command-A usually fixes this. If it remains stuck,
        // explicitly restore the intended imported event as well.
        if attempt % 3 == 0 {
            let restored = selectNamedElement(
                in: appAX,
                name: eventName
            )

            log(
                "Share selection recovery: "
                + "event reselection "
                + (restored ? "succeeded" : "did not resolve")
            )

            RunLoop.current.run(
                until: Date().addingTimeInterval(0.35)
            )
        }

        commandKey(18) // Command-1: Browser
        RunLoop.current.run(
            until: Date().addingTimeInterval(0.30)
        )

        commandKey(0) // Command-A
        RunLoop.current.run(
            until: Date().addingTimeInterval(0.80)
        )
    }

    throw AXFailure(
        description:
            "Timed out waiting for Share selection count "
            + "\(expectedCount); last menu title was '\(lastTitle)'"
    )
}

func findButton(_ appAX: AXUIElement, names: [String]) -> AXUIElement? {
    let set = Set(names)
    return descendants(of: appAX, maxDepth: 20).first { element in
        axString(element, kAXRoleAttribute) == kAXButtonRole
            && texts(element).contains(where: { set.contains($0) })
    }
}


func pressableAncestor(_ element: AXUIElement) -> AXUIElement? {
    var current: AXUIElement? = element
    for _ in 0..<8 {
        guard let candidate = current else { return nil }
        if axActions(candidate).contains(kAXPressAction) {
            return candidate
        }
        current = axParent(candidate)
    }
    return nil
}

func optimizedOriginalMenuItem(_ appAX: AXUIElement) -> AXUIElement? {
    let matches = findExactText(appAX, "Optimized/Original")
    for match in matches {
        if let pressable = pressableAncestor(match) {
            return pressable
        }
    }
    return nil
}

func playbackViewControlCandidates(_ appAX: AXUIElement) -> [AXUIElement] {
    descendants(of: appAX, maxDepth: 20, maxNodes: 50_000).filter { element in
        let role = axString(element, kAXRoleAttribute)
        guard role == "AXPopUpButton"
                || role == "AXMenuButton"
                || role == kAXButtonRole else {
            return false
        }

        let text = texts(element)
            .joined(separator: " ")
            .casefolded

        return text.contains("view")
    }
}

func ensureOptimizedOriginalPlayback(_ appAX: AXUIElement) throws {
    // If the menu is somehow already open, use it immediately.
    if let item = optimizedOriginalMenuItem(appAX) {
        log("Selecting Optimized/Original playback from already-open View menu")
        try axPress(item)
        return
    }

    let candidates = playbackViewControlCandidates(appAX)

    log("Searching \(candidates.count) View control candidate(s) for Media Playback")

    for candidate in candidates {
        do {
            try axPress(candidate)
        } catch {
            continue
        }

        do {
            let item = try waitUntil(
                timeout: 1.5,
                interval: 0.15,
                description: "Optimized/Original menu item"
            ) {
                optimizedOriginalMenuItem(appAX)
            }

            log("Forcing Final Cut Media Playback to Optimized/Original")
            try axPress(item)

            RunLoop.current.run(
                until: Date().addingTimeInterval(0.35)
            )

            return
        } catch {
            keyEvent(53) // Escape any unrelated menu/popover.
            RunLoop.current.run(
                until: Date().addingTimeInterval(0.15)
            )
        }
    }

    throw AXFailure(
        description: "Could not locate Final Cut Viewer View menu with Optimized/Original playback option"
    )
}

enum ShareLaunchOutcome {
    case ready(nextButton: AXUIElement)
    case copyingFiles(alert: AXUIElement)
}

func copyingFilesAlertText(_ root: AXUIElement) -> String {
    descendants(of: root, maxDepth: 12, maxNodes: 10_000)
        .flatMap { texts($0) }
        .joined(separator: " ")
        .casefolded
}

func findCopyingFilesBlockingAlert(_ appAX: AXUIElement) -> AXUIElement? {
    for element in descendants(of: appAX, maxDepth: 10, maxNodes: 20_000) {
        let role = axString(element, kAXRoleAttribute)
        let subrole = axString(element, kAXSubroleAttribute)

        guard role == "AXSheet"
                || role == "AXWindow"
                || subrole == "AXDialog" else {
            continue
        }

        let text = copyingFilesAlertText(element)
        if text.contains("operation cannot be performed")
            && text.contains("copying files")
            && text.contains("running in the background") {
            return element
        }
    }
    return nil
}

func dismissCopyingFilesBlockingAlert(
    appAX: AXUIElement,
    alert: AXUIElement
) throws {
    guard let ok = findButton(alert, names: ["OK"]) else {
        throw AXFailure(
            description: "Detected Final Cut Copying Files blocker but could not find its OK button"
        )
    }

    try axPressAllowingModalTransition(
        ok,
        description: "Dismiss Copying Files blocker"
    )

    _ = try waitUntil(
        timeout: 15,
        description: "Copying Files blocker dismissal"
    ) {
        findCopyingFilesBlockingAlert(appAX) == nil ? true : nil
    }
}

func waitForShareLaunchOutcome(
    appAX: AXUIElement,
    timeout: TimeInterval = 15
) throws -> ShareLaunchOutcome {
    let deadline = Date().addingTimeInterval(timeout)

    while Date() < deadline {
        if let alert = findCopyingFilesBlockingAlert(appAX) {
            return .copyingFiles(alert: alert)
        }

        if let next = findButton(
            appAX,
            names: ["Next…", "Next...", "Next"]
        ) {
            let enabled = axBool(next, kAXEnabledAttribute) ?? true
            if enabled {
                return .ready(nextButton: next)
            }
        }

        RunLoop.current.run(
            until: Date().addingTimeInterval(0.35)
        )
    }

    throw AXFailure(
        description: "Share destination opened neither the Share sheet nor the recognized Copying Files blocker"
    )
}


func normalizedDimensionText(_ value: String) -> String {
    value
        .replacingOccurrences(of: "×", with: "x")
        .replacingOccurrences(of: " ", with: "")
        .casefolded
}

func containsResolution(
    _ element: AXUIElement,
    width: Int,
    height: Int
) -> Bool {
    let target = "\(width)x\(height)"
    let joined = descendants(
        of: element,
        maxDepth: 4,
        maxNodes: 500
    )
    .flatMap { texts($0) }
    .map(normalizedDimensionText)
    .joined(separator: " ")

    return joined.contains(target)
}

func selectShareSettingsTab(_ appAX: AXUIElement) throws {
    let matches = try waitUntil(
        timeout: 15,
        description: "Share Settings tab"
    ) {
        let values = findExactText(appAX, "Settings")
        return values.isEmpty ? nil : values
    }

    for match in matches {
        if let button = pressableAncestor(match) {
            do {
                try axPress(button)
                RunLoop.current.run(
                    until: Date().addingTimeInterval(0.35)
                )
                return
            } catch {
            }
        }
    }

    throw AXFailure(
        description: "Found Share Settings text but could not activate it"
    )
}

func targetResolutionMenuItem(
    _ appAX: AXUIElement,
    width: Int,
    height: Int
) -> AXUIElement? {
    descendants(
        of: appAX,
        maxDepth: 24,
        maxNodes: 60_000
    ).first { element in
        let role = axString(element, kAXRoleAttribute)
        guard role == "AXMenuItem" else {
            return false
        }
        return containsResolution(
            element,
            width: width,
            height: height
        )
    }
}

func resolutionPopupCandidates(
    _ appAX: AXUIElement
) -> [AXUIElement] {
    descendants(
        of: appAX,
        maxDepth: 24,
        maxNodes: 60_000
    ).filter { element in
        let role = axString(element, kAXRoleAttribute)

        guard role == "AXPopUpButton"
                || role == "AXMenuButton"
                || role == kAXButtonRole else {
            return false
        }

        let content = descendants(
            of: element,
            maxDepth: 4,
            maxNodes: 500
        )
        .flatMap { texts($0) }
        .map(normalizedDimensionText)
        .joined(separator: " ")

        let pattern = #"[0-9]{3,4}x[0-9]{3,4}"#
        return content.range(
            of: pattern,
            options: .regularExpression
        ) != nil
    }
}

func ensureShareResolution(
    appAX: AXUIElement,
    width: Int,
    height: Int
) throws {
    log(
        "Forcing Share resolution to "
        + "\(width)x\(height)"
    )

    try selectShareSettingsTab(appAX)

    let candidates = resolutionPopupCandidates(appAX)

    log(
        "Searching \(candidates.count) "
        + "Share resolution control candidate(s)"
    )

    for candidate in candidates {
        // Already correct.
        if containsResolution(
            candidate,
            width: width,
            height: height
        ) {
            log(
                "Share resolution already "
                + "\(width)x\(height)"
            )
            return
        }

        do {
            try axPress(candidate)
        } catch {
            continue
        }

        do {
            let item = try waitUntil(
                timeout: 2.0,
                interval: 0.15,
                description: "Share resolution \(width)x\(height)"
            ) {
                targetResolutionMenuItem(
                    appAX,
                    width: width,
                    height: height
                )
            }

            try axPress(item)

            RunLoop.current.run(
                until: Date().addingTimeInterval(0.35)
            )

            log(
                "Selected Share resolution "
                + "\(width)x\(height)"
            )
            return
        } catch {
            keyEvent(53)
            RunLoop.current.run(
                until: Date().addingTimeInterval(0.15)
            )
        }
    }

    throw AXFailure(
        description:
            "Could not select required Share resolution "
            + "\(width)x\(height)"
    )
}

func launchShareWhenReady(
    appAX: AXUIElement,
    expectedCount: Int,
    destinationName: String,
    eventName: String,
    expectedWidth: Int,
    expectedHeight: Int,
    copyTimeout: TimeInterval = 900,
    retryDelay: TimeInterval = 5
) throws -> String {
    let deadline = Date().addingTimeInterval(copyTimeout)
    var attempt = 0

    while Date() < deadline {
        attempt += 1
        log("Share attempt \(attempt) for \(expectedCount) project(s)")

        let shareTitle = try invokeShareDestination(
            appAX: appAX,
            expectedCount: expectedCount,
            destinationName: destinationName,
            eventName: eventName
        )

        switch try waitForShareLaunchOutcome(appAX: appAX) {
        case .ready:
            log("Share sheet ready after \(attempt) attempt(s)")

            try ensureShareResolution(
                appAX: appAX,
                width: expectedWidth,
                height: expectedHeight
            )

            // Leave the configured Share sheet open.
            // chooseSaveDirectory owns advancing to NSSavePanel and
            // deterministic filesystem navigation.
            return shareTitle

        case .copyingFiles(let alert):
            let remaining = max(0, deadline.timeIntervalSinceNow)
            log(
                "Share blocked by Final Cut Copying Files background task; "
                + "dismissing alert and retrying "
                + "(\(Int(remaining))s remaining)"
            )

            try dismissCopyingFilesBlockingAlert(
                appAX: appAX,
                alert: alert
            )

            let pause = min(
                retryDelay,
                max(0, deadline.timeIntervalSinceNow)
            )
            if pause > 0 {
                RunLoop.current.run(
                    until: Date().addingTimeInterval(pause)
                )
            }
        }
    }

    throw AXFailure(
        description: "Timed out after \(Int(copyTimeout))s waiting for Final Cut Copying Files background task to finish"
    )
}

func chooseSaveDirectory(_ appAX: AXUIElement, directory: String) throws {
    // launchShareWhenReady() stops at Final Cut's Share sheet. Advance that
    // sheet explicitly before attempting filesystem navigation.
    let next = try waitUntil(timeout: 60, description: "Share Next button") {
        findButton(appAX, names: ["Next", "Next…", "Next..."])
    }

    log("Advancing Share sheet to system save panel")
    try axPress(next)

    _ = try waitUntil(timeout: 120, description: "system save panel") {
        findButton(appAX, names: ["Share", "Save", "Choose", "Open", "Export"])
    }

    log("Navigating save panel to: \(directory)")
    commandKey(5, shift: true) // Command-Shift-G
    RunLoop.current.run(until: Date().addingTimeInterval(0.5))

    paste(directory)
    RunLoop.current.run(until: Date().addingTimeInterval(0.3))

    keyEvent(36) // Return in Go to Folder
    RunLoop.current.run(until: Date().addingTimeInterval(1.0))

    let save = try waitUntil(timeout: 30, description: "Save/Choose button") {
        findButton(appAX, names: ["Share", "Save", "Choose", "Open", "Export"])
    }

    log("Confirming export save panel")
    try axPress(save)

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


func dialogText(_ element: AXUIElement) -> String {
    descendants(
        of: element,
        maxDepth: 16,
        maxNodes: 20_000
    )
    .flatMap { texts($0) }
    .joined(separator: " ")
    .casefolded
}

func candidateModalDialogs(
    _ appAX: AXUIElement
) -> [AXUIElement] {
    descendants(
        of: appAX,
        maxDepth: 18,
        maxNodes: 30_000
    ).filter { element in
        let role = axString(
            element,
            kAXRoleAttribute
        )
        let subrole = axString(
            element,
            kAXSubroleAttribute
        )

        return role == "AXSheet"
            || role == "AXWindow"
            || subrole == "AXDialog"
    }
}

func findFatalXMLImportDialog(
    _ appAX: AXUIElement
) -> AXUIElement? {
    for dialog in candidateModalDialogs(appAX) {
        let text = dialogText(dialog)

        if text.contains(
            "xml document could not be imported"
        ) || text.contains(
            "the xml could not be imported"
        ) || (
            text.contains("could not be imported")
            && text.contains("xml")
        ) {
            return dialog
        }
    }

    return nil
}

func findXMLImportWarningDialog(
    _ appAX: AXUIElement
) -> AXUIElement? {
    for dialog in candidateModalDialogs(appAX) {
        let text = dialogText(dialog)

        if text.contains(
            "your xml was imported with the following warnings"
        ) || (
            text.contains("xml")
            && text.contains("imported")
            && text.contains("following warnings")
        ) {
            return dialog
        }
    }

    return nil
}

func settleXMLImportDialogs(
    appAX: AXUIElement,
    timeout: TimeInterval = 15
) throws {
    let deadline = Date().addingTimeInterval(
        timeout
    )

    var quietSince: Date?

    while Date() < deadline {
        if let fatal = findFatalXMLImportDialog(
            appAX
        ) {
            let text = dialogText(fatal)

            throw AXFailure(
                description:
                    "Final Cut reported fatal FCPXML import failure: "
                    + String(text.prefix(800))
            )
        }

        if let warning = findXMLImportWarningDialog(
            appAX
        ) {
            quietSince = nil

            log(
                "Dismissing recoverable Final Cut "
                + "FCPXML import warning"
            )

            guard let ok = findButton(
                warning,
                names: ["OK"]
            ) else {
                throw AXFailure(
                    description:
                        "Detected FCPXML import warning "
                        + "but could not find its OK button"
                )
            }

            try axPressAllowingModalTransition(
                ok,
                description:
                    "Dismiss FCPXML import warning"
            )

            _ = try waitUntil(
                timeout: 10,
                interval: 0.20,
                description:
                    "FCPXML import warning dismissal"
            ) {
                findXMLImportWarningDialog(
                    appAX
                ) == nil ? true : nil
            }

            continue
        }

        if quietSince == nil {
            quietSince = Date()
        }

        if let quietSince,
           Date().timeIntervalSince(
               quietSince
           ) >= 1.5 {
            return
        }

        RunLoop.current.run(
            until: Date().addingTimeInterval(
                0.20
            )
        )
    }

    if let fatal = findFatalXMLImportDialog(
        appAX
    ) {
        throw AXFailure(
            description:
                "Final Cut remained in fatal "
                + "FCPXML import state"
        )
    }

    if findXMLImportWarningDialog(appAX) != nil {
        throw AXFailure(
            description:
                "Timed out dismissing Final Cut "
                + "FCPXML import warning"
        )
    }
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
    var width: Int?
    var height: Int?
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
        case "--width": result.width = Int(value)
        case "--height": result.height = Int(value)
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
              let rawOutput = args.output,
              let expectedWidth = args.width,
              let expectedHeight = args.height else {
            throw AXFailure(description: "export-batch requires --xml, --event, --expected and --output")
        }
        let output = rawOutput.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !output.isEmpty else {
            throw AXFailure(description: "export-batch requires a non-empty --output directory")
        }
        guard expected > 0 else {
            throw AXFailure(description: "export-batch requires --expected > 0")
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

        try settleXMLImportDialogs(
            appAX: appAX
        )

        activate(app)

        // Re-select the exact newly imported event after
        // any import-warning modal has been dismissed.
        guard selectNamedElement(
            in: appAX,
            name: event
        ) else {
            throw AXFailure(
                description:
                    "Could not re-select imported event "
                    + "after resolving FCPXML dialogs: "
                    + event
            )
        }

        commandKey(18) // Command-1: focus Browser in Final Cut
        RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        commandKey(0)  // Command-A
        RunLoop.current.run(until: Date().addingTimeInterval(0.35))

        log("Ensuring Optimized/Original media before Share")
        try ensureOptimizedOriginalPlayback(appAX)
        activate(app)

        // Re-focus the Browser and restore the exact batch selection because
        // interacting with the Viewer View menu may move keyboard focus.
        commandKey(18) // Command-1
        RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        commandKey(0)  // Command-A
        RunLoop.current.run(until: Date().addingTimeInterval(0.35))

        log("Invoking batch Share for \(expected) project(s)")
        let shareTitle = try launchShareWhenReady(
            appAX: appAX,
            expectedCount: expected,
            destinationName: args.destination,
            eventName: event,
            expectedWidth: expectedWidth,
            expectedHeight: expectedHeight,
            copyTimeout: 900,
            retryDelay: 5
        )
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
