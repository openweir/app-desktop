// Package logind follows systemd-logind for the Linux service (docs/architecture.md "Linux",
// D66): which users are signed in, and when the machine sleeps and wakes. The
// owner's Linux platform turns them into the service's session and power events, as session
// notifications and power broadcasts are on Windows, and asks it whether a caller sits in an
// active local session.
package logind

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"slices"
	"strings"
	"sync"
	"time"
)

// Inhibitor is the delay lock on sleep that Watch holds: logind waits for it before the
// machine sleeps, so that onSleep(true) can bring the tunnel down first, and Watch takes it
// again on waking. Close ends the watch and lets go of the lock.
type Inhibitor interface {
	Close() error
}

var (
	// ErrNoUser: logind tracks no such user, so they are not signed in.
	ErrNoUser = errors.New("logind tracks no such user")
	// ErrNoSession: the process belongs to no session, as a program the desktop started
	// under the user's service manager does.
	ErrNoSession = errors.New("the process belongs to no logind session")
)

// Session is what the service needs of a logind session.
type Session struct {
	ID  string
	UID uint32
	// Class is "user" for a session someone signed in to (with "user-early", "user-light"
	// and "user-incomplete" from systemd 256 on), else "greeter", "lock-screen",
	// "background", or "manager" for the user's service manager from systemd 256 on.
	Class string
	// State is "opening", "online", "active" (in front on its seat, or seatless) or
	// "closing": signed out, while some of its processes are left.
	State  string
	Remote bool
}

// SignedIn reports whether the session's user signed in to it and has not signed out yet.
// Their service manager's session, which lingering keeps, and a cron job's do not count.
func (s Session) SignedIn() bool {
	return strings.HasPrefix(s.Class, "user") && s.State != "closing"
}

// ActiveLocal reports whether the session is in front of someone at this machine: active,
// not remote, and one they signed in to (D66).
func (s Session) ActiveLocal() bool {
	return s.Class == "user" && s.State == "active" && !s.Remote
}

// User is what the service needs of a logind user.
type User struct {
	UID  uint32
	Name string
	// Timestamp is when logind began to track the user, in microseconds since the epoch. It
	// changes when they sign in again after they signed out of every session.
	Timestamp uint64
	// Display is the ID of their primary session, graphical if they have one; "" for none.
	Display  string
	Sessions []Session
}

// SignedIn reports whether the user has a session they signed in to that is not closing;
// a lingering user with none counts as signed out.
func (u User) SignedIn() bool {
	return slices.ContainsFunc(u.Sessions, Session.SignedIn)
}

// DisplaySession is the user's primary session, if they have one.
func (u User) DisplaySession() (Session, bool) {
	index := slices.IndexFunc(u.Sessions, func(session Session) bool { return session.ID == u.Display })
	if u.Display == "" || index < 0 {
		return Session{}, false
	}
	return u.Sessions[index], true
}

// Queries is what the service asks logind; tests stand in for it.
type Queries interface {
	// Users lists the uids logind tracks.
	Users() ([]uint32, error)
	// User is uid as logind tracks it, with their sessions; ErrNoUser when it does not.
	User(uid uint32) (User, error)
	// SessionOf is the session process pid belongs to; ErrNoSession when it belongs to none.
	SessionOf(pid uint32) (Session, error)
}

// How Watch follows logind. Signals for one change come in bursts, so the users are looked at
// once they settle. logind before systemd 257 sends nothing when a session starts closing,
// and a user who signed out while a program of theirs runs on keeps it closing, so Watch
// also looks every poll.
const (
	settle = 200 * time.Millisecond
	poll   = time.Minute
)

// event is a logind signal: the machine going to sleep or waking (sleep), or anything that
// may have signed someone in or out.
type event struct {
	sleep, sleeping bool
}

// watcher turns logind's signals into onUser and onSleep calls. Its methods run on one
// goroutine.
type watcher struct {
	queries Queries
	// inhibit takes the delay lock on sleep.
	inhibit  func() (io.Closer, error)
	onUser   func(uid uint32, signedIn bool)
	onSleep  func(sleeping bool)
	log      *slog.Logger
	signedIn map[uint32]bool
	sleeping bool
	lock     io.Closer
}

// start notes who is signed in, which the service reads for itself as it starts, and takes the
// delay lock. Without the lock the tunnel still goes down for sleep, only maybe not before.
func (w *watcher) start() {
	w.signedIn = map[uint32]bool{}
	w.refresh(false)
	w.hold()
}

// refresh looks at every user logind tracks, and at those signed in before, and reports
// those whose sign-in changed when report is set.
func (w *watcher) refresh(report bool) {
	uids, err := w.queries.Users()
	if err != nil {
		w.log.Warn("list logind's users", "error", err)
		return
	}
	for uid := range w.signedIn {
		uids = append(uids, uid)
	}
	slices.Sort(uids)
	for _, uid := range slices.Compact(uids) {
		user, err := w.queries.User(uid)
		if err != nil && !errors.Is(err, ErrNoUser) {
			w.log.Warn("read a logind user", "uid", uid, "error", err)
			continue
		}
		signedIn := err == nil && user.SignedIn()
		if signedIn == w.signedIn[uid] {
			continue
		}
		if signedIn {
			w.signedIn[uid] = true
		} else {
			delete(w.signedIn, uid)
		}
		if report {
			w.onUser(uid, signedIn)
		}
	}
}

// sleep follows PrepareForSleep: going to sleep, onSleep(true) runs before the lock goes, so
// logind waits for it; waking, the lock is back before onSleep(false).
func (w *watcher) sleep(sleeping bool) {
	if sleeping == w.sleeping {
		return
	}
	w.sleeping = sleeping
	if sleeping {
		w.onSleep(true)
		w.release()
		return
	}
	w.hold()
	w.onSleep(false)
}

func (w *watcher) hold() {
	if w.lock != nil {
		return
	}
	lock, err := w.inhibit()
	if err != nil {
		w.log.Warn("take logind's delay lock on sleep", "error", err)
		return
	}
	w.lock = lock
}

func (w *watcher) release() {
	if w.lock == nil {
		return
	}
	if err := w.lock.Close(); err != nil {
		w.log.Warn("let go of logind's delay lock on sleep", "error", err)
	}
	w.lock = nil
}

// run handles events until ctx ends or events closes, then lets go of the lock.
func (w *watcher) run(ctx context.Context, events <-chan event, ticks <-chan time.Time) {
	defer w.release()
	var settled <-chan time.Time
	for {
		select {
		case <-ctx.Done():
			return
		case e, ok := <-events:
			if !ok {
				w.log.Error("lost logind; sign-ins and sleep are no longer followed")
				return
			}
			if e.sleep {
				w.sleep(e.sleeping)
			} else if settled == nil {
				settled = time.After(settle)
			}
		case <-settled:
			settled = nil
			w.refresh(true)
		case <-ticks:
			w.refresh(true)
		}
	}
}

// watch is the Inhibitor Watch returns.
type watch struct {
	cancel context.CancelFunc
	done   <-chan struct{}
	closer io.Closer
	once   sync.Once
	err    error
}

// Close stops the watch, waits for an onSleep or onUser call under way, lets go of the lock
// and closes the connection.
func (w *watch) Close() error {
	w.once.Do(func() {
		w.cancel()
		<-w.done
		w.err = w.closer.Close()
	})
	return w.err
}

// follow starts w on its own goroutine and returns what stops it.
func follow(ctx context.Context, w *watcher, events <-chan event, closer io.Closer) Inhibitor {
	ctx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	w.start()
	go func() {
		defer close(done)
		ticker := time.NewTicker(poll)
		defer ticker.Stop()
		w.run(ctx, events, ticker.C)
	}()
	return &watch{cancel: cancel, done: done, closer: closer}
}

// interesting are the properties of logind's users, sessions and seats whose change may sign
// someone in or out.
var interesting = []string{"Active", "Class", "Display", "Sessions", "State", "ActiveSession"}

// changesSignIn reports whether a PropertiesChanged of logind's, which names properties,
// names one of interesting.
func changesSignIn(properties []string) bool {
	return slices.ContainsFunc(properties, func(name string) bool { return slices.Contains(interesting, name) })
}
