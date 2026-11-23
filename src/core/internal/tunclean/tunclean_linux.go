package tunclean

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"time"

	"github.com/sagernet/netlink"
	"golang.org/x/sys/unix"
)

// families are the address families of sing-tun's rules and routes, which Clean removes with
// the netlink package sing-tun writes them with.
var families = [...]int{netlink.FAMILY_V4, netlink.FAMILY_V6}

// resolvectlWait bounds resolvectl, which waits on systemd-resolved over D-Bus; systemd runs
// Clean before the service starts and would otherwise wait for it as long as resolved hangs.
const resolvectlWait = 10 * time.Second

// links is what Clean needs of netlink.Handle; the tests give it a fake.
type links interface {
	RuleList(family int) ([]netlink.Rule, error)
	RuleDel(rule *netlink.Rule) error
	RouteListFiltered(family int, filter *netlink.Route, filterMask uint64) ([]netlink.Route, error)
	RouteDel(route *netlink.Route) error
	LinkByName(name string) (netlink.Link, error)
	LinkDel(link netlink.Link) error
}

// Clean deletes the IPv4 and IPv6 rules from pins.RuleIndex to RuleIndex+RuleSpan, flushes
// pins.Table and, if pins.Device is still there, reverts systemd-resolved's settings for it
// and deletes it. It is idempotent: nothing left behind is no error, and neither is what
// vanished while it ran. Its error names everything it had to leave. Deleting needs
// CAP_NET_ADMIN; finding nothing to delete does not.
func Clean(pins Pins) error {
	if err := pins.Validate(); err != nil {
		return err
	}
	handle, err := netlink.NewHandle(unix.NETLINK_ROUTE)
	if err != nil {
		return fmt.Errorf("cleaning up after %s: %w", pins.Device, err)
	}
	defer handle.Close()
	return cleaner{links: handle, revert: resolvectl()}.clean(pins)
}

type cleaner struct {
	links links
	// revert tells systemd-resolved to forget what it was told about a device; nil when
	// there is no resolved to tell (resolvectl).
	revert func(device string) error
}

func (c cleaner) clean(pins Pins) error {
	// The rules first: once they are gone the table is out of every lookup, and the
	// machine's own routes work again even if what follows fails.
	left := c.rules(pins)
	left = append(left, c.table(pins)...)
	left = append(left, c.device(pins)...)
	if len(left) > 0 {
		return fmt.Errorf("cleaning up after %s left %w", pins.Device, errors.Join(left...))
	}
	return nil
}

// rules deletes the TUN's rules of each family. A rule is deleted by its family and priority,
// as sing-tun does, once for each rule listed there: the listing leaves out the action, so
// the rule as listed would not match an unreachable one.
func (c cleaner) rules(pins Pins) []error {
	var left []error
	for _, family := range families {
		rules, err := c.links.RuleList(family)
		if err != nil {
			left = append(left, fmt.Errorf("%s rules %d to %d, which it could not list: %w",
				familyName(family), pins.RuleIndex, pins.RuleIndex+RuleSpan, err))
			continue
		}
		for _, rule := range rules {
			if !pins.HoldsRule(rule.Priority) {
				continue
			}
			target := netlink.NewRule()
			target.Family = family
			target.Priority = rule.Priority
			if err := c.links.RuleDel(target); err != nil && !errors.Is(err, unix.ENOENT) {
				left = append(left, fmt.Errorf("%s rule %d: %w", familyName(family), rule.Priority, err))
			}
		}
	}
	return left
}

// table deletes every route of pins.Table. The kernel lists a default route without a
// destination, which netlink cannot delete, so it gets the family's whole range.
func (c cleaner) table(pins Pins) []error {
	var left []error
	for _, family := range families {
		routes, err := c.links.RouteListFiltered(family, &netlink.Route{Table: pins.Table}, netlink.RT_FILTER_TABLE)
		if err != nil {
			left = append(left, fmt.Errorf("%s routes of table %d, which it could not list: %w",
				familyName(family), pins.Table, err))
			continue
		}
		for _, route := range routes {
			if route.Table != pins.Table {
				continue
			}
			if route.Dst == nil {
				route.Dst = everything(family)
			}
			if err := c.links.RouteDel(&route); err != nil && !errors.Is(err, unix.ESRCH) {
				left = append(left, fmt.Errorf("%s route to %s in table %d: %w",
					familyName(family), route.Dst, pins.Table, err))
			}
		}
	}
	return left
}

// device deletes the TUN if it is still there, which it is only while some process holds
// it open: the kernel removes a TUN with its last descriptor, and systemd-resolved forgets a
// device that goes. A device of that name that is not a TUN is someone else's and stays.
func (c cleaner) device(pins Pins) []error {
	link, err := c.links.LinkByName(pins.Device)
	var missing netlink.LinkNotFoundError
	switch {
	case errors.As(err, &missing):
		return nil
	case err != nil:
		return []error{fmt.Errorf("device %s, which it could not look up: %w", pins.Device, err)}
	case link.Type() != "tuntap":
		return []error{fmt.Errorf("device %s, a %s rather than a TUN", pins.Device, link.Type())}
	}
	// Reverted first, while resolved still knows the device; deleting it would make resolved
	// forget it too, so a failed revert only matters when the device stays.
	var reverted error
	if c.revert != nil {
		reverted = c.revert(pins.Device)
	}
	if err := c.links.LinkDel(link); err != nil && !errors.Is(err, unix.ENODEV) {
		left := []error{fmt.Errorf("device %s: %w", pins.Device, err)}
		if reverted != nil {
			left = append(left, fmt.Errorf("systemd-resolved's settings for %s: %w", pins.Device, reverted))
		}
		return left
	}
	return nil
}

// resolvectl reverts a device's settings in systemd-resolved, as sing-tun does when it closes
// the TUN, or is nil when that cannot be done from here: without resolvectl, or from a network
// namespace other than PID 1's. resolved serves PID 1's namespace and takes the device by its
// index, so from another namespace the revert would land on whichever of resolved's devices
// has that index (the CI's TUN tests run in such namespaces).
func resolvectl() func(device string) error {
	path, err := exec.LookPath("resolvectl")
	if err != nil {
		return nil
	}
	self, selfErr := os.Readlink("/proc/self/ns/net")
	host, hostErr := os.Readlink("/proc/1/ns/net")
	if selfErr == nil && hostErr == nil && self != host {
		return nil
	}
	return func(device string) error {
		ctx, cancel := context.WithTimeout(context.Background(), resolvectlWait)
		defer cancel()
		output, err := exec.CommandContext(ctx, path, "revert", device).CombinedOutput()
		if err != nil {
			return fmt.Errorf("resolvectl revert: %w: %s", err, bytes.TrimSpace(output))
		}
		return nil
	}
}

func everything(family int) *net.IPNet {
	if family == netlink.FAMILY_V6 {
		return &net.IPNet{IP: net.IPv6zero, Mask: net.CIDRMask(0, 128)}
	}
	return &net.IPNet{IP: net.IPv4zero.To4(), Mask: net.CIDRMask(0, 32)}
}

func familyName(family int) string {
	if family == netlink.FAMILY_V6 {
		return "IPv6"
	}
	return "IPv4"
}
