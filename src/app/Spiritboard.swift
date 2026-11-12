import AppKit
import WebKit

private final class HealthRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

private struct AppReadiness: Decodable {
    let url: String
    let instance_id: String
}

private struct DashboardHealth: Decodable {
    let ok: Bool
    let service: String
    let instance_id: String
}

final class SpiritboardApp: NSObject, NSApplicationDelegate, NSMenuItemValidation,
    WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private static let dataDirectoryKey = "dataDirectory"
    private let headerColor = NSColor(
        srgbRed: 17.0 / 255, green: 14.0 / 255, blue: 26.0 / 255, alpha: 1
    )
    private let boardColor = NSColor(
        srgbRed: 13.0 / 255, green: 11.0 / 255, blue: 20.0 / 255, alpha: 1
    )
    private var window: NSWindow!
    private var webView: WKWebView!
    private let spinner = NSProgressIndicator()
    private var backend: Process?
    private var launch = 0
    private var origin: URL?
    private var instanceID: UUID?
    private var output = Data()
    private var ready = false
    private var quitting = false
    private var failureShowing = false
    private var retries = 0
    private var healthySince: Date?
    private var healthTimer: Timer?
    private var startupTimer: Timer?
    private var healthTask: URLSessionDataTask?
    private var recovery: DispatchWorkItem?
    private var dashboardNavigation: WKNavigation?
    private let healthSession: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 2
        configuration.timeoutIntervalForResource = 2
        return URLSession(configuration: configuration, delegate: HealthRedirects(), delegateQueue: nil)
    }()
    private var popupWindows = [ObjectIdentifier: NSWindow]()

    // A folder chosen by the user; nil means the normal default data directory.
    private var dataDirectory: String? {
        get { UserDefaults.standard.string(forKey: Self.dataDirectoryKey) }
        set { UserDefaults.standard.set(newValue, forKey: Self.dataDirectoryKey) }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        installMenu()

        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(self, name: "titlebar")
        configuration.userContentController.addUserScript(WKUserScript(
            source: """
                const reportTitlebar = () => {
                  const header = document.querySelector('.top');
                  if (header) window.webkit.messageHandlers.titlebar.postMessage(
                    header.getBoundingClientRect().bottom > 0);
                };
                addEventListener('scroll', reportTitlebar, {passive: true});
                addEventListener('resize', reportTitlebar, {passive: true});
                addEventListener('load', reportTitlebar);
                reportTitlebar();
                """,
            injectionTime: .atDocumentEnd,
            forMainFrameOnly: true
        ))
        // The dashboard may be served by an older CLI process, so keep Mac controls in the app.
        if let url = Bundle.main.url(forResource: "select-menu", withExtension: "js"),
           let source = try? String(contentsOf: url, encoding: .utf8) {
            configuration.userContentController.addUserScript(WKUserScript(
                source: source, injectionTime: .atDocumentEnd, forMainFrameOnly: true
            ))
        }
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.underPageBackgroundColor = headerColor
        webView.autoresizingMask = [.width, .height]

        let content = NSView()
        webView.frame = content.bounds
        content.addSubview(webView)
        spinner.style = .spinning
        spinner.isDisplayedWhenStopped = false
        spinner.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(spinner)
        NSLayoutConstraint.activate([
            spinner.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            spinner.centerYAnchor.constraint(equalTo: content.centerYAnchor),
        ])

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.titlebarSeparatorStyle = .none
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = headerColor
        window.minSize = NSSize(width: 640, height: 420)
        window.contentView = content
        if !window.setFrameUsingName("Spiritboard") {
            window.center()
        }
        window.setFrameAutosaveName("Spiritboard")
        window.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
        connect()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        launch += 1
        recovery?.cancel()
        stopChecks()
        healthSession.invalidateAndCancel()
        if let backend, backend.isRunning {
            backend.interrupt()
        }
    }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.name == "titlebar", message.webView === webView,
              let headerVisible = message.body as? Bool else { return }
        let color = headerVisible ? headerColor : boardColor
        window.backgroundColor = color
        webView.underPageBackgroundColor = color
    }

    private func installMenu() {
        let main = NSMenu()
        func item(_ title: String, _ action: Selector?, _ key: String = "",
                  _ modifiers: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            item.keyEquivalentModifierMask = modifiers
            return item
        }
        func submenu(_ title: String, _ items: [NSMenuItem]) -> NSMenu {
            let menu = NSMenu(title: title)
            items.forEach(menu.addItem)
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            item.submenu = menu
            main.addItem(item)
            return menu
        }
        _ = submenu("Spiritboard", [
            item("About Spiritboard",
                 #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            item("Hide Spiritboard", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h",
                 [.command, .option]),
            item("Show All", #selector(NSApplication.unhideAllApplications(_:))),
            .separator(),
            item("Quit Spiritboard", #selector(NSApplication.terminate(_:)), "q"),
        ])
        _ = submenu("File", [
            item("Choose Data Folder…", #selector(chooseDataDirectory(_:)), "o"),
            item("Use Default Data Folder", #selector(useDefaultDataDirectory(_:))),
            .separator(),
            item("Close Window", #selector(NSWindow.performClose(_:)), "w"),
        ])
        _ = submenu("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        _ = submenu("View", [
            item("Reload", #selector(reloadDashboard(_:)), "r"),
            .separator(),
            item("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f",
                 [.command, .control]),
        ])
        let windows = submenu("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Zoom", #selector(NSWindow.performZoom(_:))),
            .separator(),
            item("Bring All to Front", #selector(NSApplication.arrangeInFront(_:))),
        ])
        NSApplication.shared.mainMenu = main
        NSApplication.shared.windowsMenu = windows
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        switch menuItem.action {
        case #selector(useDefaultDataDirectory(_:)):
            menuItem.state = dataDirectory == nil ? .on : .off
        case #selector(chooseDataDirectory(_:)):
            menuItem.state = dataDirectory == nil ? .off : .on
        case #selector(reloadDashboard(_:)):
            return ready
        default:
            break
        }
        return window.attachedSheet == nil
    }

    @objc private func chooseDataDirectory(_ sender: Any?) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        // Custom --data-dir folders are often hidden, such as a project's .spiritboard.
        panel.showsHiddenFiles = true
        panel.prompt = "Show Dashboard"
        panel.message = "Choose a Spiritboard data folder. Its saved settings are used to restart it."
        if let dataDirectory {
            panel.directoryURL = URL(fileURLWithPath: dataDirectory)
        }
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self else { return }
            guard response == .OK, let path = panel.url?.path else {
                // Cancelling after a failure must not leave an empty window.
                if !self.ready { self.connect() }
                return
            }
            self.dataDirectory = path
            self.connect()
        }
    }

    @objc private func useDefaultDataDirectory(_ sender: Any?) {
        guard dataDirectory != nil else { return }
        dataDirectory = nil
        connect()
    }

    @objc private func reloadDashboard(_ sender: Any?) {
        dashboardNavigation = webView.reload()
    }

    private func stopChecks() {
        healthTimer?.invalidate()
        healthTimer = nil
        startupTimer?.invalidate()
        startupTimer = nil
        healthTask?.cancel()
        healthTask = nil
    }

    private func connect(resetRetries: Bool = true) {
        guard !quitting else { return }
        recovery?.cancel()
        recovery = nil
        stopChecks()
        if resetRetries { retries = 0 }
        launch += 1
        let current = launch
        output = Data()
        ready = false
        origin = nil
        instanceID = nil
        healthySince = nil
        dashboardNavigation = nil
        webView.stopLoading()
        webView.isHidden = true
        spinner.startAnimation(nil)
        let folder = dataDirectory.map { URL(fileURLWithPath: $0).lastPathComponent }
        window.title = folder.map { "Spiritboard — \($0)" } ?? "Spiritboard"
        guard let old = backend, old.isRunning else {
            startBackend(current)
            return
        }
        // Our own server may own the folder chosen next, so let it release the lock first.
        old.interrupt()
        DispatchQueue.global().async {
            let deadline = Date().addingTimeInterval(5)
            while old.isRunning, Date() < deadline { usleep(50_000) }
            if old.isRunning {
                old.terminate()
                let terminationDeadline = Date().addingTimeInterval(3)
                while old.isRunning, Date() < terminationDeadline { usleep(50_000) }
            }
            let stopped = !old.isRunning
            DispatchQueue.main.async { [weak self] in
                guard let self, !self.quitting, current == self.launch else { return }
                guard stopped else {
                    self.showFailure(
                        "The previous dashboard helper could not stop. "
                            + "Stop that dashboard before trying again."
                    )
                    return
                }
                self.startBackend(current)
            }
        }
    }

    private func startBackend(_ current: Int) {
        guard !quitting, current == launch else { return }
        let process = Process()
        process.executableURL = Bundle.main.bundleURL
            .appendingPathComponent("Contents/Helpers/spiritboard-backend")
        if let dataDirectory {
            process.arguments = ["--data-dir", dataDirectory]
        }
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let chunk = handle.availableData
            if chunk.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            DispatchQueue.main.async {
                guard let self, current == self.launch else { return }
                self.receive(chunk)
            }
        }
        process.terminationHandler = { [weak self] terminated in
            let status = terminated.terminationStatus
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                guard let self, !self.quitting, current == self.launch else { return }
                // Joining an existing server ends the helper normally; health checks watch it.
                if self.ready && status == 0 { return }
                self.recover(self.ready ? "The dashboard stopped unexpectedly." : nil)
            }
        }
        do {
            try process.run()
            backend = process
            startupTimer = Timer.scheduledTimer(withTimeInterval: 20, repeats: false) { [weak self] _ in
                guard let self, current == self.launch, !self.ready else { return }
                self.recover("The dashboard did not finish starting.")
            }
        } catch {
            recover("The dashboard helper could not start.")
        }
    }

    private func receive(_ chunk: Data) {
        output.append(chunk)
        if output.count > 16_384 {
            output.removeFirst(output.count - 8_192)
        }
        guard !ready, let message = String(data: output, encoding: .utf8) else { return }
        for line in message.split(separator: "\n", omittingEmptySubsequences: false).dropLast() {
            let prefix = "Spiritboard app ready: "
            guard line.hasPrefix(prefix),
                  let data = String(line.dropFirst(prefix.count)).data(using: .utf8),
                  let payload = try? JSONDecoder().decode(AppReadiness.self, from: data) else { continue }
            let address = payload.url
            guard let url = URL(string: address), url.scheme == "http",
                  url.host == "127.0.0.1", let port = url.port, (1...65535).contains(port),
                  address == "http://127.0.0.1:\(port)" || address == "http://127.0.0.1:\(port)/",
                  let identity = Self.identity(payload.instance_id) else { continue }
            startupTimer?.invalidate()
            startupTimer = nil
            ready = true
            origin = url
            instanceID = identity
            dashboardNavigation = webView.load(URLRequest(url: url))
            let current = launch
            healthTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
                self?.checkHealth(current)
            }
            checkHealth(current)
            return
        }
    }

    private static func identity(_ value: String) -> UUID? {
        var canonical = value
        if value.range(of: "^[0-9A-Fa-f]{32}$", options: .regularExpression) != nil {
            for offset in [20, 16, 12, 8] {
                canonical.insert("-", at: canonical.index(canonical.startIndex, offsetBy: offset))
            }
        }
        return UUID(uuidString: canonical)
    }

    private func checkHealth(_ current: Int) {
        guard !quitting, current == launch, ready, healthTask == nil,
              let origin, let instanceID else { return }
        let url = origin.appendingPathComponent("api/health")
        let request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 2)
        healthTask = healthSession.dataTask(with: request) { [weak self] data, response, error in
            let response = response as? HTTPURLResponse
            let payload = data.flatMap { bytes -> DashboardHealth? in
                guard bytes.count <= 8192 else { return nil }
                return try? JSONDecoder().decode(DashboardHealth.self, from: bytes)
            }
            let valid = error == nil && response?.statusCode == 200 && response?.url == url
                && payload?.ok == true && payload?.service == "spiritboard"
                && payload.flatMap { Self.identity($0.instance_id) } == instanceID
            DispatchQueue.main.async {
                guard let self, !self.quitting, current == self.launch, self.ready else { return }
                self.healthTask = nil
                guard valid else {
                    self.recover("The dashboard stopped responding. Automatic recovery could not restore it.")
                    return
                }
                if let since = self.healthySince {
                    if Date().timeIntervalSince(since) >= 30 { self.retries = 0 }
                } else {
                    self.healthySince = Date()
                }
            }
        }
        healthTask?.resume()
    }

    private func recover(_ detail: String? = nil) {
        guard !quitting, !failureShowing, recovery == nil else { return }
        launch += 1
        let current = launch
        stopChecks()
        ready = false
        origin = nil
        instanceID = nil
        healthySince = nil
        dashboardNavigation = nil
        webView.stopLoading()
        webView.isHidden = true
        spinner.startAnimation(nil)
        guard retries < 3 else {
            showFailure(detail)
            return
        }
        let delay = pow(2.0, Double(retries))
        retries += 1
        let work = DispatchWorkItem { [weak self] in
            guard let self, !self.quitting, current == self.launch else { return }
            self.recovery = nil
            self.connect(resetRetries: false)
        }
        recovery = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func showFailure(_ detail: String? = nil) {
        guard !quitting, !failureShowing, window.attachedSheet == nil else { return }
        failureShowing = true
        stopChecks()
        spinner.stopAnimation(nil)
        webView.isHidden = true
        ready = false
        let reason = detail ?? String(data: output, encoding: .utf8)?
            .split(separator: "\n").last.map {
                String($0).replacingOccurrences(of: "Spiritboard could not connect: ", with: "")
            }
        let alert = NSAlert()
        let current = launch
        let custom = dataDirectory != nil
        alert.addButton(withTitle: "Try Again")
        alert.addButton(withTitle: "Choose Data Folder…")
        if let dataDirectory {
            let folder = (dataDirectory as NSString).abbreviatingWithTildeInPath
            alert.messageText = "Spiritboard can't show the dashboard for this data folder."
            alert.informativeText = [
                reason, "Folder: \(folder)",
                "Check that the folder and its saved source settings are still available, then try again.",
            ].compactMap { $0 }.joined(separator: "\n\n")
            alert.addButton(withTitle: "Use Default Data Folder")
        } else {
            alert.messageText = "Spiritboard could not start its local dashboard."
            alert.informativeText = reason ?? ""
        }
        alert.addButton(withTitle: "Quit")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self else { return }
            self.failureShowing = false
            guard !self.quitting, current == self.launch else { return }
            switch response {
            case .alertFirstButtonReturn:
                self.connect()
            case .alertSecondButtonReturn:
                DispatchQueue.main.async { self.chooseDataDirectory(nil) }
            case .alertThirdButtonReturn where custom:
                self.useDefaultDataDirectory(nil)
            default:
                NSApplication.shared.terminate(nil)
            }
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard webView === self.webView, navigation === dashboardNavigation, ready,
              let origin, webView.url?.host == origin.host,
              webView.url?.port == origin.port else { return }
        spinner.stopAnimation(nil)
        webView.isHidden = false
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        navigationFailed(webView, navigation, error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        navigationFailed(webView, navigation, error)
    }

    private func navigationFailed(_ webView: WKWebView, _ navigation: WKNavigation?, _ error: Error) {
        // A replaced or cancelled load is not a dashboard failure.
        guard webView === self.webView, navigation === dashboardNavigation, ready,
              (error as NSError).code != NSURLErrorCancelled else { return }
        recover("The dashboard stopped responding. Automatic recovery could not restore it.")
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else {
            decisionHandler(.cancel)
            return
        }
        if url.absoluteString == "about:blank",
           popupWindows[ObjectIdentifier(webView)] != nil {
            decisionHandler(.allow)
            return
        }
        if let origin, url.scheme == origin.scheme, url.host == origin.host,
           url.port == origin.port {
            decisionHandler(.allow)
        } else {
            if url.scheme == "https" || url.scheme == "http" {
                NSWorkspace.shared.open(url)
            }
            if let popup = popupWindows.removeValue(forKey: ObjectIdentifier(webView)) {
                popup.close()
            }
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        let popup = WKWebView(frame: .zero, configuration: configuration)
        popup.navigationDelegate = self
        popup.uiDelegate = self
        let popupWindow = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 640),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        popupWindow.title = "Spiritboard sign in"
        popupWindow.contentView = popup
        popupWindow.center()
        popupWindow.makeKeyAndOrderFront(nil)
        popupWindows[ObjectIdentifier(popup)] = popupWindow
        return popup
    }

    func webViewDidClose(_ webView: WKWebView) {
        popupWindows.removeValue(forKey: ObjectIdentifier(webView))?.close()
    }
}

let app = NSApplication.shared
let delegate = SpiritboardApp()
app.delegate = delegate
app.run()
