//go:build !linux

package tunclean

import (
	"errors"
	"fmt"
)

// Clean is Linux's, where sing-tun's auto-route leaves policy routing behind; elsewhere it
// reports errors.ErrUnsupported.
func Clean(pins Pins) error {
	return fmt.Errorf("cleaning up after %s: %w", pins.Device, errors.ErrUnsupported)
}
