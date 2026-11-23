package peer

import (
	"bytes"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"

	"golang.org/x/sys/unix"
)

// userHZ is the unit of a process's start time in /proc/<pid>/stat: USER_HZ, which the kernel
// fixes at 100 on every architecture the service is built for.
const userHZ = 100

// LinuxClientVerifier backs the Linux service's socket, which every local user may connect to
// (docs/architecture.md "Linux (in progress)", D60): the check here is the access control. It
// accepts uiPath, the installed UI, run by any user but root, and returns that user's uid as
// UserID and as SessionID (the service follows logind's users), with the caller's process. It
// also accepts servicePath, the service's own executable, run as root: the take-over helper
// that the UI starts through pkexec once an administrator confirmed it (D66), the one caller
// with Administrator. Root never connects as the UI, so it never claims the tunnel as one.
//
// A caller is the executable it runs. /proc/<pid>/exe must name uiPath or servicePath and be
// the very file there now, and that file and every directory above it must be root's and
// writable by no one else: then no other user put it there, nor can swap it for their own.
// The process comes from the kernel as a pidfd (SO_PEERPIDFD, Linux 6.5); before 6.5 the pid
// the socket reports is opened with pidfd_open, and must not belong to a process that started
// after the check began. Either way the process must still run once checked, or its pid could
// be another's by then; the connection holds the pidfd until it closes, as the Windows one
// holds the client's process.
func LinuxClientVerifier(uiPath, servicePath string) Verifier {
	return clients{proc: "/proc", owner: 0, top: "/", ui: filepath.Clean(uiPath), service: filepath.Clean(servicePath)}.verify
}

// clients is LinuxClientVerifier's check. Tests point it at fake process data and files of
// their own.
type clients struct {
	// proc is where procfs is mounted.
	proc string
	// owner must own the executables and the directories above them: root.
	owner uint32
	// top is the highest directory checked, "/".
	top         string
	ui, service string
	// beforePeerPidfd takes the way for kernels without SO_PEERPIDFD.
	beforePeerPidfd bool
}

func (c clients) verify(conn net.Conn) (net.Conn, Identity, error) {
	unixConn, ok := conn.(*net.UnixConn)
	if !ok {
		return nil, Identity{}, errors.New("control connection is not a Unix socket")
	}
	raw, err := unixConn.SyscallConn()
	if err != nil {
		return nil, Identity{}, err
	}
	var credentials *unix.Ucred
	var credentialsErr error
	pidfd, pidfdErr := -1, error(unix.ENOPROTOOPT)
	if err := raw.Control(func(fd uintptr) {
		credentials, credentialsErr = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
		if credentialsErr == nil && !c.beforePeerPidfd {
			pidfd, pidfdErr = unix.GetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_PEERPIDFD)
		}
	}); err != nil {
		return nil, Identity{}, err
	}
	if credentialsErr != nil {
		return nil, Identity{}, fmt.Errorf("read socket peer credentials: %w", credentialsErr)
	}
	// 0 is a process in a PID namespace the service cannot see into.
	if credentials.Pid <= 0 {
		return nil, Identity{}, errors.New("socket peer runs where the service cannot see its process")
	}
	pid := int(credentials.Pid)
	if errors.Is(pidfdErr, unix.ENOPROTOOPT) {
		pidfd, pidfdErr = c.openBeforePeerPidfd(pid)
	}
	if pidfdErr != nil {
		return nil, Identity{}, fmt.Errorf("open socket peer process %d: %w", pid, pidfdErr)
	}
	identity, err := c.identify(credentials.Uid, pid)
	if err == nil && exited(pidfd) {
		err = fmt.Errorf("socket peer process %d exited while it was checked", pid)
	}
	if err != nil {
		unix.Close(pidfd)
		return nil, Identity{}, err
	}
	return &verifiedConn{Conn: conn, pidfd: pidfd}, identity, nil
}

// openBeforePeerPidfd opens the process behind the pid the socket reports, on a kernel that
// cannot hand over its pidfd. That process connected, unless it has exited and its pid gone
// to another since: one that started after the check began cannot have connected.
func (c clients) openBeforePeerPidfd(pid int) (int, error) {
	began, err := bootTicks()
	if err != nil {
		return -1, err
	}
	pidfd, err := unix.PidfdOpen(pid, 0)
	if err != nil {
		return -1, err
	}
	started, err := c.startTime(pid)
	if err == nil && started > began {
		err = errors.New("the process started after the connection")
	}
	if err != nil {
		unix.Close(pidfd)
		return -1, err
	}
	return pidfd, nil
}

// process is what procfs says of a caller.
type process struct {
	pid int
	// executable is where the file the process runs is, by name; image is that file.
	executable string
	image      unix.Stat_t
	// uid is the effective uid it runs as now.
	uid uint32
}

// identify reads the caller's process from procfs and decides on it.
func (c clients) identify(uid uint32, pid int) (Identity, error) {
	caller, err := c.read(pid)
	if err != nil {
		return Identity{}, err
	}
	return c.decide(uid, caller)
}

func (c clients) read(pid int) (process, error) {
	dir := filepath.Join(c.proc, strconv.Itoa(pid))
	exe := filepath.Join(dir, "exe")
	caller := process{pid: pid}
	var err error
	if caller.executable, err = os.Readlink(exe); err != nil {
		return process{}, fmt.Errorf("read socket peer executable: %w", err)
	}
	if err := unix.Stat(exe, &caller.image); err != nil {
		return process{}, fmt.Errorf("read socket peer executable: %w", err)
	}
	if caller.uid, err = effectiveUID(filepath.Join(dir, "status")); err != nil {
		return process{}, fmt.Errorf("read socket peer user: %w", err)
	}
	return caller, nil
}

// decide accepts caller, which connected as uid, when it runs the installed UI as a user or the
// service's executable as root.
func (c clients) decide(uid uint32, caller process) (Identity, error) {
	helper := false
	switch caller.executable {
	case c.ui:
	case c.service:
		helper = true
	default:
		return Identity{}, fmt.Errorf("socket peer %s is not the installed UI", caller.executable)
	}
	if err := c.installed(caller.executable, caller.image); err != nil {
		return Identity{}, err
	}
	switch {
	case caller.uid != uid:
		return Identity{}, fmt.Errorf("socket peer connected as uid %d and runs as %d now", uid, caller.uid)
	case helper && uid != 0:
		return Identity{}, errors.New("socket peer runs the service's executable, which connects only as root to take over")
	case !helper && uid == 0:
		return Identity{}, errors.New("socket peer runs the UI as root")
	}
	return Identity{
		UserID:        strconv.FormatUint(uint64(uid), 10),
		SessionID:     uid,
		ProcessID:     uint32(caller.pid),
		Administrator: helper,
	}, nil
}

// installed checks that image, the file a caller runs, is the regular file at path now, and
// that it and every directory above it are the owner's and writable by no one else.
func (c clients) installed(path string, image unix.Stat_t) error {
	var file unix.Stat_t
	if err := unix.Lstat(path, &file); err != nil {
		return fmt.Errorf("socket peer executable: %w", err)
	}
	// By name alone the file could be another one mounted over path in the caller's own mount
	// namespace, which unprivileged users may make.
	if file.Mode&unix.S_IFMT != unix.S_IFREG || file.Dev != image.Dev || file.Ino != image.Ino {
		return fmt.Errorf("socket peer does not run the file at %s", path)
	}
	if err := c.ownerOnly(path, file); err != nil {
		return err
	}
	for dir := filepath.Dir(path); ; dir = filepath.Dir(dir) {
		var info unix.Stat_t
		if err := unix.Lstat(dir, &info); err != nil {
			return fmt.Errorf("socket peer executable: %w", err)
		}
		if info.Mode&unix.S_IFMT != unix.S_IFDIR {
			return fmt.Errorf("%s, above the socket peer's executable, is not a directory", dir)
		}
		if err := c.ownerOnly(dir, info); err != nil {
			return err
		}
		if dir == c.top || dir == filepath.Dir(dir) {
			return nil
		}
	}
}

// ownerOnly refuses what someone but the owner may write. The group's bits count too: they
// are the mask of a POSIX ACL, without which no named user or group gets to write.
func (c clients) ownerOnly(path string, info unix.Stat_t) error {
	if info.Uid != c.owner || info.Mode&(unix.S_IWGRP|unix.S_IWOTH) != 0 {
		return fmt.Errorf("others than uid %d may change %s", c.owner, path)
	}
	return nil
}

// effectiveUID reads the effective uid, the second on the Uid line, from /proc/<pid>/status.
func effectiveUID(status string) (uint32, error) {
	content, err := os.ReadFile(status)
	if err != nil {
		return 0, err
	}
	for line := range strings.SplitSeq(string(content), "\n") {
		if ids, ok := strings.CutPrefix(line, "Uid:"); ok {
			fields := strings.Fields(ids)
			if len(fields) < 2 {
				break
			}
			uid, err := strconv.ParseUint(fields[1], 10, 32)
			return uint32(uid), err
		}
	}
	return 0, fmt.Errorf("%s has no Uid line", status)
}

// startTime reads when pid started, in ticks since boot, from /proc/<pid>/stat.
func (c clients) startTime(pid int) (uint64, error) {
	stat, err := os.ReadFile(filepath.Join(c.proc, strconv.Itoa(pid), "stat"))
	if err != nil {
		return 0, err
	}
	// The command name, in parentheses, may itself hold spaces and parentheses.
	end := bytes.LastIndexByte(stat, ')')
	if end < 0 {
		return 0, fmt.Errorf("unreadable stat for process %d", pid)
	}
	// starttime is field 22; the fields after the name begin with field 3.
	const startTime = 22 - 3
	fields := strings.Fields(string(stat[end+1:]))
	if len(fields) <= startTime {
		return 0, fmt.Errorf("unreadable stat for process %d", pid)
	}
	return strconv.ParseUint(fields[startTime], 10, 64)
}

// bootTicks is the time since boot in the unit of a process's start time.
func bootTicks() (uint64, error) {
	var now unix.Timespec
	if err := unix.ClockGettime(unix.CLOCK_BOOTTIME, &now); err != nil {
		return 0, err
	}
	return uint64(now.Nano()) / (1e9 / userHZ), nil
}

// exited says whether the process behind pidfd has exited, as a pidfd turns readable then.
func exited(pidfd int) bool {
	for {
		ready, err := unix.Poll([]unix.PollFd{{Fd: int32(pidfd), Events: unix.POLLIN}}, 0)
		if errors.Is(err, unix.EINTR) {
			continue
		}
		return err != nil || ready != 0
	}
}

// verifiedConn lets go of the caller's pidfd when it closes.
type verifiedConn struct {
	net.Conn
	once  sync.Once
	pidfd int
}

func (c *verifiedConn) Close() error {
	err := c.Conn.Close()
	c.once.Do(func() { unix.Close(c.pidfd) })
	return err
}
