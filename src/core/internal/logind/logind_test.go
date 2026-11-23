package logind

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"slices"
	"sync"
	"testing"
	"time"
)

// seats stands in for logind.
type seats struct {
	mu    sync.Mutex
	users map[uint32]User
	err   error
}

func (s *seats) Users() ([]uint32, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var uids []uint32
	for uid := range s.users {
		uids = append(uids, uid)
	}
	return uids, s.err
}

func (s *seats) User(uid uint32) (User, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.err != nil {
		return User{}, s.err
	}
	user, ok := s.users[uid]
	if !ok {
		return User{}, ErrNoUser
	}
	return user, nil
}

func (s *seats) SessionOf(uint32) (Session, error) { return Session{}, ErrNoSession }

func (s *seats) set(uid uint32, sessions ...Session) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if sessions == nil {
		delete(s.users, uid)
		return
	}
	s.users[uid] = User{UID: uid, Sessions: sessions}
}

// lock is a delay lock: held until closed.
type lock struct{ held *int }

func (l lock) Close() error { *l.held--; return nil }

// recorder is what the watcher reported, and how many delay locks it held when.
type recorder struct {
	mu     sync.Mutex
	users  []string
	sleeps []bool
	held   int
	// heldAsleep is how many locks were held as each onSleep ran.
	heldAsleep []int
}

func (r *recorder) watcher(queries Queries) *watcher {
	return &watcher{
		queries: queries,
		inhibit: func() (io.Closer, error) { r.held++; return lock{&r.held}, nil },
		onUser: func(uid uint32, signedIn bool) {
			r.mu.Lock()
			defer r.mu.Unlock()
			state := "out"
			if signedIn {
				state = "in"
			}
			r.users = append(r.users, string(rune('0'+uid%10))+" "+state)
		},
		onSleep: func(sleeping bool) {
			r.mu.Lock()
			defer r.mu.Unlock()
			r.sleeps = append(r.sleeps, sleeping)
			r.heldAsleep = append(r.heldAsleep, r.held)
		},
		log: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

func (r *recorder) reported() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return slices.Clone(r.users)
}

func user(id string, uid uint32) Session {
	return Session{ID: id, UID: uid, Class: "user", State: "active"}
}

func TestSignedIn(t *testing.T) {
	for _, session := range []Session{
		user("1", 1000),
		{Class: "user", State: "online"},
		{Class: "user", State: "opening"},
		{Class: "user", State: "active", Remote: true},
		{Class: "user-early", State: "active"},
		{Class: "user-light", State: "online"},
	} {
		if !session.SignedIn() {
			t.Errorf("%+v is not signed in", session)
		}
	}
	for _, session := range []Session{
		{Class: "user", State: "closing"},
		{Class: "manager", State: "active"},
		{Class: "background", State: "active"},
		{Class: "greeter", State: "active"},
		{Class: "lock-screen", State: "active"},
	} {
		if session.SignedIn() {
			t.Errorf("%+v is signed in", session)
		}
	}
	lingering := User{UID: 1000, Sessions: []Session{{ID: "1", Class: "manager", State: "active"}}}
	if lingering.SignedIn() || (User{UID: 1000}).SignedIn() {
		t.Error("a lingering user is signed in")
	}
	if _, ok := lingering.DisplaySession(); ok {
		t.Error("a lingering user has a display session")
	}
	signedIn := User{UID: 1000, Display: "2", Sessions: []Session{lingering.Sessions[0], user("2", 1000)}}
	if display, ok := signedIn.DisplaySession(); !signedIn.SignedIn() || !ok || display.ID != "2" {
		t.Errorf("display session %+v, %v", display, ok)
	}
	if !user("2", 1000).ActiveLocal() {
		t.Error("an active local session is not")
	}
	for _, session := range []Session{
		{Class: "user", State: "active", Remote: true},
		{Class: "user", State: "online"},
		{Class: "user-light", State: "active"},
		{Class: "greeter", State: "active"},
	} {
		if session.ActiveLocal() {
			t.Errorf("%+v is active and local", session)
		}
	}
}

// The watcher reports changes from what it found as it started, and keeps users it lost
// track of until logind says they are gone.
func TestWatcherReportsSignInChanges(t *testing.T) {
	logind := &seats{users: map[uint32]User{}}
	logind.set(1000, user("1", 1000))
	logind.set(1001, Session{ID: "2", UID: 1001, Class: "manager", State: "active"})
	r := &recorder{}
	w := r.watcher(logind)
	w.start()
	if got := r.reported(); len(got) != 0 || r.held != 1 {
		t.Fatalf("reported %v at start, %d locks", got, r.held)
	}
	w.refresh(true)
	if got := r.reported(); len(got) != 0 {
		t.Fatalf("reported %v with nothing changed", got)
	}
	logind.set(1001, Session{ID: "2", UID: 1001, Class: "manager", State: "active"}, user("3", 1001))
	logind.set(1000, Session{ID: "1", UID: 1000, Class: "user", State: "closing"})
	w.refresh(true)
	if got := r.reported(); !slices.Equal(got, []string{"0 out", "1 in"}) {
		t.Fatalf("reported %v", got)
	}
	// While logind cannot answer, nobody changes.
	logind.err = errors.New("no bus")
	logind.set(1001)
	w.refresh(true)
	logind.err = nil
	w.refresh(true)
	if got := r.reported(); !slices.Equal(got, []string{"0 out", "1 in", "1 out"}) {
		t.Fatalf("reported %v", got)
	}
}

// Going to sleep, the lock stays until onSleep(true) returns; waking, it is back before
// onSleep(false). A repeated signal changes nothing.
func TestWatcherHoldsSleepForTheTunnel(t *testing.T) {
	r := &recorder{}
	w := r.watcher(&seats{users: map[uint32]User{}})
	w.start()
	w.sleep(false)
	w.sleep(true)
	w.sleep(true)
	if r.held != 0 {
		t.Fatalf("%d locks held asleep", r.held)
	}
	w.sleep(false)
	if !slices.Equal(r.sleeps, []bool{true, false}) || !slices.Equal(r.heldAsleep, []int{1, 1}) || r.held != 1 {
		t.Fatalf("sleeps %v with %v locks, %d held", r.sleeps, r.heldAsleep, r.held)
	}
	// Without a lock, the tunnel still goes down for sleep.
	w.release()
	w.inhibit = func() (io.Closer, error) { return nil, errors.New("denied") }
	w.sleep(true)
	w.sleep(false)
	if !slices.Equal(r.sleeps, []bool{true, false, true, false}) || w.lock != nil {
		t.Fatalf("sleeps %v", r.sleeps)
	}
}

// follow handles events on its own goroutine until closed: a burst of sign-in signals is one
// look at the users, and closing lets go of the lock.
func TestFollow(t *testing.T) {
	logind := &seats{users: map[uint32]User{}}
	r := &recorder{}
	w := r.watcher(logind)
	events := make(chan event)
	closed := false
	watch := follow(context.Background(), w, events, closerFunc(func() error { closed = true; return nil }))
	logind.set(1000, user("1", 1000))
	for range 3 {
		events <- event{}
	}
	deadline := time.Now().Add(5 * time.Second)
	for len(r.reported()) == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	events <- event{sleep: true, sleeping: true}
	events <- event{sleep: true, sleeping: false}
	if err := watch.Close(); err != nil || !closed {
		t.Fatalf("close = %v, closed %v", err, closed)
	}
	if got := r.reported(); !slices.Equal(got, []string{"0 in"}) || !slices.Equal(r.sleeps, []bool{true, false}) || r.held != 0 {
		t.Fatalf("reported %v, sleeps %v, %d locks held", got, r.sleeps, r.held)
	}
	if err := watch.Close(); err != nil {
		t.Fatal(err)
	}
}

type closerFunc func() error

func (f closerFunc) Close() error { return f() }

func TestChangesSignIn(t *testing.T) {
	if !changesSignIn([]string{"IdleHint", "State"}) || !changesSignIn([]string{"Display"}) {
		t.Error("missed a change of who is signed in")
	}
	if changesSignIn([]string{"IdleHint", "IdleSinceHint", "LockedHint"}) || changesSignIn(nil) {
		t.Error("a change of idleness is one of who is signed in")
	}
}
