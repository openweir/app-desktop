pragma Singleton
import QtQuick
import OpenWeir

// The window's own title band (DESIGN.md "顶部区域"): whose platform it follows, and what the
// window does when the band asks.
QtObject {
    // Main.qml binds this to the desktop ViewModel's title bar.
    property TitleBar titleBar
    readonly property bool mac: titleBar?.style === "mac"
    readonly property bool windows: titleBar?.style === "windows"
    // The system's title bar is above the band, with its own buttons.
    readonly property bool linux: titleBar?.style === "linux"

    function toggleMaximized(window: Window) {
        if (window.visibility === Window.Maximized)
            window.showNormal()
        else
            window.showMaximized()
    }

    // A double-click on an empty part of the band: macOS does what the system settings say;
    // Windows and Linux maximize.
    function doubleClicked(window: Window) {
        const action = titleBar?.doubleClickAction() ?? "zoom"
        if (action === "minimize")
            window.showMinimized()
        else if (action === "zoom")
            toggleMaximized(window)
    }
}
