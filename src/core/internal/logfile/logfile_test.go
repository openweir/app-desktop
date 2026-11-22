package logfile

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func read(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// The log moves aside while the program runs, each time it would outgrow its limit, so at
// most two limits' worth stays on disk however much is written.
func TestLogMovesAsideWhileRunning(t *testing.T) {
	path := filepath.Join(t.TempDir(), "service.log")
	log, err := Open(path, 100)
	if err != nil {
		t.Fatal(err)
	}
	defer log.Close()
	line := strings.Repeat("x", 29) + "\n" // 30 bytes: three fit, the fourth moves the log
	for i := range 1000 {
		if _, err := log.Write([]byte(line)); err != nil {
			t.Fatalf("write %d: %v", i, err)
		}
	}
	current, previous := read(t, path), read(t, path+".1")
	if len(current) == 0 || len(current) > 100 || len(previous) != 90 {
		t.Fatalf("log %d bytes, previous %d", len(current), len(previous))
	}
	// 1000 lines of three per log: the current one has the last line, a full log before it.
	if current != strings.Repeat(line, 1) || previous != strings.Repeat(line, 3) {
		t.Fatalf("log %q, previous %q", current, previous)
	}
}

// A log left over past its limit moves aside at the first line, as it did at start.
func TestLogPastItsLimitMovesAsideFirst(t *testing.T) {
	path := filepath.Join(t.TempDir(), "service.log")
	if err := os.WriteFile(path, []byte(strings.Repeat("old\n", 50)), 0o600); err != nil {
		t.Fatal(err)
	}
	log, err := Open(path, 100)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := log.Write([]byte("new\n")); err != nil {
		t.Fatal(err)
	}
	if err := log.Close(); err != nil {
		t.Fatal(err)
	}
	if read(t, path) != "new\n" || read(t, path+".1") != strings.Repeat("old\n", 50) {
		t.Fatalf("log %q", read(t, path))
	}
	if _, err := log.Write([]byte("late\n")); !errors.Is(err, os.ErrClosed) {
		t.Fatalf("write after close: %v", err)
	}
}

// A line longer than the limit goes to a log of its own rather than nowhere.
func TestLongLineIsKept(t *testing.T) {
	path := filepath.Join(t.TempDir(), "service.log")
	log, err := Open(path, 10)
	if err != nil {
		t.Fatal(err)
	}
	defer log.Close()
	for _, line := range []string{"short\n", strings.Repeat("y", 20) + "\n"} {
		if _, err := log.Write([]byte(line)); err != nil {
			t.Fatal(err)
		}
	}
	if read(t, path) != strings.Repeat("y", 20)+"\n" || read(t, path+".1") != "short\n" {
		t.Fatalf("log %q, previous %q", read(t, path), read(t, path+".1"))
	}
}
