//go:build darwin || linux

package peer

import (
	"net"
	"os"
	"path/filepath"
	"strconv"
	"testing"
)

// The development socket takes the user running the server, by the uid the kernel reports
// for the other end, and turns anyone else away.
func TestUnixSocketVerifier(t *testing.T) {
	// A short path: macOS limits a socket's to 104 bytes, which t.TempDir() may overrun.
	dir, err := os.MkdirTemp("", "peer-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	listener, err := net.Listen("unix", filepath.Join(dir, "s"))
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	accept := func() net.Conn {
		t.Helper()
		client, err := net.Dial("unix", listener.Addr().String())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { client.Close() })
		conn, err := listener.Accept()
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { conn.Close() })
		return conn
	}
	uid := uint32(os.Getuid())
	_, identity, err := UnixSocketVerifier(uid)(accept())
	if err != nil || identity != (Identity{UserID: strconv.FormatUint(uint64(uid), 10)}) {
		t.Fatalf("identity = %+v, %v", identity, err)
	}
	if _, _, err := UnixSocketVerifier(uid + 1)(accept()); err == nil {
		t.Fatal("accepted another user")
	}
}
