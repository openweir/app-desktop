// Package logind follows systemd-logind for the Linux service (docs/architecture.md "Linux
// (in progress)", D66): which users are signed in, and when the machine sleeps and wakes. The
// owner's Linux platform turns them into the service's session and power events, as session
// notifications and power broadcasts are on Windows.
package logind

// Inhibitor is the delay lock on sleep that Watch holds: logind waits for it before the
// machine sleeps, so that onSleep(true) can bring the tunnel down first, and Watch takes it
// again on waking. Close ends the watch and lets go of the lock.
type Inhibitor interface {
	Close() error
}
