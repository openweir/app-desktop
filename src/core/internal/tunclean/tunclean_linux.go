package tunclean

import (
	"errors"
	"fmt"

	"github.com/sagernet/netlink"
)

// families are the address families of sing-tun's rules and routes, which Clean removes with
// the netlink package sing-tun writes them with.
var families = [...]int{netlink.FAMILY_V4, netlink.FAMILY_V6}

// Clean is to delete the rules of families from pins.RuleIndex to RuleIndex+RuleSpan, flush
// pins.Table, revert systemd-resolved's settings for pins.Device and delete the device if it
// is still there. It is idempotent: nothing left behind is no error.
//
// Not implemented yet: it checks pins, then reports errors.ErrUnsupported.
func Clean(pins Pins) error {
	if err := pins.Validate(); err != nil {
		return err
	}
	return fmt.Errorf("cleaning up after %s: not implemented yet: %w", pins.Device, errors.ErrUnsupported)
}
