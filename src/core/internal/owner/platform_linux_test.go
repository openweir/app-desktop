package owner

import (
	"bytes"
	"encoding/binary"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"testing"

	"golang.org/x/sys/unix"

	"github.com/openweir/app-desktop/src/core/internal/logind"
	"github.com/openweir/app-desktop/src/core/internal/peer"
	"github.com/openweir/app-desktop/src/core/internal/sysproxy"
)

// seats stands in for logind: its users by uid, and the session of each process.
type seats struct {
	users     map[uint32]logind.User
	processes map[uint32]logind.Session
	err       error
}

func (s *seats) Users() ([]uint32, error) {
	var uids []uint32
	for uid := range s.users {
		uids = append(uids, uid)
	}
	return uids, s.err
}

func (s *seats) User(uid uint32) (logind.User, error) {
	if s.err != nil {
		return logind.User{}, s.err
	}
	user, ok := s.users[uid]
	if !ok {
		return logind.User{}, logind.ErrNoUser
	}
	return user, nil
}

func (s *seats) SessionOf(pid uint32) (logind.Session, error) {
	if s.err != nil {
		return logind.Session{}, s.err
	}
	session, ok := s.processes[pid]
	if !ok {
		return logind.Session{}, logind.ErrNoSession
	}
	return session, nil
}

// desktopProxies stands in for the users' desktops, by the account the platform names.
type desktopProxies map[string]sysproxy.Settings

func (d desktopProxies) Read(user string) (sysproxy.Settings, error) {
	settings, ok := d[user]
	if !ok {
		return sysproxy.Settings{}, sysproxy.ErrUnsupportedDesktop
	}
	return settings, nil
}

func (d desktopProxies) Write(user string, settings sysproxy.Settings) error {
	if _, ok := d[user]; !ok {
		return sysproxy.ErrUnsupportedDesktop
	}
	d[user] = settings
	return nil
}

func graphical(id string, uid uint32) logind.Session {
	return logind.Session{ID: id, UID: uid, Class: "user", State: "active"}
}

func linuxMachine() (*linuxPlatform, *seats, desktopProxies) {
	users := &seats{users: map[uint32]logind.User{}, processes: map[uint32]logind.Session{}}
	proxies := desktopProxies{}
	return &linuxPlatform{restart: func() {}, logind: users, proxies: proxies, dirs: rootDirs}, users, proxies
}

// A user is signed in while they have a session they signed in to that is not closing; their
// service manager's session, which lingering keeps, does not count.
func TestLinuxSessionsAreSignedInUsers(t *testing.T) {
	platform, users, _ := linuxMachine()
	users.users[1000] = logind.User{UID: 1000, Name: "alice", Timestamp: 1795340267506871, Display: "2", Sessions: []logind.Session{
		{ID: "1", UID: 1000, Class: "manager", State: "active"}, graphical("2", 1000),
	}}
	users.users[1001] = logind.User{UID: 1001, Name: "bob", Timestamp: 1795340267000000, Sessions: []logind.Session{
		{ID: "3", UID: 1001, Class: "manager", State: "active"},
		{ID: "4", UID: 1001, Class: "user", State: "closing"},
		{ID: "5", UID: 1001, Class: "background", State: "active"},
	}}
	if got := platform.SignedIn("1000"); !slices.Equal(got, []uint32{1000}) {
		t.Fatalf("alice is signed in to %v", got)
	}
	for _, id := range []string{"1001", "1002", "01000", alice + " "} {
		if got := platform.SignedIn(id); got != nil {
			t.Errorf("%q is signed in to %v", id, got)
		}
	}
	if user, err := platform.SessionUser(1000); err != nil || user != "1000" {
		t.Fatalf("session 1000's user = %q, %v", user, err)
	}
	if _, err := platform.SessionUser(1001); err == nil {
		t.Fatal("bob's lingering manager counts as signed in")
	}
	if logon, err := platform.SessionLogonTime(1000); err != nil || logon != 1795340267506871 {
		t.Fatalf("alice signed in at %d, %v", logon, err)
	}
	if _, err := platform.SessionLogonTime(1001); err == nil {
		t.Fatal("bob has a sign-in time while signed out")
	}
	users.err = errors.New("no bus")
	if got := platform.SignedIn("1000"); got != nil {
		t.Fatalf("signed in to %v without logind", got)
	}
}

// Only a user in an active local session may claim (D66). A program the desktop started under
// the user's service manager is in no session, and their display session decides.
func TestLinuxClaimNeedsAnActiveLocalSession(t *testing.T) {
	platform, users, _ := linuxMachine()
	user := logind.User{UID: 1000, Name: "alice", Display: "2", Sessions: []logind.Session{
		graphical("2", 1000), {ID: "7", UID: 1000, Class: "user", State: "active", Remote: true},
	}}
	users.users[1000] = user
	if err := platform.MayClaim(peer.Identity{UserID: "1000", SessionID: 1000}); err != nil {
		t.Fatalf("claim from the display session: %v", err)
	}
	for name, display := range map[string]logind.Session{
		"remote":      {ID: "2", UID: 1000, Class: "user", State: "active", Remote: true},
		"in the back": {ID: "2", UID: 1000, Class: "user", State: "online"},
		"closing":     {ID: "2", UID: 1000, Class: "user", State: "closing"},
		"greeter":     {ID: "2", UID: 1000, Class: "greeter", State: "active"},
	} {
		users.users[1000] = logind.User{UID: 1000, Display: "2", Sessions: []logind.Session{display}}
		if err := platform.MayClaim(peer.Identity{UserID: "1000"}); !errors.Is(err, errNotLocal) {
			t.Errorf("claim from a display session %s: %v", name, err)
		}
	}
	users.users[1000] = logind.User{UID: 1000, Sessions: []logind.Session{graphical("2", 1000)}}
	if err := platform.MayClaim(peer.Identity{UserID: "1000"}); !errors.Is(err, errNotLocal) {
		t.Errorf("claim without a display session: %v", err)
	}
	if err := platform.MayClaim(peer.Identity{UserID: "1001"}); !errors.Is(err, errNotLocal) {
		t.Errorf("claim by a user logind does not track: %v", err)
	}
	if err := platform.MayClaim(peer.Identity{UserID: alice + "x"}); err == nil {
		t.Error("claim without a uid")
	}

	// The caller's own session decides where it has one: over SSH they may not claim, though
	// they sit at the machine too.
	users.users[1000] = user
	users.processes[40] = user.Sessions[1]
	users.processes[41] = user.Sessions[0]
	users.processes[42] = graphical("9", 1001) // su to alice in bob's session, or a stale pid
	users.processes[44] = logind.Session{ID: "1", UID: 1000, Class: "manager", State: "active"}
	for _, pid := range []uint32{40, 42} {
		if err := platform.mayClaim(1000, pid); !errors.Is(err, errNotLocal) {
			t.Errorf("claim from process %d: %v", pid, err)
		}
	}
	for _, pid := range []uint32{41, 43, 44} {
		if err := platform.mayClaim(1000, pid); err != nil {
			t.Errorf("claim from process %d: %v", pid, err)
		}
	}
	users.err = errors.New("no bus")
	if err := platform.mayClaim(1000, 41); err == nil || errors.Is(err, errNotLocal) {
		t.Errorf("claim without logind: %v", err)
	}
}

// The service sets a user's proxy through systemd-run under the name logind knows them by, or
// their uid where that name would not parse; never for a user who is not signed in.
func TestLinuxProxyIsTheSignedInUsers(t *testing.T) {
	platform, users, proxies := linuxMachine()
	users.users[1000] = logind.User{UID: 1000, Name: "alice", Sessions: []logind.Session{graphical("2", 1000)}}
	users.users[1001] = logind.User{UID: 1001, Name: "bob@corp.example", Sessions: []logind.Session{graphical("3", 1001)}}
	users.users[1002] = logind.User{UID: 1002, Name: "carol", Sessions: []logind.Session{{ID: "4", UID: 1002, Class: "manager", State: "active"}}}
	theirs := sysproxy.Settings{Flags: sysproxy.Direct, Desktop: &sysproxy.Desktop{GNOME: map[string]string{"mode": "'none'"}}}
	proxies["alice"], proxies["1001"], proxies["carol"] = theirs, theirs, theirs
	if settings, err := platform.ReadProxy(1000); err != nil || settings.Desktop != theirs.Desktop {
		t.Fatalf("alice's proxy = %+v, %v", settings, err)
	}
	if err := platform.WriteProxy(1001, sysproxy.Core(9347)); err != nil || proxies["1001"] != sysproxy.Core(9347) {
		t.Fatalf("bob's proxy = %+v, %v", proxies["1001"], err)
	}
	if err := platform.WriteProxy(1002, sysproxy.Core(9347)); err == nil || proxies["carol"] != theirs {
		t.Fatalf("wrote the proxy of a user who is not signed in: %v", err)
	}
	users.users[1000] = logind.User{UID: 1000, Name: "alice", Sessions: []logind.Session{graphical("2", 1000)}}
	delete(proxies, "alice")
	if _, err := platform.ReadProxy(1000); !errors.Is(err, sysproxy.ErrUnsupportedDesktop) {
		t.Fatalf("read on a desktop without proxy settings: %v", err)
	}
}

// fakeACLs records the ACLs secure sets, as a file system that has them would keep them, or
// fails as one without them does.
type fakeACLs struct {
	set     map[string][]byte
	removed []string
	err     error
}

func (f *fakeACLs) dirs() dirs {
	return dirs{
		owner: uint32(os.Getuid()),
		setxattr: func(_ int, name string, data []byte) error {
			if f.err != nil {
				return f.err
			}
			f.set[name] = data
			return nil
		},
		removexattr: func(_ int, name string) error {
			f.removed = append(f.removed, name)
			if f.err != nil {
				return f.err
			}
			return unix.ENODATA
		},
	}
}

func mode(t *testing.T, path string) os.FileMode {
	t.Helper()
	info, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	return info.Mode().Perm()
}

// Root alone writes the service's directories. Others may pass through those without a reader,
// to the home whose ACL lets its owner read it; a link, a file or someone else's directory
// is refused.
func TestLinuxSecureDir(t *testing.T) {
	root := t.TempDir()
	acls := &fakeACLs{set: map[string][]byte{}}
	d := acls.dirs()
	users := filepath.Join(root, usersDir)
	if err := os.Mkdir(users, 0o777); err != nil {
		t.Fatal(err)
	}
	for _, dir := range []string{root, users} {
		if err := d.secure(dir, ""); err != nil {
			t.Fatal(err)
		}
		if got := mode(t, dir); got != 0o711 {
			t.Fatalf("%s has mode %v", dir, got)
		}
	}
	if !slices.Equal(acls.removed, []string{accessACL, defaultACL, accessACL, defaultACL}) {
		t.Fatalf("removed %v", acls.removed)
	}
	home := filepath.Join(users, "1000")
	if err := d.secure(home, "1000"); err != nil {
		t.Fatal(err)
	}
	if got := mode(t, home); got != 0o700 {
		t.Fatalf("home has mode %v", got)
	}
	want := binary.LittleEndian.AppendUint32(nil, 2)
	for _, entry := range [][3]uint32{{0x01, 7, 0xffffffff}, {0x02, 5, 1000}, {0x04, 0, 0xffffffff}, {0x10, 5, 0xffffffff}, {0x20, 0, 0xffffffff}} {
		want = binary.LittleEndian.AppendUint16(want, uint16(entry[0]))
		want = binary.LittleEndian.AppendUint16(want, uint16(entry[1]))
		want = binary.LittleEndian.AppendUint32(want, entry[2])
	}
	if !bytes.Equal(acls.set[accessACL], want) || !bytes.Equal(acls.set[defaultACL], want) {
		t.Fatalf("ACLs %x", acls.set)
	}
	// Without ACLs the home stays root's alone.
	acls.err = unix.EOPNOTSUPP
	if err := d.secure(filepath.Join(users, "1001"), "1001"); err != nil {
		t.Fatal(err)
	}
	acls.err = unix.EIO
	if err := d.secure(filepath.Join(users, "1002"), "1002"); err == nil {
		t.Fatal("secured a home whose ACL could not be set")
	}
	acls.err = nil

	if err := os.Symlink(home, filepath.Join(users, "1003")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(users, "1004"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	for _, uid := range []string{"1003", "1004"} {
		if err := d.secure(filepath.Join(users, uid), uid); err == nil {
			t.Errorf("secured %s, which is no plain directory", uid)
		}
	}
	if err := d.secure(filepath.Join(users, "1005"), "not a uid"); err == nil {
		t.Error("secured a home for a reader with no uid")
	}
	someoneElse := d
	someoneElse.owner++
	if err := someoneElse.secure(home, "1000"); err == nil {
		t.Error("secured a directory that is not root's")
	}
}

// The ACL secure writes is one the kernel takes, where the test's file system has ACLs: the
// owner of a file may set its ACL.
func TestLinuxSecureDirSetsAnACLTheKernelTakes(t *testing.T) {
	home := filepath.Join(t.TempDir(), "1000")
	d := rootDirs
	d.owner = uint32(os.Getuid())
	if err := os.Mkdir(home, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := unix.Setxattr(home, accessACL, readerACL(uint32(os.Getuid())+1), 0); unsupported(err) {
		t.Skip("the file system has no ACLs")
	}
	if err := d.secure(home, "1000"); err != nil {
		t.Fatal(err)
	}
	buffer := make([]byte, 256)
	for _, name := range []string{accessACL, defaultACL} {
		n, err := unix.Getxattr(home, name, buffer)
		if err != nil || !bytes.Equal(buffer[:n], readerACL(1000)) {
			t.Fatalf("%s = %x, %v", name, buffer[:n], err)
		}
	}
	// The access ACL's mask is the mode's group bits.
	if got := mode(t, home); got != 0o750 {
		t.Fatalf("home has mode %v", got)
	}
	// A directory without a reader loses the ACL.
	if err := d.secure(home, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := unix.Getxattr(home, accessACL, buffer); !errors.Is(err, unix.ENODATA) {
		t.Fatalf("the ACL is still there: %v", err)
	}
	if got := mode(t, home); got != 0o711 {
		t.Fatalf("mode %v", got)
	}
}
