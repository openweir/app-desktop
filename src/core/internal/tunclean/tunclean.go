// Package tunclean removes what the Linux service's TUN leaves behind when the service dies
// without closing it (docs/architecture.md "Linux (in progress)", D67): sing-tun's policy
// routing rules and table, and systemd-resolved's settings for the device. With strict-route
// those rules send every packet of a family the TUN has no address for nowhere until
// something removes them, and sing-tun itself does so only at its next start. The service runs
// Clean before it starts and after it stops (-cleanup), and holds every profile's TUN to the
// same Pins (internal/admission), so Clean removes the tunnel's own rules and no one else's.
package tunclean

import (
	"fmt"
	"math"
	"strings"
)

// RuleSpan is how far above Pins.RuleIndex sing-tun's auto-route writes its rules: the last
// one, where its exclusions jump to, sits at RuleIndex+RuleSpan.
const RuleSpan = 10

// Pins are the TUN device, the route table and the first rule priority that the service holds
// a profile's TUN to (iproute2-table-index, iproute2-rule-index): product.LinuxTunDevice,
// LinuxRouteTable and LinuxRuleIndex. The zero value pins nothing.
type Pins struct {
	Device    string
	Table     int
	RuleIndex int
}

// Validate refuses pins that would make Clean remove what is not the tunnel's: the kernel's
// tables and rules, or sing-tun's random table (0). The device must be a name the kernel
// takes for an interface, and one that resolvectl cannot read as an option.
func (p Pins) Validate() error {
	switch {
	case p.Device == "" || len(p.Device) > 15:
		return fmt.Errorf("TUN device %q: a Linux interface name has 1 to 15 bytes", p.Device)
	case p.Device == "." || p.Device == ".." || strings.HasPrefix(p.Device, "-") ||
		strings.ContainsAny(p.Device, "/:% \t\n\v\f\r"):
		return fmt.Errorf("TUN device %q is not a name for a Linux interface", p.Device)
	case p.Table <= 0 || p.Table >= 253 && p.Table <= 255 || int64(p.Table) > math.MaxUint32:
		return fmt.Errorf("route table %d is the kernel's or none", p.Table)
	case p.RuleIndex <= 0 || p.RuleIndex+RuleSpan >= 32766:
		return fmt.Errorf("rule index %d: its rules must come after 0 and before main (32766)", p.RuleIndex)
	}
	return nil
}

// HoldsRule says whether a rule of this priority is the TUN's: from RuleIndex to
// RuleIndex+RuleSpan, both included, the range sing-tun itself removes before it starts.
func (p Pins) HoldsRule(priority int) bool {
	return priority >= p.RuleIndex && priority <= p.RuleIndex+RuleSpan
}
