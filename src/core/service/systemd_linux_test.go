package main

import (
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// shortDir is a directory for sockets, whose paths must fit in 108 bytes.
func shortDir(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "service-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	return dir
}

func TestSystemdPassesOneSocketToThisProcess(t *testing.T) {
	pid := os.Getpid()
	this := fmt.Sprint(pid)
	if err := passedSockets(this, "1", pid); err != nil {
		t.Fatal(err)
	}
	if err := passedSockets("", "", pid); !errors.Is(err, errNotActivated) {
		t.Fatalf("no variables: %v", err)
	}
	for _, tc := range [][2]string{
		{fmt.Sprint(pid + 1), "1"}, // passed to the parent, say, before it started this one
		{"", "1"},
		{this, "0"},
		{this, "2"},
		{this, ""},
		{this, " 1"},
	} {
		if err := passedSockets(tc[0], tc[1], pid); err == nil {
			t.Errorf("LISTEN_PID=%q LISTEN_FDS=%q accepted", tc[0], tc[1])
		}
	}
}

// duplicate returns a descriptor of file's own, for listenerFrom to take over.
func duplicate(t *testing.T, file *os.File) int {
	t.Helper()
	defer file.Close()
	fd, err := unix.Dup(int(file.Fd()))
	if err != nil {
		t.Fatal(err)
	}
	return fd
}

func TestTheSocketFromSystemdIsServed(t *testing.T) {
	socket := filepath.Join(shortDir(t), "control.sock")
	passed, err := net.ListenUnix("unix", &net.UnixAddr{Name: socket, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	file, err := passed.File()
	if err != nil {
		t.Fatal(err)
	}
	fd := duplicate(t, file)
	// systemd keeps the socket; only this process's listener goes.
	passed.SetUnlinkOnClose(false)
	passed.Close()
	listener, err := listenerFrom(fd)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	if flags, err := unix.FcntlInt(uintptr(fd), unix.F_GETFD, 0); !errors.Is(err, unix.EBADF) {
		t.Fatalf("the passed descriptor stays open (flags %d, %v)", flags, err)
	}
	client, err := net.Dial("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	conn, err := listener.Accept()
	if err != nil {
		t.Fatal(err)
	}
	conn.Close()
}

func TestOnlyAUnixSocketFromSystemdIsServed(t *testing.T) {
	tcp, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer tcp.Close()
	file, err := tcp.(*net.TCPListener).File()
	if err != nil {
		t.Fatal(err)
	}
	if listener, err := listenerFrom(duplicate(t, file)); err == nil {
		listener.Close()
		t.Fatal("served a TCP socket")
	}
	null, err := os.Open(os.DevNull)
	if err != nil {
		t.Fatal(err)
	}
	if listener, err := listenerFrom(duplicate(t, null)); err == nil {
		listener.Close()
		t.Fatal("served a file")
	}
}

// In the foreground the service makes its socket as the socket unit does, for every user.
func TestTheForegroundListensAsTheSocketUnit(t *testing.T) {
	dir := shortDir(t)
	socket := filepath.Join(dir, "openweir", "control.sock")
	listener, err := listenItself(socket, filepath.Join(dir, "no-sockets"))
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	for path, want := range map[string]os.FileMode{filepath.Dir(socket): os.ModeDir | 0o755, socket: os.ModeSocket | 0o666} {
		if info, err := os.Lstat(path); err != nil || info.Mode() != want {
			t.Errorf("%s: %v, %v; want %v", path, info.Mode(), err, want)
		}
	}
	client, err := net.Dial("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	client.Close()
}

func TestTheForegroundReplacesOnlyAStaleSocket(t *testing.T) {
	dir := shortDir(t)
	sockets := filepath.Join(dir, "unix")
	socket := filepath.Join(dir, "control.sock")
	stale, err := net.ListenUnix("unix", &net.UnixAddr{Name: socket, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	stale.SetUnlinkOnClose(false)
	stale.Close()
	listener, err := listenItself(socket, sockets)
	if err != nil {
		t.Fatalf("a stale socket: %v", err)
	}
	listener.Close()

	// The socket unit listening there: removing its socket would leave it unreachable.
	listing := "Num       RefCount Protocol Flags    Type St Inode Path\n" +
		"0000000000000000: 00000002 00000000 00010000 0001 01 4242 " + socket + "\n"
	if err := os.WriteFile(sockets, []byte(listing), 0o644); err != nil {
		t.Fatal(err)
	}
	if listener, err := listenItself(socket, sockets); err == nil {
		listener.Close()
		t.Fatal("took over a socket in use")
	}

	file := filepath.Join(dir, "file")
	if err := os.WriteFile(file, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if listener, err := listenItself(file, filepath.Join(dir, "no-sockets")); err == nil {
		listener.Close()
		t.Fatal("replaced a file")
	}
}

// listenedOn reads the kernel's own list: this process listening on a socket shows there.
func TestListenedOnReadsProcNetUnix(t *testing.T) {
	socket := filepath.Join(shortDir(t), "control.sock")
	if listening, err := listenedOn(procNetUnix, socket); err != nil || listening {
		t.Fatalf("before listening: %v, %v", listening, err)
	}
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	if listening, err := listenedOn(procNetUnix, socket); err != nil || !listening {
		t.Fatalf("while listening: %v, %v", listening, err)
	}
	// A connected socket bound to the path, without __SO_ACCEPTCON, is not listening.
	sockets := filepath.Join(shortDir(t), "unix")
	listing := "Num RefCount Protocol Flags Type St Inode Path\n0: 00000003 00000000 00000000 0001 03 4242 " + socket + "\n"
	if err := os.WriteFile(sockets, []byte(listing), 0o644); err != nil {
		t.Fatal(err)
	}
	if listening, err := listenedOn(sockets, socket); err != nil || listening {
		t.Fatalf("a connected socket: %v, %v", listening, err)
	}
}

// READY=1 reaches systemd's socket, by path or in the abstract namespace.
func TestNotifyTellsSystemd(t *testing.T) {
	for _, address := range []string{
		filepath.Join(shortDir(t), "notify"),
		fmt.Sprintf("@openweir-test-notify-%d-%d", os.Getpid(), time.Now().UnixNano()),
	} {
		systemd, err := net.ListenUnixgram("unixgram", &net.UnixAddr{Name: address, Net: "unixgram"})
		if err != nil {
			t.Fatal(err)
		}
		if err := notify(address, "READY=1"); err != nil {
			t.Fatalf("%s: %v", address, err)
		}
		systemd.SetReadDeadline(time.Now().Add(5 * time.Second))
		buffer := make([]byte, 64)
		n, err := systemd.Read(buffer)
		systemd.Close()
		if err != nil || string(buffer[:n]) != "READY=1" {
			t.Fatalf("%s: got %q, %v", address, buffer[:n], err)
		}
	}
	if err := notify("", "READY=1"); err != nil {
		t.Fatalf("no NOTIFY_SOCKET: %v", err)
	}
	if err := notify("vsock:2:1234", "READY=1"); err == nil || !strings.Contains(err.Error(), "vsock") {
		t.Fatalf("a vsock address: %v", err)
	}
}
