// SmartVoiceInput's own macOS Accessibility bridge.
import AppKit
import ApplicationServices
import Foundation
import Darwin

// Initialize AppKit before querying system accessibility focus.
let nativeApplication = NSApplication.shared
nativeApplication.setActivationPolicy(.prohibited)

// BEGIN UNICODE SCALAR COORDINATES
// Canonical positions are Unicode scalars (code points), not grapheme clusters.
// AX and NSString positions are UTF-16 and are converted only at their boundary.
func scalarTextEquals(_ actual: String, _ expected: String) -> Bool {
    return actual.unicodeScalars.elementsEqual(expected.unicodeScalars)
}
struct ScalarTextRange: Equatable {
    let location: Int
    let length: Int
}
func boundedRangeEnd(location: Int, length: Int, limit: Int) -> Int? {
    guard location >= 0, length >= 0, limit >= 0, location <= limit,
          length <= limit - location else { return nil }
    return location + length
}
func scalarRange(fromUTF16 range: CFRange, in text: String) -> ScalarTextRange? {
    guard let end = boundedRangeEnd(location: range.location, length: range.length,
                                    limit: text.utf16.count) else { return nil }
    var units = 0, scalars = 0
    var startScalar: Int? = range.location == 0 ? 0 : nil
    var endScalar: Int? = end == 0 ? 0 : nil
    for scalar in text.unicodeScalars {
        if startScalar != nil && endScalar != nil { break }
        units += scalar.value > 0xffff ? 2 : 1
        scalars += 1
        if units == range.location { startScalar = scalars }
        if units == end { endScalar = scalars }
        if units > end { break }
    }
    // An offset between the two UTF-16 units of a scalar never matches a boundary.
    guard let start = startScalar, let finish = endScalar else { return nil }
    return ScalarTextRange(location: start, length: finish - start)
}
func utf16Range(fromScalars range: ScalarTextRange, in text: String) -> CFRange? {
    guard let end = boundedRangeEnd(location: range.location, length: range.length,
                                    limit: text.unicodeScalars.count) else { return nil }
    var units = 0, scalars = 0
    var startUnit: Int? = range.location == 0 ? 0 : nil
    var endUnit: Int? = end == 0 ? 0 : nil
    for scalar in text.unicodeScalars {
        if startUnit != nil && endUnit != nil { break }
        units += scalar.value > 0xffff ? 2 : 1
        scalars += 1
        if scalars == range.location { startUnit = units }
        if scalars == end { endUnit = units }
    }
    guard let start = startUnit, let finish = endUnit else { return nil }
    return CFRange(location: start, length: finish - start)
}
struct ScalarInsertion {
    let expected: String
    let insertedRange: ScalarTextRange
    let caret: ScalarTextRange
}
func scalarInsertion(in original: String, replacing range: ScalarTextRange,
                     with text: String) -> ScalarInsertion? {
    guard let native = utf16Range(fromScalars: range, in: original) else { return nil }
    let expected = (original as NSString).replacingCharacters(
        in: NSRange(location: native.location, length: native.length), with: text)
    let inserted = ScalarTextRange(location: range.location, length: text.unicodeScalars.count)
    guard let end = boundedRangeEnd(location: inserted.location, length: inserted.length,
                                    limit: expected.unicodeScalars.count),
          utf16Range(fromScalars: inserted, in: expected) != nil else { return nil }
    let caret = ScalarTextRange(location: end, length: 0)
    guard utf16Range(fromScalars: caret, in: expected) != nil else { return nil }
    return ScalarInsertion(expected: expected, insertedRange: inserted, caret: caret)
}
func observedScalarFragment(in text: String, baseline: String,
                            insertedRange: ScalarTextRange) -> String? {
    guard let native = utf16Range(fromScalars: insertedRange, in: baseline) else { return nil }
    let base = baseline as NSString
    let prefix = base.substring(to: native.location)
    let suffix = base.substring(from: native.location + native.length)
    let current = text.unicodeScalars
    let prefixScalars = prefix.unicodeScalars, suffixScalars = suffix.unicodeScalars
    let count = current.count, prefixCount = prefixScalars.count, suffixCount = suffixScalars.count
    // The outside text must retain the same scalars, including normalization form.
    guard prefixCount <= count, suffixCount <= count - prefixCount,
          current.starts(with: prefixScalars),
          current.suffix(suffixCount).elementsEqual(suffixScalars) else { return nil }
    let range = ScalarTextRange(location: prefixCount, length: count - prefixCount - suffixCount)
    guard let fragment = utf16Range(fromScalars: range, in: text) else { return nil }
    return (text as NSString).substring(with: NSRange(location: fragment.location, length: fragment.length))
}
// END UNICODE SCALAR COORDINATES

// Process generation comes from a complete OS BSD-info read.
// Integer seconds/microseconds are kept exact; unknown or exiting processes reject.
struct ProcessGeneration: Equatable {
    let startSeconds: UInt64
    let startMicroseconds: UInt64
}
enum CapturedWriteRoute: Equatable {
    case pidPaste
    case unverifiedPaste
}
// BEGIN UNIVERSAL PASTE POLICY
// Metadata constrains known targets; missing text metadata is not a paste veto.
func pasteFrontMetadataAllowed(bundle: String, active: Bool, hidden: Bool,
                              terminated: Bool, isOwner: Bool) -> Bool {
    return !bundle.isEmpty && !bundle.contains(where: { $0.isWhitespace })
        && active && !hidden && !terminated && !isOwner
}
func pasteWindowMetadataAllowed(role: String, subrole: String,
                               enabled: Bool?, protected: Bool, readOnly: Bool) -> Bool {
    return role == "AXWindow" && subrole == "AXStandardWindow"
        && enabled != false && !protected && !readOnly
}
func pasteControlMetadataAllowed(role: String, subrole: String, enabled: Bool?,
                                protected: Bool, explicitlyReadOnly: Bool,
                                selectedSettable: Bool?, valueSettable: Bool?) -> Bool {
    guard subrole != "AXSecureTextField", enabled != false, !protected, !explicitlyReadOnly,
          !["AXButton", "AXCheckBox", "AXRadioButton", "AXMenu", "AXMenuBar", "AXMenuItem",
            "AXToolbar", "AXSheet", "AXSlider", "AXPopUpButton", "AXTabGroup"].contains(role),
          !["AXDialog", "AXSystemDialog"].contains(subrole) else { return false }
    // A standard text control explicitly declaring both writes unavailable is
    // read-only. AttributeUnsupported remains nil, never falsely read-only.
    let textRole = ["AXTextArea", "AXTextField", "AXComboBox"].contains(role)
    return !(textRole && selectedSettable == false && valueSettable == false)
}
func pasteAXFallbackAllowed(_ error: Int32) -> Bool {
    return [AXError.noValue.rawValue, AXError.attributeUnsupported.rawValue,
            AXError.notImplemented.rawValue].contains(error)
}
// END UNIVERSAL PASTE POLICY
func osProcessGeneration(for pid: pid_t) -> ProcessGeneration? {
    guard pid > 0, let capacity = Int32(exactly: MemoryLayout<proc_bsdinfo>.size) else { return nil }
    var info = proc_bsdinfo()
    let returned = proc_pidinfo(pid, Int32(PROC_PIDTBSDINFO), 0, &info, capacity)
    guard returned == capacity, info.pbi_pid == UInt32(pid),
          info.pbi_status == UInt32(SRUN) || info.pbi_status == UInt32(SSLEEP) || info.pbi_status == UInt32(SSTOP),
          (info.pbi_flags & UInt32(PROC_FLAG_INEXIT)) == 0,
          info.pbi_start_tvsec > 0, info.pbi_start_tvusec < 1_000_000 else { return nil }
    return ProcessGeneration(startSeconds: info.pbi_start_tvsec, startMicroseconds: info.pbi_start_tvusec)
}

struct Target {
    let element: AXUIElement
    let window: AXUIElement
    let pid: pid_t
    let launched: Date
    let generation: ProcessGeneration
    let writeRoute: CapturedWriteRoute
    let range: ScalarTextRange?
    let original: String?
    var inserted: String?
    var after: String?
}
var targets: [String: Target] = [:]
let ownerPid = getppid()

func attribute(_ element: AXUIElement, _ key: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, key as CFString, &value) == .success ? value : nil
}
func axElement(_ raw: CFTypeRef?) -> AXUIElement? {
    guard let raw = raw, CFGetTypeID(raw) == AXUIElementGetTypeID() else { return nil }
    return (raw as! AXUIElement)
}
// This check must run before EVERY AXValue read, including insertion rechecks and observation.
// Role/protected state may change while a control keeps the same AX identity.
func safeTextControl(_ element: AXUIElement) -> Bool {
    let role = attribute(element, kAXRoleAttribute) as? String ?? ""
    let subrole = attribute(element, kAXSubroleAttribute) as? String ?? ""
    guard [kAXTextAreaRole, kAXTextFieldRole, kAXComboBoxRole].contains(role),
          subrole != kAXSecureTextFieldSubrole,
          (attribute(element, NSAccessibility.Attribute.containsProtectedContent.rawValue) as? Bool) != true else { return false }
    return true
}
func writable(_ element: AXUIElement, _ key: String) -> Bool {
    var settable: DarwinBoolean = false
    return AXUIElementIsAttributeSettable(element, key as CFString, &settable) == .success && settable.boolValue
}
func editable(_ element: AXUIElement) -> Bool {
    guard safeTextControl(element), (attribute(element, kAXEnabledAttribute) as? Bool) != false else { return false }
    return writable(element, kAXSelectedTextAttribute) || writable(element, kAXValueAttribute)
}
func value(_ element: AXUIElement) -> String? {
    guard safeTextControl(element), let text = attribute(element, kAXValueAttribute) as? String,
          text.utf16.count <= 200_000 else { return nil }
    return text
}
func selection(_ element: AXUIElement) -> CFRange? {
    guard let raw = attribute(element, kAXSelectedTextRangeAttribute), CFGetTypeID(raw) == AXValueGetTypeID() else { return nil }
    var range = CFRange()
    guard AXValueGetValue(raw as! AXValue, .cfRange, &range) else { return nil }
    return range
}
// Opt-in local acceptance diagnostics: numeric metadata only, never input text,
// clipboard contents, user identity, or result text. Disabled in normal runs.
func traceSnapshotMetadata(_ stage: String, element: AXUIElement, text: String?,
                           range: CFRange?, expectedUTF16: Int? = nil) {
    guard let path = ProcessInfo.processInfo.environment["SVI_NATIVE_SNAPSHOT_DIAGNOSTICS"],
          path.hasPrefix("/private/tmp/svi-r2-"),
          path.hasSuffix("/native-snapshot-metadata.jsonl"), safeTextControl(element) else { return }
    var record: [String: Any] = ["event": stage, "unix": Date().timeIntervalSince1970,
        "text_utf16": text?.utf16.count ?? -1,
        "text_scalars": text?.unicodeScalars.count ?? -1,
        "range_location_utf16": range?.location ?? -1,
        "range_length_utf16": range?.length ?? -1,
        "declared_characters": (attribute(element, kAXNumberOfCharactersAttribute) as? NSNumber)?.intValue ?? -1,
        "selected_text_utf16": (attribute(element, kAXSelectedTextAttribute) as? String)?.utf16.count ?? -1]
    if let expectedUTF16 = expectedUTF16 { record["expected_utf16"] = expectedUTF16 }
    if let text = text, let range = range,
       boundedRangeEnd(location: range.location, length: range.length, limit: text.utf16.count) != nil,
       let selected = attribute(element, kAXSelectedTextAttribute) as? String {
        record["selected_text_matches_range"] = scalarTextEquals(selected,
            (text as NSString).substring(with: NSRange(location: range.location, length: range.length)))
    }
    guard var bytes = try? JSONSerialization.data(withJSONObject: record, options: [.sortedKeys]) else { return }
    bytes.append(10)
    let descriptor = open(path, O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW, S_IRUSR | S_IWUSR)
    guard descriptor >= 0 else { return }
    defer { close(descriptor) }
    bytes.withUnsafeBytes { buffer in
        guard let base = buffer.baseAddress else { return }
        _ = Darwin.write(descriptor, base, buffer.count)
    }
}
func focus() -> AXUIElement? {
    return axElement(attribute(AXUIElementCreateSystemWide(), kAXFocusedUIElementAttribute))
}
// Opt-in failure localization only. No candidate PID from this probe authorizes
// capture or writing, and no text/title/clipboard or identity is recorded.
func traceFrontmostMetadata(axError: AXError, axPIDError: AXError, axPID: pid_t) {
    guard let path = ProcessInfo.processInfo.environment["SVI_NATIVE_SNAPSHOT_DIAGNOSTICS"],
          path.hasPrefix("/private/tmp/svi-r2-"),
          path.hasSuffix("/native-snapshot-metadata.jsonl") else { return }
    let workspace = NSWorkspace.shared
    let cachedPID = workspace.frontmostApplication?.processIdentifier ?? 0
    // Refresh only this opt-in diagnostic's observation. It never supplies a
    // target PID to production capture, and does not activate any app/window.
    let loopResult = CFRunLoopRunInMode(CFRunLoopMode.defaultMode, 0.005, true)
    let observed = workspace.frontmostApplication
    let livePID = observed?.processIdentifier ?? 0
    var record: [String: Any] = ["event": "frontmost-probe", "unix": Date().timeIntervalSince1970,
        "ax_error": axError.rawValue, "ax_pid_error": axPIDError.rawValue, "ax_pid": axPID,
        "workspace_cached_pid": cachedPID, "workspace_after_loop_pid": livePID,
        "loop_result": loopResult.rawValue, "main_thread": Thread.isMainThread,
        "trusted": AXIsProcessTrusted()]
    // Only inspect structural attributes of the explicitly tested WeChat app.
    // The AppKit observation is not a fallback or new paste exception. Its
    // cached/lifecycle limitations are retained explicitly in the metadata.
    if livePID > 0, observed?.bundleIdentifier == "com.tencent.xinWeChat" {
        record["candidate_is_wechat"] = true
        let app = AXUIElementCreateApplication(livePID)
        AXUIElementSetMessagingTimeout(app, 0.5)
        var focused: CFTypeRef?, window: CFTypeRef?, systemFocused: CFTypeRef?
        let focusedError = AXUIElementCopyAttributeValue(app, kAXFocusedUIElementAttribute as CFString, &focused)
        let windowError = AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute as CFString, &window)
        let system = AXUIElementCreateSystemWide()
        AXUIElementSetMessagingTimeout(system, 0.5)
        let systemError = AXUIElementCopyAttributeValue(system, kAXFocusedUIElementAttribute as CFString, &systemFocused)
        record["app_focused_element_error"] = focusedError.rawValue
        record["app_focused_window_error"] = windowError.rawValue
        record["system_focused_element_error"] = systemError.rawValue
        if focusedError == .success, let element = axElement(focused) {
            record["focused_element_pid_matches"] = belongsToProcess(element, livePID)
            record["focused_element_safe_text_role"] = safeTextControl(element)
            record["focused_element_editable"] = editable(element)
        }
        if windowError == .success, let element = axElement(window) {
            record["focused_window_pid_matches"] = belongsToProcess(element, livePID)
            record["focused_window_standard"] = (attribute(element, kAXSubroleAttribute) as? String) == kAXStandardWindowSubrole
        }
    } else { record["candidate_is_wechat"] = false }
    guard var bytes = try? JSONSerialization.data(withJSONObject: record, options: [.sortedKeys]) else { return }
    bytes.append(10)
    let descriptor = open(path, O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW, S_IRUSR | S_IWUSR)
    guard descriptor >= 0 else { return }
    defer { close(descriptor) }
    bytes.withUnsafeBytes { buffer in
        guard let base = buffer.baseAddress else { return }
        _ = Darwin.write(descriptor, base, buffer.count)
    }
}
// Refresh AppKit before every observation. A remembered recording-time app
// or visually topmost window must never become the paste destination.
func refreshedFrontmostApplication() -> NSRunningApplication? {
    guard Thread.isMainThread else { return nil }
    let workspace = NSWorkspace.shared
    _ = workspace.frontmostApplication
    let refreshed = CFRunLoopRunInMode(CFRunLoopMode.defaultMode, 0.005, false)
    guard refreshed == .timedOut || refreshed == .handledSource,
          let application = workspace.frontmostApplication,
          pasteFrontMetadataAllowed(bundle: application.bundleIdentifier ?? "",
            active: application.isActive, hidden: application.isHidden,
            terminated: application.isTerminated, isOwner: false),
          application.processIdentifier > 0 else { return nil }
    return application
}
func frontmostPID() -> pid_t? {
    // AppKit knows the keyboard-receiving application independently of an
    // application's optional focused-input AX implementation.
    let fresh = refreshedFrontmostApplication()
    let system = AXUIElementCreateSystemWide()
    AXUIElementSetMessagingTimeout(system, 0.5)
    var raw: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(system, kAXFocusedApplicationAttribute as CFString, &raw)
    var pid: pid_t = 0
    let application = error == .success ? axElement(raw) : nil
    let pidError = application.map { AXUIElementGetPid($0, &pid) } ?? AXError.failure
    traceFrontmostMetadata(axError: error, axPIDError: pidError, axPID: pid)
    if error == .success {
        guard application != nil, pidError == .success, pid > 0 else { return nil }
        // Disagreement means focus moved or AppKit did not refresh: do not guess.
        if let fresh = fresh, fresh.processIdentifier != pid { return nil }
        return pid
    }
    guard pasteAXFallbackAllowed(error.rawValue), let fresh = fresh else { return nil }
    return fresh.processIdentifier
}
func belongsToProcess(_ element: AXUIElement, _ expected: pid_t) -> Bool {
    var pid: pid_t = 0
    return AXUIElementGetPid(element, &pid) == .success && pid == expected
}

func focusedElement(for pid: pid_t) -> AXUIElement? {
    // Preserve the app-scoped fallback needed by real macOS controls.
    return axElement(attribute(AXUIElementCreateApplication(pid), kAXFocusedUIElementAttribute)) ?? focus()
}
func sameProcess(_ target: Target) -> Bool {
    guard let generation = osProcessGeneration(for: target.pid) else { return false }
    return generation == target.generation
}
func settableState(_ element: AXUIElement, _ key: String) -> Bool? {
    var settable: DarwinBoolean = false
    guard AXUIElementIsAttributeSettable(element, key as CFString, &settable) == .success else { return nil }
    return settable.boolValue
}
func pasteControlAllowed(_ element: AXUIElement) -> Bool {
    let role = attribute(element, kAXRoleAttribute) as? String ?? ""
    let isText = [kAXTextAreaRole, kAXTextFieldRole, kAXComboBoxRole].contains(role)
    return pasteControlMetadataAllowed(role: role,
        subrole: attribute(element, kAXSubroleAttribute) as? String ?? "",
        enabled: attribute(element, kAXEnabledAttribute) as? Bool,
        protected: attribute(element, NSAccessibility.Attribute.containsProtectedContent.rawValue) as? Bool == true,
        explicitlyReadOnly: attribute(element, "AXEditable") as? Bool == false || attribute(element, "AXReadOnly") as? Bool == true,
        selectedSettable: isText ? settableState(element, kAXSelectedTextAttribute) : nil,
        valueSettable: isText ? settableState(element, kAXValueAttribute) : nil)
}
func currentPasteWindow(for pid: pid_t, element: AXUIElement?) -> AXUIElement? {
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.5)
    guard let window = axElement(attribute(app, kAXFocusedWindowAttribute)), belongsToProcess(window, pid),
          pasteWindowMetadataAllowed(role: attribute(window, kAXRoleAttribute) as? String ?? "",
            subrole: attribute(window, kAXSubroleAttribute) as? String ?? "",
            enabled: attribute(window, kAXEnabledAttribute) as? Bool,
            protected: attribute(window, NSAccessibility.Attribute.containsProtectedContent.rawValue) as? Bool == true,
            readOnly: attribute(window, "AXEditable") as? Bool == false || attribute(window, "AXReadOnly") as? Bool == true)
    else { return nil }
    if let element = element {
        guard belongsToProcess(element, pid), pasteControlAllowed(element) else { return nil }
        if let controlWindow = axElement(attribute(element, kAXWindowAttribute)), !CFEqual(controlWindow, window) { return nil }
        // Check exposed ancestors without requiring every opaque custom view to
        // implement AXParent. Never read any value while doing this safety pass.
        var node: AXUIElement? = element
        for _ in 0..<16 {
            guard let current = node else { break }
            if CFEqual(current, window) { break }
            guard pasteControlAllowed(current) else { return nil }
            node = axElement(attribute(current, kAXParentAttribute))
        }
    }
    return window
}
func pasteTargetMatches(_ target: Target) -> Bool {
    guard sameProcess(target), frontmostPID() == target.pid,
          let application = NSRunningApplication(processIdentifier: target.pid),
          application.launchDate == target.launched,
          pasteFrontMetadataAllowed(bundle: application.bundleIdentifier ?? "",
            active: application.isActive, hidden: application.isHidden,
            terminated: application.isTerminated, isOwner: target.pid == ownerPid),
          let window = currentPasteWindow(for: target.pid, element: focusedElement(for: target.pid))
    else { return false }
    return CFEqual(window, target.window)
}
func matches(_ target: Target) -> Bool {
    guard pasteTargetMatches(target) else { return false }
    if target.writeRoute == .unverifiedPaste { return true }
    guard
          let current = focusedElement(for: target.pid), CFEqual(current, target.element),
          belongsToProcess(current, target.pid),
          let window = axElement(attribute(current, kAXWindowAttribute)) else { return false }
    return CFEqual(window, target.window)
}
func snapshotMatches(_ target: Target) -> Bool {
    if target.writeRoute == .unverifiedPaste { return pasteTargetMatches(target) }
    guard let original = target.original, let range = target.range else { return false }
    return snapshotMatches(target, expectedValue: original, expectedRange: range)
}
func snapshotMatches(_ target: Target, expectedValue: String, expectedRange: ScalarTextRange) -> Bool {
    guard matches(target), pasteControlAllowed(target.element), let actual = value(target.element),
          scalarTextEquals(actual, expectedValue), let native = selection(target.element),
          let range = scalarRange(fromUTF16: native, in: expectedValue) else { return false }
    return range == expectedRange
}
func restoreIfAllowed(_ target: Target, requested: Bool) -> Bool {
    if matches(target) { return true }
    // Opaque targets never authorize restoring a background window.
    if target.writeRoute == .unverifiedPaste { return false }
    // Explicit UI buttons may focus our own application. Never steal focus from a
    // third-party B to captured A, and never restore a replaced process/window.
    guard requested, sameProcess(target), frontmostPID() == ownerPid,
          let application = NSRunningApplication(processIdentifier: target.pid),
          application.launchDate == target.launched, !application.isTerminated,
          sameProcess(target), application.activate(options: []) else { return false }
    for _ in 0..<10 {
        if matches(target) { return true }
        Thread.sleep(forTimeInterval: 0.02)
    }
    return matches(target)
}
func fail(_ message: String, _ code: String = "NATIVE_REJECTED", effect: String = "none") -> [String: Any] {
    return ["error": ["code": code, "message": message, "effect": effect]]
}
func invalidTarget(_ message: String) -> [String: Any] {
    return ["valid": false, "effect": "none", "code": "TARGET_CHANGED", "message": message]
}
// The SDK constant has raw value AXTextInputMarkedRange. Use that same key
// directly so older SDKs can compile; unsupported runtime attributes stay unknown.
// SDK: NSAccessibilityConstants.h declares a marked NSRange. Cross-process AX
// normally carries ranges as AXValue(CFRange); do not infer "committed" from a
// missing attribute, noValue, a stable string, or an unverified zero-length range.
func composition(_ element: AXUIElement) -> (String, String) {
    var raw: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element,
        "AXTextInputMarkedRange" as CFString, &raw)
    guard error == .success, let raw = raw else {
        if error == .noValue { return ("unknown", "ax-marked-range-no-value") }
        if error == .attributeUnsupported { return ("unknown", "ax-marked-range-unsupported") }
        return ("unknown", "ax-marked-range-error-\(error.rawValue)")
    }
    var range: CFRange?
    if CFGetTypeID(raw) == AXValueGetTypeID() {
        let boxed = raw as! AXValue
        var found = CFRange()
        if AXValueGetType(boxed) == .cfRange && AXValueGetValue(boxed, .cfRange, &found) { range = found }
    } else if let boxed = raw as? NSValue,
              strcmp(boxed.objCType, NSValue(range: NSRange(location: 0, length: 0)).objCType) == 0 {
        let found = boxed.rangeValue
        range = CFRange(location: found.location, length: found.length)
    }
    guard let range = range else { return ("unknown", "ax-marked-range-invalid-type") }
    if range.length == 0 && (range.location == kCFNotFound || range.location == NSNotFound) {
        return ("committed", "ax-marked-range-not-found")
    }
    if range.location >= 0 && range.location != NSNotFound && range.length > 0 {
        return ("composing", "ax-marked-range-active")
    }
    return ("unknown", "ax-marked-range-ambiguous")
}
// HUD geometry only. Never reads AXValue/selected text, captures a write target,
// activates an app, or authorizes insertion. Main matches the native global bounds to a display.
func hudRectangle(_ element: AXUIElement) -> CGRect? {
    guard let position = attribute(element, kAXPositionAttribute),
          let size = attribute(element, kAXSizeAttribute),
          CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero, extent = CGSize.zero
    guard AXValueGetValue(position as! AXValue, .cgPoint, &point),
          AXValueGetValue(size as! AXValue, .cgSize, &extent) else { return nil }
    return CGRect(origin: point, size: extent)
}
func hudPositionResponse(_ rect: CGRect, source: String, pid: pid_t) -> [String: Any]? {
    guard rect.origin.x.isFinite, rect.origin.y.isFinite,
          rect.width.isFinite, rect.height.isFinite, rect.width >= 0, rect.height > 0,
          frontmostPID() == pid else { return nil }
    return ["available": true, "source": source, "bounds": [
        "x": Double(rect.origin.x), "y": Double(rect.origin.y),
        "width": Double(max(1, rect.width)), "height": Double(rect.height)]]
}
func currentInputBounds() -> [String: Any] {
    guard AXIsProcessTrusted(), let pid = frontmostPID() else { return ["available": false] }
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.05)
    let element = axElement(attribute(app, kAXFocusedUIElementAttribute))
    if let element = element, belongsToProcess(element, pid) {
        AXUIElementSetMessagingTimeout(element, 0.05)
        if safeTextControl(element), var range = selection(element), range.location >= 0,
           range.location != NSNotFound {
            // The selection's first insertion point identifies its screen even
            // when a multiline selection spans several displays.
            range.length = 0
            if let parameter = AXValueCreate(.cfRange, &range) {
                var raw: CFTypeRef?
                if AXUIElementCopyParameterizedAttributeValue(element,
                    kAXBoundsForRangeParameterizedAttribute as CFString, parameter, &raw) == .success,
                   let raw = raw, CFGetTypeID(raw) == AXValueGetTypeID() {
                    var rect = CGRect.zero
                    if AXValueGetValue(raw as! AXValue, .cgRect, &rect),
                       let result = hudPositionResponse(rect, source: "caret", pid: pid) { return result }
                }
            }
        }
        if let rect = hudRectangle(element), let result = hudPositionResponse(rect, source: "control", pid: pid) { return result }
    }
    if let window = axElement(attribute(app, kAXFocusedWindowAttribute)), belongsToProcess(window, pid) {
        AXUIElementSetMessagingTimeout(window, 0.05)
        if let rect = hudRectangle(window), let result = hudPositionResponse(rect, source: "window", pid: pid) { return result }
    }
    return ["available": false]
}

func confirmTextAndCaret(_ target: Target, expected: String, caret: ScalarTextRange) -> Bool {
    // Verification must never manufacture success by moving the application's
    // actual caret to the AX-derived expectation after paste.
    return snapshotMatches(target, expectedValue: expected, expectedRange: caret)
}

func command(_ data: [String: Any]) -> [String: Any] {
    let action = data["action"] as? String ?? ""
    if action == "keyDown" {
        guard let key = data["keyCode"] as? Int, key >= 0, key <= 127 else { return ["down": false] }
        return ["down": CGEventSource.keyState(.combinedSessionState, key: CGKeyCode(key))]
    }
    if action == "permission" {
        let ask = data["prompt"] as? Bool ?? false
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: ask] as CFDictionary
        return ["trusted": AXIsProcessTrustedWithOptions(options), "supported": true]
    }
    if action == "release" {
        if let id = data["target"] as? String { targets.removeValue(forKey: id) }
        else { targets.removeAll() }
        return ["released": true]
    }
    if action == "inputBounds" { return currentInputBounds() }
    guard AXIsProcessTrusted() else { return fail("外部输入需要辅助功能权限，请在语音设置中授权", "PERMISSION_REQUIRED") }
    if action == "insertCurrent" {
        // The insertion claim and durable once-only intent are already settled
        // by Main. Obtain the final caret and write in this same helper request;
        // never restore the earlier result-time application or selection.
        let captured = command(["action": "capture"])
        guard let id = captured["target"] as? String else { return captured }
        var request = data
        request["action"] = "insert"
        request["target"] = id
        request["restoreFocus"] = false
        var result = command(request)
        if result["error"] != nil {
            targets.removeValue(forKey: id)
        } else {
            result["target"] = id
            result["app"] = captured["app"]
        }
        return result
    }
    if action == "capture" {
        targets.removeAll()
        // Keep each identity/focus check fail-closed, but distinguish its
        // actionable failure. A combined guard hid whether a real input field
        // was missing or the OS could not establish the application's identity.
        guard let pid = frontmostPID() else {
            return fail("无法确定当前前台应用，请点击目标输入框后重试", "UNSUPPORTED_TARGET")
        }
        guard let generation = osProcessGeneration(for: pid) else {
            return fail("无法核验当前应用的运行状态，请重新选择目标", "UNSUPPORTED_TARGET")
        }
        guard let application = NSRunningApplication(processIdentifier: pid) else {
            return fail("无法读取当前目标应用的信息，请重新选择目标", "UNSUPPORTED_TARGET")
        }
        guard let launched = application.launchDate else {
            return fail("无法确认目标应用的启动状态，请重新选择目标", "UNSUPPORTED_TARGET")
        }
        guard let bundle = application.bundleIdentifier, !bundle.isEmpty,
              bundle.rangeOfCharacter(from: .whitespacesAndNewlines) == nil else {
            return fail("当前目标应用无法可靠标识，请复制结果", "UNSUPPORTED_TARGET")
        }
        guard pasteFrontMetadataAllowed(bundle: bundle, active: application.isActive,
                  hidden: application.isHidden, terminated: application.isTerminated, isOwner: pid == ownerPid) else {
            return fail("当前应用不支持自动粘贴，结果已保留供复制", "TARGET_UNSUPPORTED")
        }
        let element = focusedElement(for: pid)
        guard let window = currentPasteWindow(for: pid, element: element) else {
            return fail("当前窗口无法确认或目标明确受限，结果已保留供复制", "UNSUPPORTED_TARGET")
        }
        // Read-back/learning snapshots are optional; never read a protected value.
        let text = element.flatMap { value($0) }
        let nativeRange = text == nil ? nil : element.flatMap { selection($0) }
        if let element = element { traceSnapshotMetadata("capture", element: element, text: text, range: nativeRange) }
        let range = text.flatMap { text in nativeRange.flatMap { scalarRange(fromUTF16: $0, in: text) } }
        let completeSnapshot = text != nil && range != nil
        guard osProcessGeneration(for: pid) == generation else {
            return fail("输入目标进程已改变，请重新选择位置", "TARGET_CHANGED")
        }
        let id = UUID().uuidString
        // For opaque targets element is only a window identity placeholder. The
        // unverified route never reads/writes/observes it as a text input.
        let target = Target(element: element ?? window, window: window, pid: pid, launched: launched,
                            generation: generation, writeRoute: completeSnapshot ? .pidPaste : .unverifiedPaste,
                            range: completeSnapshot ? range : nil, original: completeSnapshot ? text : nil)
        guard pasteTargetMatches(target) else { return fail("前台目标已改变，请重新选择位置", "TARGET_CHANGED") }
        targets[id] = target
        return ["target": id, "app": application.localizedName ?? "目标应用", "observable": completeSnapshot]
    }

    guard let id = data["target"] as? String, var target = targets[id] else {
        if action == "validate" { return invalidTarget("原输入目标已过期") }
        return fail("原输入目标已过期", "TARGET_EXPIRED")
    }
    if action == "validate" {
        guard target.inserted == nil,
              restoreIfAllowed(target, requested: data["restoreFocus"] as? Bool == true), snapshotMatches(target) else {
            return invalidTarget("输入位置或内容已改变，请重新选择位置")
        }
        return ["valid": true, "effect": "none"]
    }
    if action == "insert" {
        guard target.inserted == nil else { return fail("该目标已有写入尝试，不会再次插入", "ALREADY_ATTEMPTED", effect: "unknown") }
        guard let text = data["text"] as? String, !text.isEmpty, text.utf16.count <= 32_000 else {
            return fail("输入内容为空或过长", "INVALID_TEXT")
        }
        guard restoreIfAllowed(target, requested: data["restoreFocus"] as? Bool == true), snapshotMatches(target) else {
            return fail("输入位置或内容已改变，结果已保留，请重新选择位置", "TARGET_CHANGED")
        }
        let insertion = target.original.flatMap { original in target.range.flatMap { scalarInsertion(in: original, replacing: $0, with: text) } }
        let pasteboard = NSPasteboard.general
        let priorChange = pasteboard.changeCount
        var snapshotComplete = true
        let savedClipboard = (pasteboard.pasteboardItems ?? []).map { item in
            item.types.compactMap { type -> (NSPasteboard.PasteboardType, Data)? in
                guard let bytes = item.data(forType: type) else { snapshotComplete = false; return nil }
                return (type, bytes)
            }
        }
        guard snapshotComplete, pasteboard.changeCount == priorChange else {
            return fail("剪贴板已改变或无法保存，请复制结果", "CLIPBOARD_UNAVAILABLE")
        }
        let source = CGEventSource(stateID: .combinedSessionState)
        guard let down = CGEvent(keyboardEventSource: source, virtualKey: 9, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: 9, keyDown: false) else {
            return fail("无法创建输入事件，请复制结果", "INPUT_EVENT_UNAVAILABLE")
        }
        guard snapshotMatches(target), pasteboard.changeCount == priorChange else {
            return fail("目标或剪贴板已改变，请复制结果", "TARGET_CHANGED")
        }
        pasteboard.clearContents()
        let clipboardReady = pasteboard.setString(text, forType: .string)
        let ownChange = pasteboard.changeCount
        if clipboardReady && snapshotMatches(target) && pasteboard.changeCount == ownChange {
            // Durable Main intent is already written; mark the native target
            // before crossing the one and only text-write boundary.
            target.inserted = text
            targets[id] = target
            down.flags = .maskCommand; up.flags = .maskCommand
            down.postToPid(target.pid); up.postToPid(target.pid)
        }
        if target.inserted != nil { Thread.sleep(forTimeInterval: 0.5) }
        if pasteboard.changeCount == ownChange {
            pasteboard.clearContents()
            let items = savedClipboard.map { saved -> NSPasteboardItem in
                let item = NSPasteboardItem()
                for (type, bytes) in saved { item.setData(bytes, forType: type) }
                return item
            }
            if !items.isEmpty { pasteboard.writeObjects(items) }
        }
        guard target.inserted != nil else { return fail("未能安全发送粘贴，结果已保留供复制", "PASTE_NOT_SENT") }
        let verified = insertion.map { confirmTextAndCaret(target, expected: $0.expected, caret: $0.caret) } ?? false
        if let insertion = insertion, verified { target.after = insertion.expected }
        if let insertion = insertion {
            traceSnapshotMetadata(verified ? "verified" : "unverified", element: target.element,
                text: value(target.element), range: selection(target.element), expectedUTF16: insertion.expected.utf16.count)
        }
        targets[id] = target
        return ["status": verified ? "inserted" : "unverified", "effect": verified ? "written" : "unknown",
                "code": verified ? "INSERTED" : "WRITE_UNVERIFIED", "observable": verified,
                "method": "pid-paste", "caret_verified": verified,
                "message": verified ? "已输入" : "已尝试，未核验；请检查目标，不会自动重试"]
    }

    if action == "observe" {
        if target.writeRoute == .unverifiedPaste { return ["valid": false, "reason": "not_observable", "composition": "unknown"] }
        guard matches(target) else { return ["valid": false, "reason": "target_changed", "composition": "unknown"] }
        guard let text = value(target.element), let inserted = target.inserted, let baseline = target.after else {
            return ["valid": false, "reason": "not_observable", "composition": "unknown"]
        }
        guard let range = target.range else { return ["valid": false, "reason": "not_observable", "composition": "unknown"] }
        let insertedRange = ScalarTextRange(location: range.location, length: inserted.unicodeScalars.count)
        guard let fragment = observedScalarFragment(in: text, baseline: baseline, insertedRange: insertedRange) else {
            return ["valid": false, "reason": "outside_changed", "composition": "unknown"]
        }
        guard fragment.unicodeScalars.count <= inserted.unicodeScalars.count + 256 else {
            return ["valid": false, "reason": "fragment_too_long", "composition": "unknown"]
        }
        let (state, evidence) = composition(target.element)
        return ["valid": true, "text": fragment, "composition": state, "compositionEvidence": evidence]
    }
    return fail("未知输入桥命令", "UNKNOWN_ACTION")
}

while let line = readLine() {
    autoreleasepool {
        var request: [String: Any] = [:]
        var response: [String: Any]
        do {
            guard line.utf8.count <= 1024 * 1024, let bytes = line.data(using: .utf8),
                  let parsed = try JSONSerialization.jsonObject(with: bytes) as? [String: Any] else {
                throw NSError(domain: "SmartVoiceInputVoice", code: 1)
            }
            request = parsed
            response = command(request)
        } catch { response = fail("输入桥请求无效", "INVALID_REQUEST") }
        response["id"] = request["id"]
        if let bytes = try? JSONSerialization.data(withJSONObject: response), let output = String(data: bytes, encoding: .utf8) {
            print(output)
            fflush(stdout)
        }
    }
}
