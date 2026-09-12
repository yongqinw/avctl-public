import ApplicationServices
import CoreGraphics
import Darwin
import Foundation

private let environment = ProcessInfo.processInfo.environment
private let socketPath = (environment["AVCTL_MINI_SOCKET"] as NSString?)?
    .expandingTildeInPath
    ?? (NSHomeDirectory() as NSString).appendingPathComponent(
        ".avctl/mac-input.sock")

private let keyCodes: [String: CGKeyCode] = [
    "KeyA": 0, "KeyS": 1, "KeyD": 2, "KeyF": 3, "KeyH": 4,
    "KeyG": 5, "KeyZ": 6, "KeyX": 7, "KeyC": 8, "KeyV": 9,
    "KeyB": 11, "KeyQ": 12, "KeyW": 13, "KeyE": 14, "KeyR": 15,
    "KeyY": 16, "KeyT": 17, "Digit1": 18, "Digit2": 19,
    "Digit3": 20, "Digit4": 21, "Digit6": 22, "Digit5": 23,
    "Digit9": 25, "Digit7": 26, "Digit8": 28, "Digit0": 29,
    "KeyO": 31, "KeyU": 32, "KeyI": 34, "KeyP": 35,
    "Enter": 36, "KeyL": 37, "KeyJ": 38, "KeyK": 40,
    "KeyN": 45, "KeyM": 46, "Tab": 48, "Space": 49,
    "Backspace": 51, "Escape": 53, "MetaLeft": 55,
    "ShiftLeft": 56, "AltLeft": 58, "ControlLeft": 59,
    "Home": 115, "PageUp": 116, "Delete": 117, "End": 119,
    "PageDown": 121, "ArrowLeft": 123, "ArrowRight": 124,
    "ArrowDown": 125, "ArrowUp": 126,
]

private let modifierFlags: [String: CGEventFlags] = [
    "MetaLeft": .maskCommand,
    "AltLeft": .maskAlternate,
    "ControlLeft": .maskControl,
    "ShiftLeft": .maskShift,
]

private final class InputController: @unchecked Sendable {
    private let queue = DispatchQueue(label: "com.avctl.input-helper.events")
    private let source = CGEventSource(stateID: .privateState)
    private var heldKeys = Set<String>()
    private var heldButtons = Set<String>()

    private func flags() -> CGEventFlags {
        heldKeys.reduce(into: CGEventFlags()) { value, code in
            if let flag = modifierFlags[code] { value.insert(flag) }
        }
    }

    private func pointerLocation() -> CGPoint {
        CGEvent(source: nil)?.location ?? .zero
    }

    private func displayBounds() -> CGRect {
        var count: UInt32 = 0
        guard CGGetOnlineDisplayList(0, nil, &count) == .success, count > 0 else {
            return CGDisplayBounds(CGMainDisplayID())
        }
        var displays = Array(repeating: CGDirectDisplayID(), count: Int(count))
        guard CGGetOnlineDisplayList(count, &displays, &count) == .success else {
            return CGDisplayBounds(CGMainDisplayID())
        }
        return displays.prefix(Int(count)).reduce(CGRect.null) {
            $0.union(CGDisplayBounds($1))
        }
    }

    private func postKey(_ code: String, down: Bool) {
        guard let keyCode = keyCodes[code],
              let event = CGEvent(keyboardEventSource: source,
                                  virtualKey: keyCode, keyDown: down) else { return }
        if down { heldKeys.insert(code) } else { heldKeys.remove(code) }
        event.flags = flags()
        event.post(tap: .cghidEventTap)
    }

    private func switchSpace(_ direction: String) {
        // Mission Control's stable keyboard equivalent of a three-finger
        // horizontal swipe. Preserve a Control key the remote already holds.
        let controlWasHeld = heldKeys.contains("ControlLeft")
        if !controlWasHeld { postKey("ControlLeft", down: true) }
        let arrow = direction == "previous" ? "ArrowLeft" : "ArrowRight"
        postKey(arrow, down: true)
        postKey(arrow, down: false)
        if !controlWasHeld { postKey("ControlLeft", down: false) }
    }

    func handle(_ message: [String: Any]) {
        queue.async { [self] in
            guard CGPreflightPostEventAccess(),
                  let type = message["type"] as? String else { return }
            switch type {
            case "move":
                guard let dx = (message["dx"] as? NSNumber)?.doubleValue,
                      let dy = (message["dy"] as? NSNumber)?.doubleValue else { return }
                let current = pointerLocation()
                let bounds = displayBounds()
                let target = CGPoint(
                    x: min(max(current.x + dx, bounds.minX), bounds.maxX - 1),
                    y: min(max(current.y + dy, bounds.minY), bounds.maxY - 1))
                let eventType: CGEventType
                let button: CGMouseButton
                if heldButtons.contains("left") {
                    eventType = .leftMouseDragged
                    button = .left
                } else if heldButtons.contains("right") {
                    eventType = .rightMouseDragged
                    button = .right
                } else {
                    eventType = .mouseMoved
                    button = .left
                }
                CGEvent(mouseEventSource: source, mouseType: eventType,
                        mouseCursorPosition: target, mouseButton: button)?
                    .post(tap: .cghidEventTap)
            case "scroll":
                guard let dx = (message["dx"] as? NSNumber)?.doubleValue,
                      let dy = (message["dy"] as? NSNumber)?.doubleValue else { return }
                CGEvent(scrollWheelEvent2Source: source, units: .pixel,
                        wheelCount: 2, wheel1: Int32(dy.rounded()),
                        wheel2: Int32(dx.rounded()), wheel3: 0)?
                    .post(tap: .cghidEventTap)
            case "space":
                guard let direction = message["direction"] as? String,
                      direction == "previous" || direction == "next" else { return }
                switchSpace(direction)
            case "button":
                guard let name = message["button"] as? String,
                      let state = message["state"] as? String else { return }
                let down = state == "down"
                let button: CGMouseButton = name == "right" ? .right : .left
                let eventType: CGEventType
                if name == "right" {
                    eventType = down ? .rightMouseDown : .rightMouseUp
                } else {
                    eventType = down ? .leftMouseDown : .leftMouseUp
                }
                if down { heldButtons.insert(name) } else { heldButtons.remove(name) }
                if let event = CGEvent(mouseEventSource: source, mouseType: eventType,
                                       mouseCursorPosition: pointerLocation(),
                                       mouseButton: button) {
                    let clicks = (message["clicks"] as? NSNumber)?.int64Value ?? 1
                    event.setIntegerValueField(.mouseEventClickState, value: clicks)
                    event.post(tap: .cghidEventTap)
                }
            case "key":
                guard let code = message["code"] as? String,
                      let state = message["state"] as? String else { return }
                postKey(code, down: state == "down")
            case "text":
                guard let text = message["text"] as? String else { return }
                let units = Array(text.utf16)
                for start in stride(from: 0, to: units.count, by: 20) {
                    let end = min(start + 20, units.count)
                    let chunk = Array(units[start..<end])
                    guard let down = CGEvent(keyboardEventSource: source,
                                             virtualKey: 0, keyDown: true),
                          let up = CGEvent(keyboardEventSource: source,
                                           virtualKey: 0, keyDown: false) else { continue }
                    chunk.withUnsafeBufferPointer { pointer in
                        down.keyboardSetUnicodeString(stringLength: chunk.count,
                                                      unicodeString: pointer.baseAddress)
                        up.keyboardSetUnicodeString(stringLength: chunk.count,
                                                    unicodeString: pointer.baseAddress)
                    }
                    down.post(tap: .cghidEventTap)
                    up.post(tap: .cghidEventTap)
                }
            case "release_all":
                releaseAllLocked()
            default:
                break
            }
        }
    }

    private func releaseAllLocked() {
        for name in heldButtons {
            let button: CGMouseButton = name == "right" ? .right : .left
            let type: CGEventType = name == "right" ? .rightMouseUp : .leftMouseUp
            CGEvent(mouseEventSource: source, mouseType: type,
                    mouseCursorPosition: pointerLocation(), mouseButton: button)?
                .post(tap: .cghidEventTap)
        }
        heldButtons.removeAll()
        for code in Array(heldKeys) { postKey(code, down: false) }
    }

    func releaseAll() {
        queue.sync { releaseAllLocked() }
    }
}

private let controller = InputController()

private func statusData() -> Data {
    let permitted = CGPreflightPostEventAccess()
    let message = permitted ? "Connected" :
        "Grant Accessibility access to avctl Input Helper on the Mac"
    let body: [String: Any] = [
        "type": "status",
        "available": true,
        "permission": permitted,
        "message": message,
    ]
    var data = (try? JSONSerialization.data(withJSONObject: body)) ?? Data()
    data.append(0x0A)
    return data
}

@discardableResult
private func writeAll(_ data: Data, to descriptor: Int32) -> Bool {
    data.withUnsafeBytes { bytes in
        guard let base = bytes.baseAddress else { return true }
        var offset = 0
        while offset < bytes.count {
            let written = Darwin.write(descriptor, base.advanced(by: offset),
                                       bytes.count - offset)
            if written < 0 {
                if errno == EINTR { continue }
                return false
            }
            if written == 0 { return false }
            offset += written
        }
        return true
    }
}

private func handleClient(_ descriptor: Int32) {
    var pending = Data()
    var buffer = Array(repeating: UInt8(0), count: 4_096)
    defer {
        controller.releaseAll()
        Darwin.close(descriptor)
    }
    while true {
        let count = Darwin.read(descriptor, &buffer, buffer.count)
        if count < 0 {
            if errno == EINTR { continue }
            return
        }
        if count == 0 { return }
        pending.append(contentsOf: buffer.prefix(count))
        if pending.count > 16_384 { return }
        while let newline = pending.firstIndex(of: 0x0A) {
            let line = pending[..<newline]
            pending.removeSubrange(...newline)
            guard line.count <= 2_048,
                  let value = try? JSONSerialization.jsonObject(with: Data(line)),
                  let message = value as? [String: Any],
                  let type = message["type"] as? String else { return }
            if type == "status" {
                if !writeAll(statusData(), to: descriptor) { return }
            } else {
                controller.handle(message)
            }
        }
    }
}

private func makeListener() throws -> Int32 {
    let parent = (socketPath as NSString).deletingLastPathComponent
    try FileManager.default.createDirectory(atPath: parent,
                                            withIntermediateDirectories: true)
    socketPath.withCString { _ = Darwin.unlink($0) }
    let descriptor = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
    guard descriptor >= 0 else { throw POSIXError(.ENOTSOCK) }

    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    let capacity = MemoryLayout.size(ofValue: address.sun_path)
    guard socketPath.utf8.count < capacity else {
        Darwin.close(descriptor)
        throw POSIXError(.ENAMETOOLONG)
    }
    withUnsafeMutablePointer(to: &address.sun_path) { pointer in
        let target = UnsafeMutableRawPointer(pointer)
            .assumingMemoryBound(to: CChar.self)
        socketPath.withCString { source in
            _ = strncpy(target, source, capacity - 1)
        }
    }
    let result = withUnsafePointer(to: &address) { pointer in
        pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            Darwin.bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
    }
    guard result == 0, Darwin.listen(descriptor, 4) == 0 else {
        let code = errno
        Darwin.close(descriptor)
        throw POSIXError(POSIXErrorCode(rawValue: code) ?? .EIO)
    }
    socketPath.withCString { _ = Darwin.chmod($0, S_IRUSR | S_IWUSR) }
    return descriptor
}

signal(SIGPIPE, SIG_IGN)
if environment["AVCTL_MINI_NO_PERMISSION_PROMPT"] != "1",
   !CGPreflightPostEventAccess() {
    _ = CGRequestPostEventAccess()
}

do {
    let listener = try makeListener()
    defer {
        Darwin.close(listener)
        socketPath.withCString { _ = Darwin.unlink($0) }
    }
    while true {
        let client = Darwin.accept(listener, nil, nil)
        if client < 0 {
            if errno == EINTR { continue }
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        DispatchQueue.global(qos: .userInteractive).async {
            handleClient(client)
        }
    }
} catch {
    FileHandle.standardError.write(
        Data("avctl-input-helper: \(error)\n".utf8))
    exit(EXIT_FAILURE)
}
