//go:build linux && tuntest

package engine

// The crash test plays the Linux service dying with its TUN up, and the clean-up systemd runs
// after it and before the next start (-cleanup, tunclean.Clean). A child process, this test
// binary again, runs owned mode with the product's pins and strict-route on a profile without
// IPv6, and the test kills it the way a crash would. The kernel removes the TUN with the
// child's last descriptor, and its routes with it, but sing-tun's rules stay: strict-route
// made IPv6, which the TUN has no address for, unreachable, and so it stays until something
// removes them. The test then freezes a second child instead, a service that hangs with its
// TUN, table and rules in place, which take every packet. In both, Clean must bring the
// namespace back to how it was, connectivity and all, and leave rules and routes beside the
// pins alone. Like the other TUN tests it runs only with OPENWEIR_TUN_TEST=1, as root and
// outside the host's namespace; OPENWEIR_TUN_TARGET6 names an HTTP server the namespace
// reaches over IPv6.

import (
	"bufio"
	"bytes"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/openweir/app-desktop/src/core/internal/admission"
	"github.com/openweir/app-desktop/src/core/internal/tunclean"
)

// crashChildEnv makes TestCrashedTUN the child that runs the TUN until it is killed.
const crashChildEnv = "OPENWEIR_TUN_CRASH_CHILD"

func TestCrashedTUN(t *testing.T) {
	if os.Getenv(crashChildEnv) == "1" {
		runCrashChild(t)
		return
	}
	requireTUNTestAsRoot(t)
	target := os.Getenv("OPENWEIR_TUN_TARGET")
	target6 := os.Getenv("OPENWEIR_TUN_TARGET6")
	if target == "" || target6 == "" {
		t.Fatal("OPENWEIR_TUN_TARGET and OPENWEIR_TUN_TARGET6 name the HTTP server outside the namespace, over IPv4 and IPv6")
	}
	pins := productPins()
	neighbours(t, pins)
	baseline := networkState(t)
	for _, url := range []string{target, target6} {
		if !reachable(t, url) {
			t.Fatalf("%s is out of reach before any TUN", url)
		}
	}
	restored := func(t *testing.T) {
		t.Helper()
		if state := networkState(t); state != baseline {
			t.Fatalf("after Clean:\n%s\nwant:\n%s", state, baseline)
		}
		for _, url := range []string{target, target6} {
			if body := plainGet(t, url); !strings.Contains(body, "through-tun") {
				t.Fatalf("%s: body = %q", url, body)
			}
		}
	}

	t.Run("killed", func(t *testing.T) {
		child := startCrashChild(t)
		child.signal(t, syscall.SIGKILL)
		child.wait()
		waitForDevice(t, pins.Device, false)
		left := policyRules(t)
		var leftRules []string
		for _, family := range []string{"-4", "-6"} {
			held := 0
			for _, rule := range left[family] {
				if pins.HoldsRule(rulePriority(t, rule)) {
					held++
					leftRules = append(leftRules, rule)
				}
			}
			if held == 0 {
				t.Fatalf("the crash left no ip %s rule from %d; the test no longer shows what Clean is for:\n%s",
					family, pins.RuleIndex, strings.Join(left[family], "\n"))
			}
		}
		if !strings.Contains(strings.Join(leftRules, "\n"), "unreachable") {
			t.Fatalf("strict-route left no unreachable rule:\n%s", strings.Join(leftRules, "\n"))
		}
		if reachable(t, target6) {
			t.Fatalf("%s is still in reach after the crash", target6)
		}

		if err := tunclean.Clean(pins); err != nil {
			t.Fatalf("Clean: %v", err)
		}
		restored(t)
		if err := tunclean.Clean(pins); err != nil {
			t.Fatalf("Clean again: %v", err)
		}
		restored(t)
	})

	t.Run("frozen", func(t *testing.T) {
		child := startCrashChild(t)
		child.signal(t, syscall.SIGSTOP)
		child.waitStopped(t)
		if reachable(t, target) {
			t.Fatalf("%s is in reach through a TUN nobody reads", target)
		}
		// The child still holds the TUN, which Clean deletes from under it.
		if err := tunclean.Clean(pins); err != nil {
			t.Fatalf("Clean: %v", err)
		}
		restored(t)
		child.signal(t, syscall.SIGKILL)
		child.wait()
		restored(t)
	})
}

// neighbours are rules just outside the pins and a route in the next table, someone else's
// that Clean must leave; the routes are for documentation addresses, so they catch nothing.
func neighbours(t *testing.T, pins tunclean.Pins) {
	t.Helper()
	table := strconv.Itoa(pins.Table + 1)
	var undo [][]string
	t.Cleanup(func() {
		for i := len(undo) - 1; i >= 0; i-- {
			if output, err := exec.Command("ip", undo[i]...).CombinedOutput(); err != nil {
				t.Errorf("ip %s: %v\n%s", strings.Join(undo[i], " "), err, output)
			}
		}
	})
	for _, family := range []string{"-4", "-6"} {
		for _, priority := range []int{pins.RuleIndex - 1, pins.RuleIndex + tunclean.RuleSpan + 1} {
			rule := []string{family, "rule", "add", "priority", strconv.Itoa(priority), "lookup", table}
			ip(t, rule...)
			undo = append(undo, []string{family, "rule", "del", "priority", strconv.Itoa(priority), "lookup", table})
		}
	}
	for _, route := range [][]string{{"-4", "192.0.2.0/24"}, {"-6", "2001:db8::/32"}} {
		ip(t, route[0], "route", "add", "unreachable", route[1], "table", table)
		undo = append(undo, []string{route[0], "route", "del", "unreachable", route[1], "table", table})
	}
}

type crashChild struct {
	cmd    *exec.Cmd
	output *lockedBuffer
	once   sync.Once
}

// startCrashChild runs TestCrashedTUN again as the child and returns once its TUN and rules
// are up. The child's home is the test's own temporary directory, which outlives the kill.
func startCrashChild(t *testing.T) *crashChild {
	t.Helper()
	ready, readyWriter, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer ready.Close()
	child := &crashChild{output: &lockedBuffer{}}
	child.cmd = exec.Command(os.Args[0], "-test.run=^TestCrashedTUN$", "-test.count=1", "-test.timeout=5m")
	child.cmd.Env = append(os.Environ(), crashChildEnv+"=1", "TMPDIR="+t.TempDir())
	child.cmd.ExtraFiles = []*os.File{readyWriter}
	child.cmd.Stdout, child.cmd.Stderr = child.output, child.output
	// Nothing the child starts should hold its output once it is killed; should anything, Wait
	// gives up on the output rather than hang.
	child.cmd.WaitDelay = 5 * time.Second
	if err := child.cmd.Start(); err != nil {
		t.Fatal(err)
	}
	readyWriter.Close()
	t.Cleanup(func() {
		_ = child.cmd.Process.Kill()
		child.wait()
		if t.Failed() {
			t.Logf("child:\n%s", child.output.String())
		}
	})
	lines := make(chan string, 1)
	go func() {
		line, _ := bufio.NewReader(ready).ReadString('\n')
		lines <- strings.TrimSpace(line)
	}()
	select {
	case line := <-lines:
		if line != "ready" {
			t.Fatalf("the child did not bring its TUN up (%q):\n%s", line, child.output.String())
		}
	case <-time.After(time.Minute):
		t.Fatalf("the child did not bring its TUN up in a minute:\n%s", child.output.String())
	}
	return child
}

func (c *crashChild) signal(t *testing.T, signal syscall.Signal) {
	t.Helper()
	if err := c.cmd.Process.Signal(signal); err != nil {
		t.Fatal(err)
	}
}

// waitStopped returns once every thread of the child has stopped, so that none of them still
// reads the TUN when the test sends into it.
func (c *crashChild) waitStopped(t *testing.T) {
	t.Helper()
	tasks := fmt.Sprintf("/proc/%d/task", c.cmd.Process.Pid)
	for deadline := time.Now().Add(10 * time.Second); ; {
		entries, err := os.ReadDir(tasks)
		if err != nil {
			t.Fatal(err)
		}
		stopped := 0
		for _, entry := range entries {
			// The state follows the command name, which is in parentheses and may hold any byte.
			stat, err := os.ReadFile(filepath.Join(tasks, entry.Name(), "stat"))
			if end := bytes.LastIndexByte(stat, ')'); err == nil && end >= 0 && end+2 < len(stat) &&
				(stat[end+2] == 'T' || stat[end+2] == 't') {
				stopped++
			}
		}
		if stopped == len(entries) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("%d of the child's %d threads stopped", stopped, len(entries))
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// wait reaps the child once, however many times it is asked.
func (c *crashChild) wait() {
	c.once.Do(func() { _ = c.cmd.Wait() })
}

// runCrashChild brings the pinned TUN up as the service would, says so on descriptor 3 and
// waits to be killed. ipv6: false leaves the TUN without an IPv6 address, which is what makes
// strict-route write the unreachable rule a crash leaves behind.
func runCrashChild(t *testing.T) {
	pins := productPins()
	engine, err := New(Options{HomeDir: testHome, Tun: admission.TunOwned, Pins: pins})
	if err != nil {
		t.Fatal(err)
	}
	profile := admission.Profile{Config: []byte(fmt.Sprintf(`mode: rule
log-level: debug
ipv6: false
proxies: []
rules: ['MATCH,DIRECT']
tun:
  stack: %s
  dns-mode: disabled
`, firstStack())), Options: admission.RunOptions{TUN: true}}
	if _, err := engine.Start(profile); err != nil {
		t.Fatalf("Start with the TUN: %v", err)
	}
	waitForDevice(t, pins.Device, true)
	for deadline := time.Now().Add(10 * time.Second); ; {
		rules := policyRules(t)
		if heldRule(t, pins, rules["-4"]) && heldRule(t, pins, rules["-6"]) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("no rules from %d:\n%v", pins.RuleIndex, rules)
		}
		time.Sleep(50 * time.Millisecond)
	}
	ready := os.NewFile(3, "ready")
	if _, err := fmt.Fprintln(ready, "ready"); err != nil {
		t.Fatal(err)
	}
	time.Sleep(4 * time.Minute)
	t.Fatal("the parent never killed the child")
}

func heldRule(t *testing.T, pins tunclean.Pins, rules []string) bool {
	for _, rule := range rules {
		if pins.HoldsRule(rulePriority(t, rule)) {
			return true
		}
	}
	return false
}

// lockedBuffer collects the child's output, which its pipes write while the test reads.
type lockedBuffer struct {
	mu     sync.Mutex
	buffer bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buffer.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buffer.String()
}
