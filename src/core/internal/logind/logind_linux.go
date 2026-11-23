package logind

import (
	"context"
	"errors"
	"fmt"

	"github.com/godbus/dbus/v5"
)

// logind's manager on the system bus.
const (
	destination                 = "org.freedesktop.login1"
	managerPath dbus.ObjectPath = "/org/freedesktop/login1"
)

// Watch is to report each user signing in (a session that is not closing appears; lingering
// counts as signed out) and signing out with onUser, and the machine going to sleep (true)
// and waking (false) with onSleep, until ctx ends or the Inhibitor is closed.
//
// Not implemented yet: it reports errors.ErrUnsupported.
func Watch(ctx context.Context, onUser func(uid uint32, signedIn bool), onSleep func(sleeping bool)) (Inhibitor, error) {
	return nil, fmt.Errorf("watch %s at %s: not implemented yet: %w", destination, managerPath, errors.ErrUnsupported)
}
