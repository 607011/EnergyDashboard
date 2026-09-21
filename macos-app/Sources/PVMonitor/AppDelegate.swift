import AppKit
import SwiftUI
import Combine

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let model = AppModel()
    private var window: NSWindow!
    private var onTopItem: NSMenuItem!
    private var defaultsObserver: AnyCancellable?

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 280, height: 380),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "PV Monitor"
        window.contentView = NSHostingView(rootView: ContentView(model: model))
        window.setFrameAutosaveName("PVMonitorMainWindow")
        if !window.setFrameUsingName("PVMonitorMainWindow") { window.center() }
        window.isReleasedWhenClosed = false
        window.makeKeyAndOrderFront(nil)

        buildMenu()
        applyAlwaysOnTop()
        // The checkbox in the window and the menu item both write this default; KVO also sees
        // changes made from outside (e.g. `defaults write`).
        defaultsObserver = UserDefaults.standard.publisher(for: \.alwaysOnTop)
            .removeDuplicates()
            .sink { [weak self] _ in self?.applyAlwaysOnTop() }

        model.start()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    /// Clicking the Dock icon brings the window back.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { window.makeKeyAndOrderFront(nil) }
        return true
    }

    private var alwaysOnTop: Bool {
        get { UserDefaults.standard.bool(forKey: "alwaysOnTop") }
        set { UserDefaults.standard.set(newValue, forKey: "alwaysOnTop") }
    }

    /// "Always on top" = floating window level, also across Spaces and above full-screen apps.
    private func applyAlwaysOnTop() {
        let on = alwaysOnTop
        window.level = on ? .floating : .normal
        window.collectionBehavior = on ? [.canJoinAllSpaces, .fullScreenAuxiliary] : []
        window.hidesOnDeactivate = false
        onTopItem?.state = on ? .on : .off
    }

    // MARK: - Menu

    private func buildMenu() {
        let main = NSMenu()

        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "PV Monitor beenden", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        main.addItem(submenu: appMenu)

        let view = NSMenu(title: "Ansicht")
        onTopItem = NSMenuItem(title: "Immer im Vordergrund", action: #selector(toggleOnTop), keyEquivalent: "t")
        onTopItem.target = self
        view.addItem(onTopItem)
        let refresh = NSMenuItem(title: "Jetzt aktualisieren", action: #selector(refresh), keyEquivalent: "r")
        refresh.target = self
        view.addItem(refresh)
        let open = NSMenuItem(title: "Dashboard öffnen", action: #selector(openDashboard), keyEquivalent: "d")
        open.target = self
        view.addItem(open)
        main.addItem(submenu: view)

        let windowMenu = NSMenu(title: "Fenster")
        windowMenu.addItem(withTitle: "Minimieren", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "Schließen", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        main.addItem(submenu: windowMenu)
        NSApp.windowsMenu = windowMenu

        NSApp.mainMenu = main
    }

    @objc private func toggleOnTop() { alwaysOnTop.toggle() }
    @objc private func refresh() { model.refresh() }
    @objc private func openDashboard() { if let url = model.dashboardURL { NSWorkspace.shared.open(url) } }
}

private extension NSMenu {
    func addItem(submenu: NSMenu) {
        let item = NSMenuItem(title: submenu.title, action: nil, keyEquivalent: "")
        item.submenu = submenu
        addItem(item)
    }
}

extension UserDefaults {
    @objc dynamic var alwaysOnTop: Bool { bool(forKey: "alwaysOnTop") }
}
