//go:build linux && tuntest

package engine

// The pinned-TUN test runs owned mode as the Linux service does, with the product's device,
// route table and rule priorities (Options.Pins, D67), on a profile and an override that try
// to move them and to pick whose traffic the TUN takes (§3.3 #7). The TUN must come up as the
// pinned device, route through the pinned table, write rules only in the pinned range and
// leave nothing behind once it stops, so that the service's clean-up finds nothing either.
// Like the owned-TUN test it compiles only with -tags tuntest and runs only with
// OPENWEIR_TUN_TEST=1, as root and outside the host's namespace (tun_linux_test.go).

import (
	"fmt"
	"net"
	"net/url"
	"os"
	"os/exec"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/openweir/app-desktop/src/core/internal/admission"
	"github.com/openweir/app-desktop/src/core/internal/product"
	"github.com/openweir/app-desktop/src/core/internal/tunclean"
)

func TestPinnedTUN(t *testing.T) {
	requireTUNTestAsRoot(t)
	target := os.Getenv("OPENWEIR_TUN_TARGET")
	outbound := os.Getenv("OPENWEIR_TUN_OUTBOUND")
	if target == "" || outbound == "" {
		t.Fatal("OPENWEIR_TUN_TARGET names the HTTP server outside the namespace, OPENWEIR_TUN_OUTBOUND the link to it")
	}
	pins := productPins()
	engine, err := New(Options{HomeDir: testHome, Tun: admission.TunOwned, Pins: pins})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { engine.Close() })

	baseline := networkState(t)
	before := policyRules(t)
	// exclude-uid would leave this process, root, out of the TUN, and include-interface
	// would take only what arrives on lo: were they kept, the request below would go around
	// the TUN. dns-mode disabled keeps sing-tun away from the runner's resolved, as in the
	// owned-TUN test.
	profile := admission.Profile{
		Config: []byte(fmt.Sprintf(`mode: rule
log-level: debug
proxies: []
rules: ['MATCH,DIRECT']
redir-port: 7892
tun:
  stack: %s
  device: mdmoved0
  dns-mode: disabled
  iproute2-table-index: 2022
  iproute2-rule-index: 9000
  include-uid: [0]
  include-interface: [lo]
`, firstStack())),
		Overrides: [][]byte{[]byte(fmt.Sprintf(
			"tun: {device: mdmoved1, iproute2-rule-index: 9100, exclude-uid: [0], exclude-interface: [%s]}\ntproxy-port: 7893\n", outbound))},
		Options: admission.RunOptions{TUN: true},
	}
	if _, err := engine.Start(profile); err != nil {
		t.Fatalf("Start with the TUN: %v", err)
	}
	defer engine.Stop()
	waitForDevice(t, pins.Device, true)
	for _, moved := range []string{"mdmoved0", "mdmoved1"} {
		if _, err := net.InterfaceByName(moved); err == nil {
			t.Fatalf("the profile's device %s came up", moved)
		}
	}

	// Every rule the TUN wrote is in the pinned range and picks nobody; every other rule is
	// as it was.
	during := policyRules(t)
	for family, rules := range during {
		var outside, wrote []string
		for _, rule := range rules {
			if pins.HoldsRule(rulePriority(t, rule)) {
				wrote = append(wrote, rule)
			} else {
				outside = append(outside, rule)
			}
		}
		var outsideBefore []string
		for _, rule := range before[family] {
			if !pins.HoldsRule(rulePriority(t, rule)) {
				outsideBefore = append(outsideBefore, rule)
			}
		}
		if !slices.Equal(outside, outsideBefore) {
			t.Errorf("ip %s rule outside %d-%d:\n%s\nwant:\n%s", family, pins.RuleIndex, pins.RuleIndex+tunclean.RuleSpan,
				strings.Join(outside, "\n"), strings.Join(outsideBefore, "\n"))
		}
		for _, rule := range wrote {
			if strings.Contains(rule, "uidrange") || strings.Contains(rule, "iif "+outbound) ||
				strings.Contains(rule, "lookup 2022") {
				t.Errorf("ip %s rule: %s", family, rule)
			}
		}
		if family == "-4" && !slices.ContainsFunc(wrote, func(rule string) bool {
			return strings.Contains(rule, fmt.Sprintf("lookup %d", pins.Table))
		}) {
			t.Errorf("no IPv4 rule looks up table %d:\n%s", pins.Table, strings.Join(wrote, "\n"))
		}
	}
	if routes := tableRoutes(t, "-4", pins.Table); !strings.Contains(routes, "dev "+pins.Device) {
		t.Errorf("table %d:\n%s", pins.Table, routes)
	}
	for _, family := range []string{"-4", "-6"} {
		if routes := tableRoutes(t, family, 2022); routes != "" {
			t.Errorf("ip %s route show table 2022:\n%s", family, routes)
		}
	}
	for _, port := range []int{7892, 7893} {
		if listening(port) {
			t.Errorf("port %d is open; redir-port and tproxy-port are 0 under pins", port)
		}
	}
	// This process is root, which the override's exclude-uid would have left out of the TUN.
	throughTUN(t, engine, target)

	if err := engine.Stop(); err != nil {
		t.Fatal(err)
	}
	waitForDevice(t, pins.Device, false)
	if state := networkState(t); state != baseline {
		t.Fatalf("routes after Stop:\n%s\nwant:\n%s", state, baseline)
	}
	// A TUN that closed leaves nothing, so the clean-up finds nothing and changes nothing.
	if err := tunclean.Clean(pins); err != nil {
		t.Fatalf("Clean after Stop: %v", err)
	}
	if state := networkState(t); state != baseline {
		t.Fatalf("routes after Clean:\n%s\nwant:\n%s", state, baseline)
	}
}

// requireTUNTestAsRoot skips the test unless the TUN tests were asked for and can run: they
// need root, and refuse the host's namespace.
func requireTUNTestAsRoot(t *testing.T) {
	t.Helper()
	if os.Getenv("OPENWEIR_TUN_TEST") != "1" {
		t.Skip("set OPENWEIR_TUN_TEST=1 and run inside a network namespace")
	}
	if os.Geteuid() != 0 {
		t.Skip("creating a TUN and its rules needs root")
	}
	requireIsolatedNamespace(t)
}

// productPins are the Linux service's own, which a namespace keeps from the host's.
func productPins() tunclean.Pins {
	return tunclean.Pins{Device: product.LinuxTunDevice, Table: product.LinuxRouteTable, RuleIndex: product.LinuxRuleIndex}
}

func firstStack() string {
	stack, _, _ := strings.Cut(os.Getenv("OPENWEIR_TUN_STACKS"), ",")
	if stack == "" {
		return "system"
	}
	return stack
}

func ip(t *testing.T, args ...string) string {
	t.Helper()
	output, err := exec.Command("ip", args...).CombinedOutput()
	if err != nil {
		t.Fatalf("ip %s: %v\n%s", strings.Join(args, " "), err, output)
	}
	return string(output)
}

// tableRoutes are a table's routes as ip prints them, none for a table the kernel does not
// have: ip fails on one of those.
func tableRoutes(t *testing.T, family string, table int) string {
	t.Helper()
	args := []string{family, "route", "show", "table", strconv.Itoa(table)}
	output, err := exec.Command("ip", args...).CombinedOutput()
	if err != nil {
		if strings.Contains(string(output), "FIB table does not exist") {
			return ""
		}
		t.Fatalf("ip %s: %v\n%s", strings.Join(args, " "), err, output)
	}
	return strings.TrimSpace(string(output))
}

// policyRules are the namespace's rules by family, as ip prints them.
func policyRules(t *testing.T) map[string][]string {
	t.Helper()
	rules := map[string][]string{}
	for _, family := range []string{"-4", "-6"} {
		for line := range strings.Lines(ip(t, family, "rule", "show")) {
			if line = strings.TrimSpace(line); line != "" {
				rules[family] = append(rules[family], line)
			}
		}
	}
	return rules
}

func rulePriority(t *testing.T, rule string) int {
	t.Helper()
	field, _, _ := strings.Cut(rule, ":")
	priority, err := strconv.Atoi(field)
	if err != nil {
		t.Fatalf("rule %q: %v", rule, err)
	}
	return priority
}

// reachable says whether a TCP connection to the target's host opens within two seconds.
func reachable(t *testing.T, target string) bool {
	t.Helper()
	address, err := url.Parse(target)
	if err != nil {
		t.Fatal(err)
	}
	conn, err := net.DialTimeout("tcp", address.Host, 2*time.Second)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}
