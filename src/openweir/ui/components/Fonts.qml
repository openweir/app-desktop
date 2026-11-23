pragma Singleton
import QtQuick

// fonts.py registers the bundled families and script/emoji fallbacks before QML loads.
// This fallback only keeps the interface usable after a diagnosed missing font asset.
QtObject {
    readonly property var installed: Qt.fontFamilies()
    // The family each type role's family resolved to: every UiText asks, and the list of
    // installed families is long. The families do not change while the app runs.
    readonly property var resolved: ({})

    function resolve(family: string): string {
        const known = resolved[family]
        if (known !== undefined)
            return known
        let result = Application.font.family
        if (installed.indexOf(family) >= 0)
            result = family
        else if (family === "JetBrains Mono")
            result = Qt.platform.os === "osx" ? "Menlo" : Qt.platform.os === "windows" ? "Consolas" : "monospace"
        resolved[family] = result
        return result
    }
}
