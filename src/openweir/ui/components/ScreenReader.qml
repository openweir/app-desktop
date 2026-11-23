pragma Singleton
import QtQuick

// What screen readers are told without the focus moving (DESIGN.md "读屏"): a status that
// shows up, or the row the arrow keys select in a list that keeps the focus itself. The
// message goes out as `item`'s announcement. `announced` repeats it, for the tests, which
// have no screen reader to hear it.
QtObject {
    signal announced(string message)

    function announce(item: Item, message: string) {
        if (message === "")
            return
        item.Accessible.announce(message)
        announced(message)
    }
}
