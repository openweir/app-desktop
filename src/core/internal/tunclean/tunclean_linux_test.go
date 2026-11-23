package tunclean

import (
	"errors"
	"fmt"
	"net"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/sagernet/netlink"
	"golang.org/x/sys/unix"
)

// These tests run Clean's steps on fakeLinks, and Clean itself only where it finds nothing, so
// they need no privileges and change nothing; the TUN tests in internal/engine run it on a
// namespace's own rules and devices (tun_crash_linux_test.go).

var testPins = Pins{Device: "openweir0", Table: 9347, RuleIndex: 9347}

// fakeLinks keeps rules, routes and devices the way the kernel answers for them: a rule is
// deleted by family and priority, a route needs a destination, and what is not there is
// ENOENT, ESRCH or LinkNotFoundError.
type fakeLinks struct {
	rules  map[int][]netlink.Rule
	routes []netlink.Route
	links  map[string]netlink.Link

	listRules, deleteRule, listRoutes, deleteRoute, lookUp, deleteLink error

	// calls records the deletions, and the revert when the test hands one to the cleaner.
	calls []string
}

func (f *fakeLinks) RuleList(family int) ([]netlink.Rule, error) {
	if f.listRules != nil && family == netlink.FAMILY_V4 {
		return nil, f.listRules
	}
	return slices.Clone(f.rules[family]), nil
}

func (f *fakeLinks) RuleDel(rule *netlink.Rule) error {
	f.calls = append(f.calls, fmt.Sprintf("delete %s rule %d", familyName(rule.Family), rule.Priority))
	if f.deleteRule != nil {
		return f.deleteRule
	}
	rules := f.rules[rule.Family]
	for i, existing := range rules {
		if existing.Priority == rule.Priority {
			f.rules[rule.Family] = slices.Delete(rules, i, i+1)
			return nil
		}
	}
	return unix.ENOENT
}

func (f *fakeLinks) RouteListFiltered(family int, filter *netlink.Route, mask uint64) ([]netlink.Route, error) {
	if f.listRoutes != nil {
		return nil, f.listRoutes
	}
	var routes []netlink.Route
	for _, route := range f.routes {
		if route.Family == family && (mask&netlink.RT_FILTER_TABLE == 0 || route.Table == filter.Table) {
			routes = append(routes, route)
		}
	}
	return routes, nil
}

func (f *fakeLinks) RouteDel(route *netlink.Route) error {
	if route.Dst == nil {
		return errors.New("Either Dst.IP, Src.IP or Gw must be set")
	}
	f.calls = append(f.calls, fmt.Sprintf("delete route to %s in table %d", route.Dst, route.Table))
	if f.deleteRoute != nil {
		return f.deleteRoute
	}
	for i, existing := range f.routes {
		if existing.Table == route.Table && existing.Family == route.Family && destination(existing) == route.Dst.String() {
			f.routes = slices.Delete(f.routes, i, i+1)
			return nil
		}
	}
	return unix.ESRCH
}

func (f *fakeLinks) LinkByName(name string) (netlink.Link, error) {
	if f.lookUp != nil {
		return nil, f.lookUp
	}
	if link, ok := f.links[name]; ok {
		return link, nil
	}
	return nil, netlink.LinkNotFoundError{}
}

func (f *fakeLinks) LinkDel(link netlink.Link) error {
	f.calls = append(f.calls, "delete device "+link.Attrs().Name)
	if f.deleteLink != nil {
		return f.deleteLink
	}
	if _, ok := f.links[link.Attrs().Name]; !ok {
		return unix.ENODEV
	}
	delete(f.links, link.Attrs().Name)
	return nil
}

// destination is how the kernel lists a route's destination: none for a default route.
func destination(route netlink.Route) string {
	if route.Dst == nil {
		if route.Family == netlink.FAMILY_V6 {
			return "::/0"
		}
		return "0.0.0.0/0"
	}
	return route.Dst.String()
}

func rule(family, priority int) netlink.Rule {
	r := netlink.NewRule()
	r.Family, r.Priority = family, priority
	return *r
}

func route(family, table int, dst string) netlink.Route {
	r := netlink.Route{Family: family, Table: table, LinkIndex: 7}
	if dst != "" {
		_, r.Dst, _ = net.ParseCIDR(dst)
	}
	return r
}

func tun(name string) netlink.Link {
	return &netlink.Tuntap{LinkAttrs: netlink.LinkAttrs{Name: name, Index: 7}, Mode: netlink.TUNTAP_MODE_TUN}
}

// leftovers is what a crashed TUN with strict-route and no IPv6 address leaves at the pins,
// among the kernel's rules and someone else's just outside them. Its device is gone with the
// process; the tests that need it add it.
func leftovers() *fakeLinks {
	v4, v6 := netlink.FAMILY_V4, netlink.FAMILY_V6
	return &fakeLinks{
		rules: map[int][]netlink.Rule{
			v4: {rule(v4, 0), rule(v4, 9346), rule(v4, 9347), rule(v4, 9348), rule(v4, 9349), rule(v4, 9349),
				rule(v4, 9349), rule(v4, 9357), rule(v4, 9358), rule(v4, 32766), rule(v4, 32767)},
			// sing-tun's unreachable rule for the family the TUN has no address for.
			v6: {rule(v6, 0), rule(v6, 9347), rule(v6, 9357), rule(v6, 32766)},
		},
		routes: []netlink.Route{
			route(v4, 9347, ""), route(v4, 9347, "198.18.0.0/30"), route(v6, 9347, ""),
			route(v4, 9346, ""), route(v4, 254, "10.0.0.0/8"), route(v6, 2022, ""),
		},
		links: map[string]netlink.Link{},
	}
}

func priorities(rules []netlink.Rule) []int {
	var out []int
	for _, r := range rules {
		out = append(out, r.Priority)
	}
	return out
}

func TestCleanRemovesTheTUNsRulesAndNoOneElses(t *testing.T) {
	links := leftovers()
	if err := (cleaner{links: links}).clean(testPins); err != nil {
		t.Fatal(err)
	}
	if got, want := priorities(links.rules[netlink.FAMILY_V4]), []int{0, 9346, 9358, 32766, 32767}; !slices.Equal(got, want) {
		t.Errorf("IPv4 rules %v, want %v", got, want)
	}
	if got, want := priorities(links.rules[netlink.FAMILY_V6]), []int{0, 32766}; !slices.Equal(got, want) {
		t.Errorf("IPv6 rules %v, want %v", got, want)
	}
}

func TestCleanFlushesThePinnedTableOnly(t *testing.T) {
	links := leftovers()
	if err := (cleaner{links: links}).clean(testPins); err != nil {
		t.Fatal(err)
	}
	var tables []string
	for _, r := range links.routes {
		tables = append(tables, fmt.Sprintf("%d %s", r.Table, destination(r)))
	}
	if want := []string{"9346 0.0.0.0/0", "254 10.0.0.0/8", "2022 ::/0"}; !slices.Equal(tables, want) {
		t.Fatalf("routes left %v, want %v", tables, want)
	}
	// netlink cannot delete a route without a destination, which is how the kernel lists a
	// default route.
	for _, call := range []string{"delete route to 0.0.0.0/0 in table 9347", "delete route to ::/0 in table 9347"} {
		if !slices.Contains(links.calls, call) {
			t.Errorf("no %q in %v", call, links.calls)
		}
	}
}

func TestCleanRevertsTheDNSOfALiveTUNAndDeletesIt(t *testing.T) {
	links := leftovers()
	links.links[testPins.Device] = tun(testPins.Device)
	reverted := func(device string) error {
		links.calls = append(links.calls, "revert "+device)
		return nil
	}
	if err := (cleaner{links: links, revert: reverted}).clean(testPins); err != nil {
		t.Fatal(err)
	}
	if len(links.links) != 0 {
		t.Fatalf("devices left: %v", links.links)
	}
	last := links.calls[len(links.calls)-2:]
	if want := []string{"revert openweir0", "delete device openweir0"}; !slices.Equal(last, want) {
		t.Fatalf("calls end with %v, want %v", last, want)
	}
}

// Deleting the device makes resolved forget it too, so a failed revert, or none, is no error.
func TestCleanDeletesTheTUNWithoutResolved(t *testing.T) {
	for name, revert := range map[string]func(string) error{
		"no resolvectl": nil,
		"resolved down": func(string) error { return errors.New("resolvectl revert: exit status 1") },
	} {
		links := leftovers()
		links.links[testPins.Device] = tun(testPins.Device)
		if err := (cleaner{links: links, revert: revert}).clean(testPins); err != nil {
			t.Errorf("%s: %v", name, err)
		}
		if len(links.links) != 0 {
			t.Errorf("%s: devices left: %v", name, links.links)
		}
	}
}

func TestCleanLeavesADeviceThatIsNotATUN(t *testing.T) {
	links := leftovers()
	links.links[testPins.Device] = &netlink.Dummy{LinkAttrs: netlink.LinkAttrs{Name: testPins.Device, Index: 7}}
	revert := func(device string) error {
		t.Errorf("reverted %s, which is not the TUN", device)
		return nil
	}
	err := (cleaner{links: links, revert: revert}).clean(testPins)
	if err == nil || !strings.Contains(err.Error(), "device openweir0, a dummy rather than a TUN") {
		t.Fatalf("clean = %v", err)
	}
	if _, ok := links.links[testPins.Device]; !ok {
		t.Fatal("the dummy device was deleted")
	}
	// The rules and the table go all the same.
	if slices.ContainsFunc(links.rules[netlink.FAMILY_V6], func(r netlink.Rule) bool { return testPins.HoldsRule(r.Priority) }) {
		t.Fatal("the TUN's rules stayed")
	}
}

func TestCleanWithNothingLeftDoesNothing(t *testing.T) {
	links := &fakeLinks{rules: map[int][]netlink.Rule{
		netlink.FAMILY_V4: {rule(netlink.FAMILY_V4, 0), rule(netlink.FAMILY_V4, 32766)},
	}}
	revert := func(device string) error {
		t.Errorf("reverted %s, which is not there", device)
		return nil
	}
	if err := (cleaner{links: links, revert: revert}).clean(testPins); err != nil {
		t.Fatal(err)
	}
	if len(links.calls) != 0 {
		t.Fatalf("calls %v", links.calls)
	}
}

func TestCleanIsIdempotent(t *testing.T) {
	links := leftovers()
	links.links[testPins.Device] = tun(testPins.Device)
	c := cleaner{links: links, revert: func(string) error { return nil }}
	if err := c.clean(testPins); err != nil {
		t.Fatal(err)
	}
	links.calls = nil
	if err := c.clean(testPins); err != nil {
		t.Fatal(err)
	}
	if len(links.calls) != 0 {
		t.Fatalf("the second clean-up deleted %v", links.calls)
	}
}

// What vanished between the listing and the deletion, by sing-tun closing late or a second
// clean-up, is gone as wanted.
func TestCleanToleratesWhatVanishesMeanwhile(t *testing.T) {
	links := leftovers()
	links.links[testPins.Device] = tun(testPins.Device)
	links.deleteRule, links.deleteRoute, links.deleteLink = unix.ENOENT, unix.ESRCH, unix.ENODEV
	if err := (cleaner{links: links}).clean(testPins); err != nil {
		t.Fatal(err)
	}
}

func TestCleanSaysWhatItLeft(t *testing.T) {
	links := leftovers()
	links.links[testPins.Device] = tun(testPins.Device)
	links.deleteRule, links.deleteRoute, links.deleteLink = unix.EPERM, unix.EPERM, unix.EPERM
	revert := func(string) error { return errors.New("resolvectl revert: exit status 1") }
	err := (cleaner{links: links, revert: revert}).clean(testPins)
	if !errors.Is(err, unix.EPERM) {
		t.Fatalf("clean = %v", err)
	}
	message := err.Error()
	for _, want := range []string{
		"cleaning up after openweir0 left IPv4 rule 9347: operation not permitted",
		"IPv4 rule 9357: operation not permitted",
		"IPv6 rule 9347: operation not permitted",
		"IPv4 route to 0.0.0.0/0 in table 9347: operation not permitted",
		"IPv4 route to 198.18.0.0/30 in table 9347",
		"IPv6 route to ::/0 in table 9347",
		"device openweir0: operation not permitted",
		"systemd-resolved's settings for openweir0: resolvectl revert: exit status 1",
	} {
		if !strings.Contains(message, want) {
			t.Errorf("no %q in:\n%s", want, message)
		}
	}
	for _, unwanted := range []string{"9346", "9358", "table 254", "table 2022"} {
		if strings.Contains(message, unwanted) {
			t.Errorf("%q in:\n%s", unwanted, message)
		}
	}
}

// A listing that fails cannot say what is there, so the error names the whole range; the
// other steps still run.
func TestCleanSaysWhatItCouldNotList(t *testing.T) {
	links := leftovers()
	links.links[testPins.Device] = tun(testPins.Device)
	links.listRules, links.listRoutes, links.lookUp = unix.ENOBUFS, unix.ENOBUFS, unix.ENOBUFS
	err := (cleaner{links: links}).clean(testPins)
	for _, want := range []string{
		"IPv4 rules 9347 to 9357, which it could not list",
		"IPv4 routes of table 9347, which it could not list",
		"IPv6 routes of table 9347, which it could not list",
		"device openweir0, which it could not look up",
	} {
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("no %q in %v", want, err)
		}
	}
	if slices.ContainsFunc(links.rules[netlink.FAMILY_V6], func(r netlink.Rule) bool { return testPins.HoldsRule(r.Priority) }) {
		t.Fatal("the IPv6 rules stayed although they could be listed")
	}
}

func TestCleanRefusesPinsThatAreNotTheTunnels(t *testing.T) {
	for _, pins := range []Pins{{}, {Device: "openweir0", Table: 254, RuleIndex: 9347}, {Device: "-h", Table: 9347, RuleIndex: 9347}} {
		if err := Clean(pins); err == nil {
			t.Errorf("Clean(%+v) passed", pins)
		}
	}
}

// Clean on this machine's own rules, routes and devices, with pins nothing uses: it lists them
// all through netlink, finds nothing of the pins' and so needs no privilege. As root it could
// delete, so it does not run as root.
func TestCleanFindsNothingWherePinsAreUnused(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("as root Clean would delete whatever happened to sit at these pins")
	}
	unused := Pins{Device: "owunused0", Table: 4000000123, RuleIndex: 32700}
	if err := Clean(unused); err != nil {
		t.Fatal(err)
	}
}
