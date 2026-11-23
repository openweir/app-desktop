package peer

import (
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

// dialEnv makes the test binary a client that connects to the socket it names and exits.
const dialEnv = "OPENWEIR_TEST_PEER_DIAL"

func TestMain(m *testing.M) {
	if socket := os.Getenv(dialEnv); socket != "" {
		if _, err := net.Dial("unix", socket); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		os.Exit(0)
	}
	os.Exit(m.Run())
}

// installation stands in for /opt/openweir with files of the test's own user, and for
// procfs with fake data for the processes that run them.
type installation struct {
	top, ui, service, proc string
}

func install(t *testing.T) installation {
	t.Helper()
	top := t.TempDir()
	dir := filepath.Join(top, "opt", "openweir")
	for _, each := range []string{filepath.Join(top, "opt"), dir} {
		mkdir(t, each)
	}
	i := installation{top: top, ui: filepath.Join(dir, "openweir"), service: filepath.Join(dir, "openweir-service"),
		proc: filepath.Join(top, "proc")}
	for _, executable := range []string{i.ui, i.service} {
		executableFile(t, executable)
	}
	return i
}

// mkdir makes dir with no write for the group or others, whatever the umask.
func mkdir(t *testing.T, dir string) {
	t.Helper()
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dir, 0o755); err != nil {
		t.Fatal(err)
	}
}

func executableFile(t *testing.T, path string) {
	t.Helper()
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, 0o755); err != nil {
		t.Fatal(err)
	}
}

func (i installation) clients() clients {
	return clients{proc: i.proc, owner: uint32(os.Getuid()), top: i.top, ui: i.ui, service: i.service}
}

// runs fakes procfs for pid running executable as uid, started start ticks after boot.
func (i installation) runs(t *testing.T, pid int, executable string, uid uint32, start uint64) {
	t.Helper()
	dir := filepath.Join(i.proc, strconv.Itoa(pid))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(executable, filepath.Join(dir, "exe")); err != nil {
		t.Fatal(err)
	}
	status := fmt.Sprintf("Name:\topenweir\nUmask:\t0022\nState:\tS (sleeping)\nUid:\t%d\t%d\t%d\t%d\nGid:\t0\t0\t0\t0\n", uid, uid, uid, uid)
	// The state (field 3), fields 4 to 21, starttime (22) and a few more; the name has what
	// trips a naive parser.
	fields := strings.Repeat("0 ", 18)
	stat := fmt.Sprintf("%d (open weir) x) S %s%d 0 0 0\n", pid, fields, start)
	for name, content := range map[string]string{"status": status, "stat": stat} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// The UI is anyone's but root's, and the service's executable root's alone, as the helper
// an administrator confirmed through pkexec.
func TestLinuxClientsAreTheUIAndTheHelper(t *testing.T) {
	i := install(t)
	other := filepath.Join(i.top, "opt", "openweir", "other")
	executableFile(t, other)
	i.runs(t, 100, i.ui, 1000, 1)
	i.runs(t, 101, i.service, 0, 1)
	i.runs(t, 102, i.service, 1000, 1)
	i.runs(t, 103, i.ui, 0, 1)
	i.runs(t, 104, other, 1000, 1)
	i.runs(t, 105, i.ui, 1001, 1)
	c := i.clients()
	for _, tc := range []struct {
		name string
		uid  uint32
		pid  int
		want Identity
	}{
		{name: "the UI", uid: 1000, pid: 100, want: Identity{UserID: "1000", SessionID: 1000, ProcessID: 100}},
		{name: "the helper as root", uid: 0, pid: 101, want: Identity{UserID: "0", ProcessID: 101, Administrator: true}},
		{name: "the helper as a user", uid: 1000, pid: 102},
		{name: "the UI as root", uid: 0, pid: 103},
		{name: "another program", uid: 1000, pid: 104},
		{name: "a process that changed its user since it connected", uid: 1000, pid: 105},
		{name: "a process that is gone", uid: 1000, pid: 106},
	} {
		identity, err := c.identify(tc.uid, tc.pid)
		switch {
		case tc.want == (Identity{}) && err == nil:
			t.Errorf("%s: accepted as %+v", tc.name, identity)
		case tc.want != (Identity{}) && (err != nil || identity != tc.want):
			t.Errorf("%s: identity = %+v, %v; want %+v", tc.name, identity, err, tc.want)
		}
	}
}

// What the caller runs must be root's file at the installed path, in directories that only
// root may change, or another user could have put their own program there.
func TestLinuxClientsRunRootsInstallation(t *testing.T) {
	for name, change := range map[string]func(t *testing.T, i *installation, c *clients){
		"someone else's files": func(t *testing.T, i *installation, c *clients) { c.owner++ },
		"a directory the group may write": func(t *testing.T, i *installation, c *clients) {
			chmod(t, filepath.Dir(i.ui), 0o775)
		},
		"a directory anyone may write": func(t *testing.T, i *installation, c *clients) {
			chmod(t, filepath.Join(i.top, "opt"), 0o1777)
		},
		"the top directory anyone may write": func(t *testing.T, i *installation, c *clients) {
			chmod(t, i.top, 0o777)
		},
		"an executable anyone may write": func(t *testing.T, i *installation, c *clients) {
			chmod(t, i.ui, 0o757)
		},
		"a link at the installed path": func(t *testing.T, i *installation, c *clients) {
			elsewhere := filepath.Join(i.top, "elsewhere")
			executableFile(t, elsewhere)
			replace(t, i.ui, func() error { return os.Symlink(elsewhere, i.ui) })
		},
		"a directory that is a link": func(t *testing.T, i *installation, c *clients) {
			real := filepath.Join(i.top, "real")
			mkdir(t, real)
			if err := os.Rename(filepath.Join(i.top, "opt"), filepath.Join(real, "opt")); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(filepath.Join(real, "opt"), filepath.Join(i.top, "opt")); err != nil {
				t.Fatal(err)
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			i := install(t)
			c := i.clients()
			change(t, &i, &c)
			i.runs(t, 100, i.ui, 1000, 1)
			if identity, err := c.identify(1000, 100); err == nil {
				t.Fatalf("accepted as %+v", identity)
			}
		})
	}
	t.Run("the installation as it should be", func(t *testing.T) {
		i := install(t)
		i.runs(t, 100, i.ui, 1000, 1)
		if _, err := i.clients().identify(1000, 100); err != nil {
			t.Fatal(err)
		}
	})
}

// By name a process may run the installed path while it runs another file: one that another
// user mounted over it in a mount namespace of their own, or the one there before an upgrade.
func TestLinuxClientsRunTheFileAtThePathNow(t *testing.T) {
	i := install(t)
	c := i.clients()
	var ui, other unix.Stat_t
	if err := unix.Stat(i.ui, &ui); err != nil {
		t.Fatal(err)
	}
	if err := unix.Stat(i.service, &other); err != nil {
		t.Fatal(err)
	}
	if _, err := c.decide(1000, process{pid: 100, executable: i.ui, image: ui, uid: 1000}); err != nil {
		t.Fatal(err)
	}
	if identity, err := c.decide(1000, process{pid: 100, executable: i.ui, image: other, uid: 1000}); err == nil {
		t.Fatalf("accepted as %+v", identity)
	}
}

func chmod(t *testing.T, path string, mode os.FileMode) {
	t.Helper()
	if err := os.Chmod(path, mode); err != nil {
		t.Fatal(err)
	}
}

func replace(t *testing.T, path string, create func() error) {
	t.Helper()
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := create(); err != nil {
		t.Fatal(err)
	}
}

// socketPair connects to a new socket from this process, or from a child process that has
// exited by the time the connection is accepted, and returns the accepted end.
func socketPair(t *testing.T, fromChild bool) (net.Conn, int) {
	t.Helper()
	// A short path: a socket's must fit in 108 bytes.
	dir, err := os.MkdirTemp("", "peer-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	listener, err := net.Listen("unix", filepath.Join(dir, "s"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	pid := os.Getpid()
	if fromChild {
		child := exec.Command(os.Args[0])
		child.Env = append(os.Environ(), dialEnv+"="+listener.Addr().String())
		if output, err := child.CombinedOutput(); err != nil {
			t.Fatalf("client: %v\n%s", err, output)
		}
		pid = child.Process.Pid
	} else {
		client, err := net.Dial("unix", listener.Addr().String())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { client.Close() })
	}
	conn, err := listener.Accept()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return conn, pid
}

// kernels runs test with the pidfd from SO_PEERPIDFD, and as on kernels before it (6.5).
func kernels(t *testing.T, test func(t *testing.T, beforePeerPidfd bool)) {
	t.Run("SO_PEERPIDFD", func(t *testing.T) { test(t, false) })
	t.Run("pidfd_open", func(t *testing.T) { test(t, true) })
}

// On a real socket the caller is this process: the kernel's uid and pid, with procfs faked to
// say that it runs the UI. The connection holds the caller's pidfd until it closes.
func TestLinuxClientVerifierOnASocket(t *testing.T) {
	kernels(t, func(t *testing.T, beforePeerPidfd bool) {
		i := install(t)
		uid := uint32(os.Getuid())
		executable, administrator := i.ui, false
		if uid == 0 {
			executable, administrator = i.service, true
		}
		conn, pid := socketPair(t, false)
		i.runs(t, pid, executable, uid, 1)
		c := i.clients()
		c.beforePeerPidfd = beforePeerPidfd
		verified, identity, err := c.verify(conn)
		if err != nil {
			t.Fatal(err)
		}
		want := Identity{UserID: strconv.FormatUint(uint64(uid), 10), SessionID: uid, ProcessID: uint32(pid),
			Administrator: administrator}
		if identity != want {
			t.Fatalf("identity = %+v, want %+v", identity, want)
		}
		held, ok := verified.(*verifiedConn)
		if !ok {
			t.Fatalf("connection = %T", verified)
		}
		if exited(held.pidfd) {
			t.Fatal("the pidfd says this process exited")
		}
		verified.Close()
		if _, err := unix.FcntlInt(uintptr(held.pidfd), unix.F_GETFD, 0); !errors.Is(err, unix.EBADF) {
			t.Fatalf("the pidfd is still open after Close: %v", err)
		}
	})
}

// Before SO_PEERPIDFD the pid the socket reports may have gone to a process started since.
func TestLinuxClientsStartedBeforeTheyConnected(t *testing.T) {
	i := install(t)
	conn, pid := socketPair(t, false)
	i.runs(t, pid, i.ui, uint32(os.Getuid()), 1<<62)
	c := i.clients()
	c.beforePeerPidfd = true
	if _, identity, err := c.verify(conn); err == nil {
		t.Fatalf("accepted as %+v", identity)
	}
}

// A caller that has exited is turned away, even though procfs says its pid runs the UI.
func TestLinuxClientsThatExitedAreTurnedAway(t *testing.T) {
	kernels(t, func(t *testing.T, beforePeerPidfd bool) {
		i := install(t)
		conn, pid := socketPair(t, true)
		i.runs(t, pid, i.ui, uint32(os.Getuid()), 1)
		c := i.clients()
		c.beforePeerPidfd = beforePeerPidfd
		if _, identity, err := c.verify(conn); err == nil {
			t.Fatalf("accepted as %+v", identity)
		}
	})
}

func TestStartTimeReadsPastTheCommandName(t *testing.T) {
	i := install(t)
	i.runs(t, 100, i.ui, 1000, 4242)
	if start, err := i.clients().startTime(100); err != nil || start != 4242 {
		t.Fatalf("start = %d, %v", start, err)
	}
	// This process's real start time is in the unit of bootTicks: it began before now.
	start, err := clients{proc: "/proc"}.startTime(os.Getpid())
	if err != nil {
		t.Fatal(err)
	}
	if now, err := bootTicks(); err != nil || start == 0 || start > now {
		t.Fatalf("started at %d, now %d (%v)", start, now, err)
	}
}
