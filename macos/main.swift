import AppKit
import Foundation

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    let title = NSTextField(labelWithString: "Изолированный рабочий стол")
    let status = NSTextField(labelWithString: "Проверяю состояние…")
    let network = NSTextField(labelWithString: "Сетевой выход ещё не проверен")
    let details = NSTextView()
    var processes: [Process] = []
    var vmProcess: Process?
    var startButton: NSButton!
    var restartButton: NSButton!
    var resourcePicker: NSPopUpButton!
    var timer: Timer?
    var busy = false
    var attemptedSetup = false
    var permissionBlocked = false
    var pendingChecks = Set<String>()
    let dataURL: URL = {
        let arguments = ProcessInfo.processInfo.arguments
        if let index = arguments.firstIndex(of: "--data"), index + 1 < arguments.count {
            return URL(fileURLWithPath: (arguments[index + 1] as NSString).expandingTildeInPath)
        }
        let output = Bundle.main.bundleURL.deletingLastPathComponent()
        let legacy = output.appendingPathComponent("Claude Environment Data", isDirectory: true)
        if FileManager.default.fileExists(atPath: legacy.appendingPathComponent("environment.json").path) {
            return legacy
        }
        // Applications and Gatekeeper translocation paths may be read-only.
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Claude Environment", isDirectory: true)
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 780, height: 700),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Claude Environment"
        window.minSize = NSSize(width: 720, height: 680)
        window.collectionBehavior = [.fullScreenPrimary]
        window.setFrameAutosaveName("ClaudeEnvironmentWindow")
        window.center()
        let content = window.contentView!
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 18
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 30),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -30),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 28),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -24)])
        title.font = .systemFont(ofSize: 25, weight: .semibold)
        stack.addArrangedSubview(title)
        let intro = NSTextField(wrappingLabelWithString:
            "Claude Desktop работает в отдельной Linux-системе. Папки, буфер обмена, камера и микрофон Mac не передаются.")
        intro.textColor = .secondaryLabelColor
        stack.addArrangedSubview(intro)
        status.font = .systemFont(ofSize: 16, weight: .medium)
        stack.addArrangedSubview(status)
        stack.addArrangedSubview(network)
        let buttons = NSStackView()
        buttons.spacing = 10
        startButton = NSButton(title: "Запустить", target: self, action: #selector(start))
        startButton.isEnabled = false
        restartButton = NSButton(title: "Перезапустить среду", target: self, action: #selector(restart))
        restartButton.isEnabled = false
        for button in [startButton!, restartButton!,
                       NSButton(title: "Проверить сеть", target: self, action: #selector(checkNetwork)),
                       NSButton(title: "Остановить", target: self, action: #selector(stop))] {
            button.bezelStyle = .rounded
            buttons.addArrangedSubview(button)
        }
        stack.addArrangedSubview(buttons)
        resourcePicker = NSPopUpButton()
        resourcePicker.addItems(withTitles: ["Экономный — 3 ГБ памяти, 2 CPU", "Стандартный — 6 ГБ памяти, 4 CPU"])
        resourcePicker.target = self
        resourcePicker.action = #selector(changeResources)
        resourcePicker.isEnabled = false
        stack.addArrangedSubview(resourcePicker)
        let note = NSTextField(wrappingLabelWithString:
            "Включите системный VPN перед запуском. Изменения маршрутов macOS отслеживаются сразу; внешний IP проверяется отдельно. Сетевые события вызывают перепроверку; смена внешнего IP закрывает доступ до перезапуска. Все компоненты устанавливаются автоматически. При первой установке macOS может запросить пароль администратора. Это экспериментальная версия; Cowork не проверен.")
        note.font = .systemFont(ofSize: 12)
        note.textColor = .secondaryLabelColor
        stack.addArrangedSubview(note)
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        details.isEditable = false
        details.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        details.autoresizingMask = [.width]
        details.textContainer?.widthTracksTextView = true
        scroll.documentView = details
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 150).isActive = true
        stack.addArrangedSubview(scroll)
        scroll.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        let folder = NSButton(title: "Открыть папку среды", target: self, action: #selector(openFolder))
        folder.bezelStyle = .rounded
        stack.addArrangedSubview(folder)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        run("status")
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            guard let self = self, !self.busy, !self.permissionBlocked else { return }
            self.run("status")
        }
    }

    func append(_ text: String) {
        details.string += text + "\n"
        if details.string.count > 15000 { details.string = String(details.string.suffix(12000)) }
        details.scrollToEndOfDocument(nil)
    }

    func handle(_ text: String, action: String) {
        // A denied TCC request must not cause another prompt every poll.
        if text.contains("Operation not permitted") || text.contains("Permission denied") || text.contains("\"permission_error\": true") {
            permissionBlocked = true
            busy = false
            status.stringValue = "Нет доступа к папке среды. Автоматические проверки приостановлены."
            append("Установите приложение в Applications; данные новой среды хранятся в Library/Application Support. После разрешения доступа нажмите «Проверить сеть».")
            return
        }
        for line in text.split(separator: "\n") {
            guard let data = String(line).data(using: .utf8),
                  let event = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let message = event["message"] as? String else {
                append(String(line)); continue
            }
            if action != "status" || status.stringValue != message { append(message) }
            if action == "country" {
                let country = event["country"] as? String ?? "неизвестно"
                network.stringValue = "Проверка IP: \(country) · \(message)"
                network.textColor = .secondaryLabelColor
                run("status")
            } else {
                status.stringValue = message
            }
            if let state = event["network"] as? [String: Any] {
                let allowed = state["allowed"] as? Bool == true
                network.stringValue = allowed ? "Сеть среды открыта · IP зафиксирован" : "Сеть среды закрыта · \(state["reason"] as? String ?? "нужна проверка")"
                network.textColor = allowed ? .systemGreen : .systemRed
            } else if action == "status", event["running"] as? Bool == false {
                network.stringValue = "Среда остановлена · сетевых соединений нет"
                network.textColor = .secondaryLabelColor
            }
            if (action == "start" || action == "restart"), event["running"] as? Bool == true { busy = false }
            if let ready = event["ready"] as? Bool {
                let running = event["running"] as? Bool == true
                resourcePicker.isEnabled = !busy && !running && vmProcess == nil
                if let memory = event["memory_mb"] as? Int {
                    resourcePicker.selectItem(at: memory <= 3072 ? 0 : 1)
                }
                startButton.isEnabled = !busy && !running && vmProcess == nil
                restartButton.isEnabled = !busy && ready
                if action == "status", !ready, !running, !attemptedSetup, vmProcess == nil {
                    attemptedSetup = true
                    start()
                }
            }
        }
    }

    func run(_ action: String) {
        guard let resources = Bundle.main.resourceURL else { return }
        let isCheck = action == "status" || action == "country"
        if isCheck {
            guard !permissionBlocked else { return }
            guard !pendingChecks.contains(action) else { return }
            pendingChecks.insert(action)
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [resources.appendingPathComponent("runtime/macos/bootstrap.sh").path,
                             action, "--data", dataURL.path]
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = env
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
            processes.append(process)
            if isCheck {
                DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [weak self] in
                    if process.isRunning {
                        process.terminate()
                        self?.append("Проверка задержалась. Повторю автоматически.")
                    }
                }
            }
            if action == "start" || action == "restart" { vmProcess = process }
            // Drain output to EOF before reporting completion; short commands
            // may otherwise terminate before readability callbacks are delivered.
            DispatchQueue.global(qos: .utility).async { [weak self] in
                var buffer = Data()
                while true {
                    let data = pipe.fileHandleForReading.availableData
                    if data.isEmpty { break }
                    buffer.append(data)
                    while let newline = buffer.firstIndex(of: 10) {
                        let line = String(decoding: buffer.prefix(upTo: newline), as: UTF8.self)
                        buffer.removeSubrange(...newline)
                        DispatchQueue.main.async { self?.handle(line, action: action) }
                    }
                }
                if !buffer.isEmpty {
                    let line = String(decoding: buffer, as: UTF8.self)
                    DispatchQueue.main.async { self?.handle(line, action: action) }
                }
                process.waitUntilExit()
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    self.processes.removeAll { $0 === process }
                    self.pendingChecks.remove(action)
                    if action == "economy" || action == "standard" { self.run("status") }
                    if (action == "start" || action == "restart"), self.vmProcess === process {
                        self.vmProcess = nil; self.busy = false
                        self.run("status")
                    }
                }
            }
        } catch { pendingChecks.remove(action); busy = false; status.stringValue = "Ошибка запуска: \(error.localizedDescription)" }
    }

    @objc func start() {
        guard vmProcess == nil else { window.makeKeyAndOrderFront(nil); return }
        busy = true; startButton.isEnabled = false; restartButton.isEnabled = false
        run("start")
    }
    @objc func restart() {
        guard !busy else { return }
        busy = true; startButton.isEnabled = false; restartButton.isEnabled = false
        run("restart")
    }
    @objc func toggleFullScreen() { window.toggleFullScreen(nil) }
    @objc func changeResources() {
        resourcePicker.isEnabled = false
        run(resourcePicker.indexOfSelectedItem == 0 ? "economy" : "standard")
    }
    @objc func stop() { run("stop") }
    @objc func checkNetwork() {
        permissionBlocked = false
        run("status")
        run("country")
    }
    @objc func openFolder() { NSWorkspace.shared.open(dataURL) }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if vmProcess?.isRunning == true {
            let alert = NSAlert()
            alert.messageText = "Среда ещё работает"
            alert.informativeText = "Сначала сохраните документы и остановите Linux кнопкой «Остановить»."
            alert.addButton(withTitle: "Вернуться")
            alert.runModal()
            return .terminateCancel
        }
        return .terminateNow
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.setActivationPolicy(.regular)
app.delegate = delegate
let menu = NSMenu()
let item = NSMenuItem()
let appMenu = NSMenu()
appMenu.addItem(withTitle: "Завершить Claude Environment", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
item.submenu = appMenu
menu.addItem(item)
let viewItem = NSMenuItem()
let viewMenu = NSMenu(title: "Вид")
let fullscreen = NSMenuItem(title: "На весь экран", action: #selector(AppDelegate.toggleFullScreen), keyEquivalent: "f")
fullscreen.keyEquivalentModifierMask = [.command, .control]
fullscreen.target = delegate
viewMenu.addItem(fullscreen)
viewItem.submenu = viewMenu
menu.addItem(viewItem)
app.mainMenu = menu
app.run()
