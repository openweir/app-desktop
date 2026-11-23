package main

import (
	"errors"
	"flag"
	"fmt"

	"github.com/openweir/app-desktop/src/core/internal/product"
	"github.com/openweir/app-desktop/src/core/internal/tunclean"
)

// The Linux release runs this binary as product.LinuxServicePath in the modes below
// (docs/architecture.md "Linux (in progress)", D60); -socket stays the development server.
// systemd runs -system for <product.LinuxUnit>.service, with -cleanup before it starts and
// after it stops; the UI runs -take-over and -install-update through pkexec.
const noMode = "-system or -socket is required: -system runs the service under systemd, -socket the development server"

// errNotImplemented is what the release modes answer until they are written.
var errNotImplemented = errors.New("not implemented yet")

// pins hold the service's TUN to product's device, table and rules, which -cleanup removes
// (D67).
var pins = tunclean.Pins{Device: product.LinuxTunDevice, Table: product.LinuxRouteTable, RuleIndex: product.LinuxRuleIndex}

type releaseModes struct {
	system, foreground, cleanup, takeOver, installUpdate *bool
}

func registerModes(flags *flag.FlagSet) releaseModes {
	return releaseModes{
		system:     flags.Bool("system", false, "run as the systemd service, as root, on the socket systemd passes"),
		foreground: flags.Bool("foreground", false, "with -system: listen on the socket itself instead of systemd's, from a root terminal"),
		cleanup:    flags.Bool("cleanup", false, "remove the routing rules, route table and DNS settings a TUN left behind; systemd runs this before and after the service"),
		takeOver: flags.Bool("take-over", false,
			"stop another user's tunnel so that the administrator's UI can claim it; the UI runs this through pkexec"),
		installUpdate: flags.Bool("install-update", false,
			"install the package given as <package> <SHA256SUMS> <SHA256SUMS.sig> once they check out; the UI runs this through pkexec"),
	}
}

// selected is the release mode that flags name, or nil for the development server (-socket
// set) or none at all; args are the arguments after the flags.
func (m releaseModes) selected(development bool, args []string) (func() error, error) {
	chosen := 0
	for _, on := range []bool{development, *m.system, *m.cleanup, *m.takeOver, *m.installUpdate} {
		if on {
			chosen++
		}
	}
	switch {
	case chosen > 1:
		return nil, errors.New("-socket, -system, -cleanup, -take-over and -install-update exclude one another")
	case *m.foreground && !*m.system:
		return nil, errors.New("-foreground goes with -system")
	case *m.installUpdate && len(args) != 3:
		return nil, errors.New("-install-update takes <package> <SHA256SUMS> <SHA256SUMS.sig>")
	case *m.system:
		foreground := *m.foreground
		return func() error { return runSystem(foreground) }, nil
	case *m.cleanup:
		return cleanUp, nil
	case *m.takeOver:
		return takeOverAsRoot, nil
	case *m.installUpdate:
		return func() error { return installUpdate(args[0], args[1], args[2]) }, nil
	}
	return nil, nil
}

// runSystem is to serve product.LinuxSocket as root under systemd: on the listener systemd
// passes (LISTEN_FDS), or one of its own with foreground, telling systemd once it is ready.
func runSystem(foreground bool) error {
	return fmt.Errorf("-system: %w", errNotImplemented)
}

// cleanUp removes what the service's TUN left behind (tunclean); it does nothing when there
// is nothing to remove.
func cleanUp() error {
	if err := tunclean.Clean(pins); err != nil {
		return fmt.Errorf("-cleanup: %w", err)
	}
	return nil
}

// takeOverAsRoot is to ask the service to take over another user's tunnel (requestTakeOver),
// as root through pkexec, after the administrator confirmed it (D66).
func takeOverAsRoot() error {
	return fmt.Errorf("-take-over: %w", errNotImplemented)
}
