package logind

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"slices"
	"sync"
	"time"

	"github.com/godbus/dbus/v5"

	"github.com/openweir/app-desktop/src/core/internal/product"
)

// logind's manager on the system bus, and what the service calls there.
const (
	destination                      = "org.freedesktop.login1"
	managerPath      dbus.ObjectPath = "/org/freedesktop/login1"
	manager                          = "org.freedesktop.login1.Manager"
	userInterface                    = "org.freedesktop.login1.User"
	sessionInterface                 = "org.freedesktop.login1.Session"
	properties                       = "org.freedesktop.DBus.Properties"

	// callTimeout bounds a call, so that a stuck logind does not hold a claim or a sign-in.
	callTimeout = 5 * time.Second
)

// Bus is logind on the system bus; it implements Queries.
type Bus struct {
	mu   sync.Mutex
	conn *dbus.Conn
	// dial connects again once conn is gone; Watch's Bus has none and keeps its conn.
	dial func() (*dbus.Conn, error)
}

var _ Queries = (*Bus)(nil)

// System is logind on the system bus. It connects when first asked, and again after the
// connection was lost, so that the service can start before it needs logind.
func System() *Bus {
	return &Bus{dial: func() (*dbus.Conn, error) { return dbus.ConnectSystemBus() }}
}

func (b *Bus) connection() (*dbus.Conn, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.conn != nil && b.conn.Connected() {
		return b.conn, nil
	}
	if b.dial == nil {
		return nil, errors.New("the connection to the system bus is closed")
	}
	conn, err := b.dial()
	if err != nil {
		return nil, fmt.Errorf("connect to the system bus: %w", err)
	}
	b.conn = conn
	return conn, nil
}

// Close closes the connection; a Bus from System connects again when asked.
func (b *Bus) Close() error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.conn == nil {
		return nil
	}
	conn := b.conn
	b.conn = nil
	return conn.Close()
}

// call calls logind's method on path with args and stores its answer in results.
func (b *Bus) call(path dbus.ObjectPath, method string, args []any, results ...any) error {
	conn, err := b.connection()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), callTimeout)
	defer cancel()
	return conn.Object(destination, path).CallWithContext(ctx, method, 0, args...).Store(results...)
}

// properties are those of path's iface.
func (b *Bus) properties(path dbus.ObjectPath, iface string) (map[string]dbus.Variant, error) {
	var values map[string]dbus.Variant
	err := b.call(path, properties+".GetAll", []any{iface}, &values)
	return values, err
}

// named reports whether err is logind's or the bus's error reply name.
func named(err error, names ...string) bool {
	var reply dbus.Error
	return errors.As(err, &reply) && slices.Contains(names, reply.Name)
}

// gone reports whether err says the object went away while the service asked about it.
func gone(err error) bool {
	return named(err, "org.freedesktop.DBus.Error.UnknownObject", "org.freedesktop.DBus.Error.UnknownMethod",
		"org.freedesktop.login1.NoSuchUser", "org.freedesktop.login1.NoSuchSession")
}

func (b *Bus) Users() ([]uint32, error) {
	var users []struct {
		UID  uint32
		Name string
		Path dbus.ObjectPath
	}
	if err := b.call(managerPath, manager+".ListUsers", nil, &users); err != nil {
		return nil, fmt.Errorf("list logind's users: %w", err)
	}
	uids := make([]uint32, 0, len(users))
	for _, user := range users {
		uids = append(uids, user.UID)
	}
	return uids, nil
}

func (b *Bus) User(uid uint32) (User, error) {
	var path dbus.ObjectPath
	if err := b.call(managerPath, manager+".GetUser", []any{uid}, &path); err != nil {
		if named(err, "org.freedesktop.login1.NoSuchUser") {
			return User{}, ErrNoUser
		}
		return User{}, fmt.Errorf("find logind's user %d: %w", uid, err)
	}
	values, err := b.properties(path, userInterface)
	if gone(err) {
		return User{}, ErrNoUser
	} else if err != nil {
		return User{}, fmt.Errorf("read logind's user %d: %w", uid, err)
	}
	user, sessions, err := decodeUser(values)
	if err != nil {
		return User{}, fmt.Errorf("read logind's user %d: %w", uid, err)
	}
	for _, path := range sessions {
		values, err := b.properties(path, sessionInterface)
		if gone(err) {
			continue // it ended meanwhile
		} else if err != nil {
			return User{}, fmt.Errorf("read a session of logind's user %d: %w", uid, err)
		}
		session, err := decodeSession(values)
		if err != nil {
			return User{}, fmt.Errorf("read a session of logind's user %d: %w", uid, err)
		}
		user.Sessions = append(user.Sessions, session)
	}
	return user, nil
}

func (b *Bus) SessionOf(pid uint32) (Session, error) {
	var path dbus.ObjectPath
	if err := b.call(managerPath, manager+".GetSessionByPID", []any{pid}, &path); err != nil {
		if named(err, "org.freedesktop.login1.NoSessionForPID") {
			return Session{}, ErrNoSession
		}
		return Session{}, fmt.Errorf("find the logind session of process %d: %w", pid, err)
	}
	values, err := b.properties(path, sessionInterface)
	if gone(err) {
		return Session{}, ErrNoSession
	} else if err != nil {
		return Session{}, fmt.Errorf("read the logind session of process %d: %w", pid, err)
	}
	session, err := decodeSession(values)
	if err != nil {
		return Session{}, fmt.Errorf("read the logind session of process %d: %w", pid, err)
	}
	return session, nil
}

// inhibit takes a delay lock on sleep, which lasts until the file it returns is closed.
func (b *Bus) inhibit() (io.Closer, error) {
	var fd dbus.UnixFD
	if err := b.call(managerPath, manager+".Inhibit", []any{"sleep", product.DisplayName,
		"Bring the tunnel down before the machine sleeps", "delay"}, &fd); err != nil {
		return nil, err
	}
	return os.NewFile(uintptr(fd), "logind delay lock"), nil
}

// objectRef is logind's (so): an ID and the object's path.
type objectRef struct {
	ID   string
	Path dbus.ObjectPath
}

// userRef is a session's User, (uo).
type userRef struct {
	UID  uint32
	Path dbus.ObjectPath
}

// decodeUser reads a User's properties, and the paths of its sessions.
func decodeUser(values map[string]dbus.Variant) (User, []dbus.ObjectPath, error) {
	var user User
	var display objectRef
	var sessions []objectRef
	for name, target := range map[string]any{
		"UID": &user.UID, "Name": &user.Name, "Timestamp": &user.Timestamp, "Display": &display, "Sessions": &sessions,
	} {
		if err := store(values, name, target); err != nil {
			return User{}, nil, err
		}
	}
	user.Display = display.ID
	paths := make([]dbus.ObjectPath, 0, len(sessions))
	for _, session := range sessions {
		paths = append(paths, session.Path)
	}
	return user, paths, nil
}

// decodeSession reads a Session's properties.
func decodeSession(values map[string]dbus.Variant) (Session, error) {
	var session Session
	var user userRef
	for name, target := range map[string]any{
		"Id": &session.ID, "User": &user, "Class": &session.Class, "State": &session.State, "Remote": &session.Remote,
	} {
		if err := store(values, name, target); err != nil {
			return Session{}, err
		}
	}
	session.UID = user.UID
	return session, nil
}

func store(values map[string]dbus.Variant, name string, target any) error {
	value, ok := values[name]
	if !ok {
		return fmt.Errorf("logind gave no %s", name)
	}
	if err := value.Store(target); err != nil {
		return fmt.Errorf("logind's %s: %w", name, err)
	}
	return nil
}

// Watch reports each user signing in (a session they signed in to that is not closing
// appears; lingering counts as signed out) and signing out with onUser, and the machine going
// to sleep (true) and waking (false) with onSleep, until ctx ends or the Inhibitor is closed.
// It reports changes only: who is signed in as it starts the service reads for itself. It
// holds a delay lock on sleep, which it lets go once onSleep(true) returned, so onSleep(true)
// should return within logind's InhibitDelayMaxSec (5 seconds by default).
func Watch(ctx context.Context, onUser func(uid uint32, signedIn bool), onSleep func(sleeping bool)) (Inhibitor, error) {
	conn, err := dbus.ConnectSystemBus()
	if err != nil {
		return nil, fmt.Errorf("connect to the system bus: %w", err)
	}
	for _, match := range [][]dbus.MatchOption{
		{dbus.WithMatchSender(destination), dbus.WithMatchObjectPath(managerPath), dbus.WithMatchInterface(manager)},
		{dbus.WithMatchSender(destination), dbus.WithMatchPathNamespace(managerPath),
			dbus.WithMatchInterface(properties), dbus.WithMatchMember("PropertiesChanged")},
	} {
		if err := conn.AddMatchSignal(match...); err != nil {
			conn.Close()
			return nil, fmt.Errorf("follow %s: %w", destination, err)
		}
	}
	// Deep enough that a burst of signals keeps its order: godbus hands the ones that do not
	// fit to goroutines of their own.
	signals := make(chan *dbus.Signal, 256)
	conn.Signal(signals)
	events := make(chan event)
	ctx, cancel := context.WithCancel(ctx)
	go func() {
		defer close(events)
		for signal := range signals {
			e, ok := eventOf(signal)
			if !ok {
				continue
			}
			select {
			case events <- e:
			case <-ctx.Done():
				return
			}
		}
	}()
	bus := &Bus{conn: conn}
	w := &watcher{queries: bus, inhibit: bus.inhibit, onUser: onUser, onSleep: onSleep, log: slog.Default()}
	inhibitor := follow(ctx, w, events, bus)
	return closing{inhibitor, cancel}, nil
}

// closing also ends Watch's own context.
type closing struct {
	Inhibitor
	cancel context.CancelFunc
}

func (c closing) Close() error {
	defer c.cancel()
	return c.Inhibitor.Close()
}

// eventOf is what a signal from logind means to the watch, if anything.
func eventOf(signal *dbus.Signal) (event, bool) {
	switch signal.Name {
	case manager + ".PrepareForSleep":
		if len(signal.Body) == 1 {
			if sleeping, ok := signal.Body[0].(bool); ok {
				return event{sleep: true, sleeping: sleeping}, true
			}
		}
		return event{}, false
	case manager + ".UserNew", manager + ".UserRemoved", manager + ".SessionNew", manager + ".SessionRemoved":
		return event{}, true
	case properties + ".PropertiesChanged":
		var names []string
		if len(signal.Body) == 3 {
			if changed, ok := signal.Body[1].(map[string]dbus.Variant); ok {
				for name := range changed {
					names = append(names, name)
				}
			}
			if invalidated, ok := signal.Body[2].([]string); ok {
				names = append(names, invalidated...)
			}
		}
		return event{}, changesSignIn(names)
	}
	return event{}, false
}
