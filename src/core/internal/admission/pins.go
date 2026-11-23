package admission

import (
	"errors"
	"fmt"

	LC "github.com/metacubex/mihomo/listener/config"

	"github.com/openweir/app-desktop/src/core/internal/tunclean"
)

// unpinnedTunKeys are the TUN options that pins delete (§3.3 #7). The UID, Android-user,
// package and interface lists choose whose traffic the TUN takes, and the TUN is the whole
// machine's (D66): as root the service would otherwise route the other users' traffic, or
// leave it out, as the owner's profile says. The address sets, marks and fallback rule are
// auto-redirect's, which owned mode turns off; the fallback rule would also sit outside the
// pinned rules, where the clean-up does not look.
var unpinnedTunKeys = []string{
	"include-uid",
	"include-uid-range",
	"exclude-uid",
	"exclude-uid-range",
	"include-android-user",
	"include-package",
	"exclude-package",
	"include-interface",
	"exclude-interface",
	"route-address-set",
	"route-exclude-address-set",
	"auto-redirect-input-mark",
	"auto-redirect-output-mark",
	"auto-redirect-iproute2-fallback-rule-index",
}

func pinned(pins tunclean.Pins) bool {
	return pins != tunclean.Pins{}
}

// pinTun holds the owned TUN to the host's pins (D67): the device, sing-tun's route table and
// the first of its rules, which tunclean removes after a crash, so the next start and the
// service's -cleanup find the TUN's leftovers where they look. redir-port and tproxy-port go to
// 0: they only receive what firewall rules send them, which the service never writes, and as
// root they would open ports the owner chose. Like the rest of the patch it comes after the
// overrides.
func pinTun(doc map[string]any, tun map[string]any, pins tunclean.Pins) {
	tun["device"] = pins.Device
	tun["iproute2-table-index"] = pins.Table
	tun["iproute2-rule-index"] = pins.RuleIndex
	for _, key := range unpinnedTunKeys {
		delete(tun, key)
	}
	doc["redir-port"] = 0
	doc["tproxy-port"] = 0
}

// verifyPins checks that mihomo read the pins and nothing that pinTun deletes.
func verifyPins(tun LC.Tun, redirPort, tproxyPort int, pins tunclean.Pins) error {
	if tun.Device != pins.Device || tun.IPRoute2TableIndex != pins.Table || tun.IPRoute2RuleIndex != pins.RuleIndex {
		return fmt.Errorf("the TUN is %q with table %d and rules from %d, want %q, %d and %d",
			tun.Device, tun.IPRoute2TableIndex, tun.IPRoute2RuleIndex, pins.Device, pins.Table, pins.RuleIndex)
	}
	for _, option := range []struct {
		key string
		set bool
	}{
		{"include-uid", len(tun.IncludeUID) != 0},
		{"include-uid-range", len(tun.IncludeUIDRange) != 0},
		{"exclude-uid", len(tun.ExcludeUID) != 0},
		{"exclude-uid-range", len(tun.ExcludeUIDRange) != 0},
		{"include-android-user", len(tun.IncludeAndroidUser) != 0},
		{"include-package", len(tun.IncludePackage) != 0},
		{"exclude-package", len(tun.ExcludePackage) != 0},
		{"include-interface", len(tun.IncludeInterface) != 0},
		{"exclude-interface", len(tun.ExcludeInterface) != 0},
		{"route-address-set", len(tun.RouteAddressSet) != 0},
		{"route-exclude-address-set", len(tun.RouteExcludeAddressSet) != 0},
		{"auto-redirect", tun.AutoRedirect},
		{"auto-redirect-input-mark", tun.AutoRedirectInputMark != 0},
		{"auto-redirect-output-mark", tun.AutoRedirectOutputMark != 0},
		{"auto-redirect-iproute2-fallback-rule-index", tun.AutoRedirectIPRoute2FallbackRuleIndex != 0},
	} {
		if option.set {
			return fmt.Errorf("tun.%s survived the pins", option.key)
		}
	}
	if redirPort != 0 || tproxyPort != 0 {
		return errors.New("redir-port or tproxy-port survived the pins")
	}
	return nil
}
