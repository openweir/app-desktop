//go:build linux

package owner

import (
	"errors"
	"fmt"
	"math"
	"strconv"
	"strings"

	"github.com/openweir/app-desktop/src/core/internal/logind"
	"github.com/openweir/app-desktop/src/core/internal/peer"
	"github.com/openweir/app-desktop/src/core/internal/power"
	"github.com/openweir/app-desktop/src/core/internal/sysproxy"
)

// Linux is the Linux service's Platform; restart ends the process for systemd to start
// again. Users are decimal uids, and a session is a user logind has signed in, numbered by
// their uid: the service follows users, not their sessions, as peer gives a caller's uid for
// both (docs/architecture.md "Linux (in progress)"). logind.Watch brings logind's sign-ins
// to Service.SessionChanged and its sleeps to Service.Sleep.
func Linux(restart func()) Platform {
	return &linuxPlatform{restart: restart, logind: logind.System(), proxies: desktop{}, dirs: rootDirs}
}

type linuxPlatform struct {
	restart func()
	logind  logind.Queries
	proxies proxies
	dirs    dirs
}

// proxies reaches a user's desktop proxy settings; tests stand in for it.
type proxies interface {
	Read(user string) (sysproxy.Settings, error)
	Write(user string, settings sysproxy.Settings) error
}

type desktop struct{}

func (desktop) Read(user string) (sysproxy.Settings, error) { return sysproxy.Read(user) }

func (desktop) Write(user string, settings sysproxy.Settings) error {
	return sysproxy.Write(user, settings)
}

func (p *linuxPlatform) Restart() { p.restart() }

func (*linuxPlatform) NetworkReady() bool { return power.NetworkAvailable() }

func (p *linuxPlatform) SecureDir(dir, reader string) error { return p.dirs.secure(dir, reader) }

// signedIn is the logind user whose uid is session, while they are signed in.
func (p *linuxPlatform) signedIn(session uint32) (logind.User, error) {
	user, err := p.logind.User(session)
	if errors.Is(err, logind.ErrNoUser) || (err == nil && !user.SignedIn()) {
		return logind.User{}, fmt.Errorf("user %d is not signed in", session)
	}
	return user, err
}

func (p *linuxPlatform) SessionUser(session uint32) (string, error) {
	if _, err := p.signedIn(session); err != nil {
		return "", err
	}
	return strconv.FormatUint(uint64(session), 10), nil
}

// SessionLogonTime is when logind began to track the user. It stays while they keep a
// session, and changes once they signed out of all of them and in again.
func (p *linuxPlatform) SessionLogonTime(session uint32) (int64, error) {
	user, err := p.signedIn(session)
	if err != nil {
		return 0, err
	}
	if user.Timestamp == 0 || user.Timestamp > math.MaxInt64 {
		return 0, fmt.Errorf("logind gave user %d no sign-in time", session)
	}
	return int64(user.Timestamp), nil
}

func (p *linuxPlatform) SignedIn(id string) []uint32 {
	uid, ok := parseUID(id)
	if !ok {
		return nil
	}
	if _, err := p.signedIn(uid); err != nil {
		return nil
	}
	return []uint32{uid}
}

func (p *linuxPlatform) ReadProxy(session uint32) (sysproxy.Settings, error) {
	account, err := p.account(session)
	if err != nil {
		return sysproxy.Settings{}, err
	}
	return p.proxies.Read(account)
}

func (p *linuxPlatform) WriteProxy(session uint32, settings sysproxy.Settings) error {
	account, err := p.account(session)
	if err != nil {
		return err
	}
	return p.proxies.Write(account, settings)
}

// account names the user signed in as session for systemd-run's --machine=<user>@.host: the
// name logind knows them by, which needs no lookup by a service built without cgo, even for a
// directory's user; their uid where the name would not parse there.
func (p *linuxPlatform) account(session uint32) (string, error) {
	user, err := p.signedIn(session)
	if err != nil {
		return "", err
	}
	if user.Name == "" || strings.ContainsAny(user.Name, "@/ \t\n") {
		return strconv.FormatUint(uint64(session), 10), nil
	}
	return user.Name, nil
}

// errNotLocal refuses a claim from anywhere but an active local session (D66): the TUN takes
// every user's traffic, so only the user in front of the machine may turn it on.
var errNotLocal = errors.New("only a user in an active session at this machine may own the tunnel")

func (p *linuxPlatform) MayClaim(caller peer.Identity) error {
	uid, ok := parseUID(caller.UserID)
	if !ok {
		return fmt.Errorf("caller %q has no uid", caller.UserID)
	}
	return p.mayClaim(uid, callerPID(caller))
}

// callerPID is the caller's process, whose session decides a claim; 0 while peer.Identity
// does not carry it, and then the user's display session decides alone.
func callerPID(peer.Identity) uint32 { return 0 }

// mayClaim lets uid claim when process pid sits in an active local session of theirs. A
// program the desktop started is often in no session at all, under the user's service manager
// (as GNOME and KDE start them), and is judged as polkit judges it: by the user's display
// session, the graphical one they signed in to. A process in someone else's session is
// refused: it is not the user at this machine, or no longer the caller's process.
func (p *linuxPlatform) mayClaim(uid, pid uint32) error {
	if pid != 0 {
		session, err := p.logind.SessionOf(pid)
		switch {
		case err == nil && session.UID != uid:
			return fmt.Errorf("%w: process %d runs in user %d's session %s", errNotLocal, pid, session.UID, session.ID)
		case err == nil && strings.HasPrefix(session.Class, "user"):
			return activeLocal(session)
		case err != nil && !errors.Is(err, logind.ErrNoSession):
			return fmt.Errorf("find the caller's session: %w", err)
		}
	}
	user, err := p.logind.User(uid)
	if errors.Is(err, logind.ErrNoUser) {
		return fmt.Errorf("%w: user %d is not signed in", errNotLocal, uid)
	} else if err != nil {
		return fmt.Errorf("find the caller's session: %w", err)
	}
	display, ok := user.DisplaySession()
	if !ok {
		return fmt.Errorf("%w: user %d has no display session", errNotLocal, uid)
	}
	return activeLocal(display)
}

func activeLocal(session logind.Session) error {
	switch {
	case session.ActiveLocal():
		return nil
	case session.Remote:
		return fmt.Errorf("%w: session %s is remote", errNotLocal, session.ID)
	case session.Class != "user":
		return fmt.Errorf("%w: session %s is of class %s", errNotLocal, session.ID, session.Class)
	}
	return fmt.Errorf("%w: session %s is %s, not active", errNotLocal, session.ID, session.State)
}

// parseUID reads a user ID that validUserID accepts.
func parseUID(id string) (uint32, bool) {
	if !ValidUID(id) {
		return 0, false
	}
	uid, err := strconv.ParseUint(id, 10, 32)
	return uint32(uid), err == nil
}
