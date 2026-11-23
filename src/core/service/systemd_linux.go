package main

import (
	"errors"
	"fmt"
	"io/fs"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/openweir/app-desktop/src/core/internal/product"
)

// What the service needs of systemd is small enough to write here rather than add a
// dependency: the listening socket of <LinuxUnit>.socket, passed as in sd_listen_fds(3), and
// READY=1 once it serves, sent as in sd_notify(3).

// listenFDsStart is SD_LISTEN_FDS_START, the first descriptor systemd passes.
const listenFDsStart = 3

// procNetUnix lists the Unix sockets in the service's network namespace.
const procNetUnix = "/proc/net/unix"

var errNotActivated = fmt.Errorf("systemd passed no socket: run %s.service, or -system -foreground from a root terminal",
	product.LinuxUnit)

// systemdListener takes the socket systemd passed. The variables that pass it go, so that no
// child process takes the socket for its own; the descriptor itself is closed on exec.
func systemdListener() (net.Listener, error) {
	pid, fds := os.Getenv("LISTEN_PID"), os.Getenv("LISTEN_FDS")
	for _, name := range []string{"LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"} {
		os.Unsetenv(name)
	}
	if err := passedSockets(pid, fds, os.Getpid()); err != nil {
		return nil, err
	}
	return listenerFrom(listenFDsStart)
}

// passedSockets checks that systemd passed process pid one socket, which <LinuxUnit>.socket has.
func passedSockets(listenPID, listenFDs string, pid int) error {
	switch {
	case listenPID == "" && listenFDs == "":
		return errNotActivated
	case listenPID != strconv.Itoa(pid):
		return fmt.Errorf("LISTEN_PID is %q, not this process (%d)", listenPID, pid)
	case listenFDs != "1":
		return fmt.Errorf("LISTEN_FDS is %q: %s.socket passes one socket", listenFDs, product.LinuxUnit)
	}
	return nil
}

// listenerFrom serves the listening Unix socket at descriptor fd, which it takes over.
func listenerFrom(fd int) (net.Listener, error) {
	file := os.NewFile(uintptr(fd), "socket from systemd")
	// The listener has a duplicate, closed on exec.
	defer file.Close()
	listener, err := net.FileListener(file)
	if err != nil {
		return nil, fmt.Errorf("the socket systemd passed: %w", err)
	}
	if _, ok := listener.(*net.UnixListener); !ok {
		listener.Close()
		return nil, fmt.Errorf("the socket systemd passed is %s, not a Unix socket", listener.Addr().Network())
	}
	return listener, nil
}

// listenItself listens on path as <LinuxUnit>.socket does, for -foreground: its directory is
// 0755 and the socket 0666, since every user's UI connects and the peer check decides whom to
// serve. It refuses a socket that something listens on, the socket unit's in particular:
// removing it would leave the unit listening where no one can connect.
func listenItself(path, sockets string) (net.Listener, error) {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	if info, err := os.Lstat(dir); err != nil {
		return nil, err
	} else if !info.IsDir() {
		return nil, fmt.Errorf("%s is not a directory", dir)
	}
	if err := os.Chmod(dir, 0o755); err != nil {
		return nil, err
	}
	switch listening, err := listenedOn(sockets, path); {
	case err != nil:
		return nil, err
	case listening:
		return nil, fmt.Errorf("something listens on %s: stop %s.socket and %s.service first", path,
			product.LinuxUnit, product.LinuxUnit)
	}
	if err := removeStaleSocket(path); err != nil {
		return nil, err
	}
	listener, err := net.Listen("unix", path)
	if err != nil {
		return nil, err
	}
	if err := os.Chmod(path, 0o666); err != nil {
		listener.Close()
		return nil, err
	}
	return listener, nil
}

// listenedOn says whether sockets, /proc/net/unix, lists a listening socket bound to path.
func listenedOn(sockets, path string) (bool, error) {
	content, err := os.ReadFile(sockets)
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	// Num RefCount Protocol Flags Type St Inode Path, where Flags has __SO_ACCEPTCON for a
	// listening socket.
	const acceptConnections = 0x10000
	for line := range strings.SplitSeq(string(content), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 8 || strings.Join(fields[7:], " ") != path {
			continue
		}
		if flags, err := strconv.ParseUint(fields[3], 16, 32); err == nil && flags&acceptConnections != 0 {
			return true, nil
		}
	}
	return false, nil
}

// notify sends state to systemd on socket, the NOTIFY_SOCKET it gave a Type=notify service;
// without one, as in the foreground, there is no one to tell. An address that starts with @
// is in the abstract namespace, which net reads the same way.
func notify(socket, state string) error {
	if socket == "" {
		return nil
	}
	if !strings.HasPrefix(socket, "/") && !strings.HasPrefix(socket, "@") {
		return fmt.Errorf("NOTIFY_SOCKET %q is not a Unix socket", socket)
	}
	conn, err := net.DialUnix("unixgram", nil, &net.UnixAddr{Name: socket, Net: "unixgram"})
	if err != nil {
		return err
	}
	defer conn.Close()
	_, err = conn.Write([]byte(state))
	return err
}
