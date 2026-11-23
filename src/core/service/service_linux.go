package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/openweir/subconv/rules"

	"github.com/openweir/app-desktop/src/core/internal/admission"
	"github.com/openweir/app-desktop/src/core/internal/control"
	"github.com/openweir/app-desktop/src/core/internal/crashlog"
	"github.com/openweir/app-desktop/src/core/internal/engine"
	"github.com/openweir/app-desktop/src/core/internal/logind"
	"github.com/openweir/app-desktop/src/core/internal/operation"
	"github.com/openweir/app-desktop/src/core/internal/owner"
	"github.com/openweir/app-desktop/src/core/internal/peer"
	"github.com/openweir/app-desktop/src/core/internal/product"
	"github.com/openweir/app-desktop/src/core/internal/server"
	"github.com/openweir/app-desktop/src/core/internal/tunclean"
)

// The Linux release runs this binary as product.LinuxServicePath in the modes below
// (docs/architecture.md "Linux (in progress)", D60); -socket stays the development server.
// systemd runs -system for <product.LinuxUnit>.service, with -cleanup before it starts and
// after it stops; the UI runs -take-over and -install-update through pkexec.
const noMode = "-system or -socket is required: -system runs the service under systemd, -socket the development server"

const (
	// geodataDir, beside the executable, holds the GEO data the package ships.
	geodataDir = "geodata"
	// restartDelay lets the answer to a claim leave before the process exits.
	restartDelay = 200 * time.Millisecond
	// takeOverWait bounds the take-over helper's exchange: systemd may have to start the
	// service first, and the other user's tunnel has to stop.
	takeOverWait = time.Minute
)

// errNotImplemented is what the release modes answer until they are written.
var errNotImplemented = errors.New("not implemented yet")

// errNewOwner ends the service for a new owner: mihomo's home is fixed for the life of a
// process (§3.2), so the process exits cleanly and systemd starts it again (Restart=always).
var errNewOwner = errors.New("the service has a new owner")

// pins hold the service's TUN to product's device, table and rules, which -cleanup removes
// (D67).
var pins = tunclean.Pins{Device: product.LinuxTunDevice, Table: product.LinuxRouteTable, RuleIndex: product.LinuxRuleIndex}

type releaseModes struct {
	system, foreground, cleanup, takeOver, installUpdate *bool
}

func registerModes(flags *flag.FlagSet) releaseModes {
	return releaseModes{
		system: flags.Bool("system", false, "run as the systemd service, as root, on the socket systemd passes"),
		foreground: flags.Bool("foreground", false,
			"with -system: listen on the socket itself instead of systemd's, from a root terminal once the units are stopped"),
		cleanup: flags.Bool("cleanup", false, "remove the routing rules, route table and DNS settings a TUN left behind; systemd runs this before and after the service"),
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

// requireRoot refuses mode to anyone but root; systemd and pkexec run the release modes so.
func requireRoot(mode string, euid int) error {
	if euid != 0 {
		return fmt.Errorf("%s runs as root", mode)
	}
	return nil
}

// runSystem serves product.LinuxSocket as root until systemd stops the service or it has a new
// owner: on the socket systemd passes, or with foreground on one of its own. It logs to stderr,
// which journald keeps.
func runSystem(foreground bool) error {
	if err := requireRoot("-system", os.Geteuid()); err != nil {
		return err
	}
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	slog.SetDefault(logger)
	// As with the socket, systemd means NOTIFY_SOCKET for this process, not its children.
	notifySocket := os.Getenv("NOTIFY_SOCKET")
	os.Unsetenv("NOTIFY_SOCKET")
	var listener net.Listener
	var err error
	if foreground {
		listener, err = listenItself(product.LinuxSocket, procNetUnix)
	} else {
		listener, err = systemdListener()
	}
	if err != nil {
		return err
	}
	defer listener.Close()
	base, cancel := context.WithCancelCause(context.Background())
	defer cancel(nil)
	ctx, stop := signal.NotifyContext(base, os.Interrupt, syscall.SIGTERM)
	defer stop()
	// The owner calls restart as it answers service_restarting, which leaves first.
	restart := func() { time.AfterFunc(restartDelay, func() { cancel(errNewOwner) }) }
	root, err := stateRoot(os.Getenv("STATE_DIRECTORY"))
	if err != nil {
		return err
	}
	err = serveSystem(ctx, listener, logger, system{
		root:     root,
		platform: owner.Linux(restart),
		verify:   peer.LinuxClientVerifier(product.LinuxUIPath, product.LinuxServicePath),
		watch:    logind.Watch,
		ready:    func() error { return notify(notifySocket, "READY=1") },
	})
	if errors.Is(context.Cause(base), errNewOwner) {
		if foreground {
			logger.Warn("the service has a new owner; start it again to serve them")
		} else {
			logger.Info("the service has a new owner; systemd starts it again")
		}
	}
	return err
}

// system is what serveSystem needs of the machine, which runs it as root; tests stand in for
// it.
type system struct {
	// root is the service's state directory.
	root     string
	platform owner.Platform
	verify   peer.Verifier
	// watch follows logind's users, and the machine's sleep and wake.
	watch func(ctx context.Context, onUser func(uid uint32, signedIn bool), onSleep func(sleeping bool)) (logind.Inhibitor, error)
	// ready tells systemd that the service serves.
	ready func() error
}

// serveSystem serves the control socket on listener until ctx ends, as the Windows service
// serves its pipe.
func serveSystem(ctx context.Context, listener net.Listener, logger *slog.Logger, machine system) error {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	directory := filepath.Dir(executable)
	host, home, err := owner.Open(ctx, machine.root, filepath.Join(directory, geodataDir), machine.platform, logger)
	if err != nil {
		logger.Error("open the owner's home", "error", err)
		return err
	}
	// A service with no owner yet has no engine: it answers GetInfo and Claim only.
	crashHome := home
	if crashHome == "" {
		crashHome = machine.root
	}
	if closeCrashes, err := crashlog.Open(filepath.Join(crashHome, "crashes")); err != nil {
		logger.Warn("crash capture unavailable", "error", err)
	} else {
		defer closeCrashes()
	}
	if err := loadNativeRuntime(directory); err != nil {
		// Keep other outbounds usable, but Naive will return this same load failure.
		logger.Warn("Naive native runtime unavailable", "error", err)
	}
	var core control.Engine
	if home != "" {
		opened, err := engine.New(engine.Options{
			HomeDir: home, Tun: admission.TunOwned, TemplateHosts: rules.DefaultHosts(), Pins: pins,
		})
		if err != nil {
			logger.Error("open the engine", "home", home, "error", err)
			return err
		}
		defer opened.Close()
		core = opened
	}
	dispatcher := control.New(control.Options{Engine: core, HostMethods: true, Host: host, OnUnresponsive: operation.Terminate})
	defer dispatcher.Close()
	defer host.Close()
	// Clients may already wait on the socket; the owner's tunnel comes back in the background.
	host.Attach(dispatcher)
	// logind's users are the service's sessions: a user's uid is their session ID. Sleep
	// returns once the tunnel is down, and logind's delay lock is held until then.
	if watch, err := machine.watch(ctx, host.SessionChanged, host.Sleep); err != nil {
		logger.Warn("logind unavailable: sign-ins, sign-outs, sleep and wake go unnoticed", "error", err)
	} else {
		defer watch.Close()
	}
	if err := machine.ready(); err != nil {
		return fmt.Errorf("tell systemd the service is ready: %w", err)
	}
	logger.Info("service started", "version", product.Version, "core", engine.Version(), "owned", home != "")
	return server.Serve(ctx, listener, machine.verify, dispatcher, logger)
}

// stateRoot is the service's root: the StateDirectory systemd names in STATE_DIRECTORY, or
// product.LinuxStateDir in the foreground. The owners' homes are its users/<uid>.
func stateRoot(stateDirectory string) (string, error) {
	if stateDirectory == "" {
		return product.LinuxStateDir, nil
	}
	// systemd joins several StateDirectory= with ':'; the unit has one.
	first, _, _ := strings.Cut(stateDirectory, ":")
	if !filepath.IsAbs(first) {
		return "", fmt.Errorf("STATE_DIRECTORY %q is not an absolute path", stateDirectory)
	}
	return filepath.Clean(first), nil
}

// cleanUp removes what the service's TUN left behind (tunclean); it does nothing when there
// is nothing to remove.
func cleanUp() error {
	if err := requireRoot("-cleanup", os.Geteuid()); err != nil {
		return err
	}
	if err := tunclean.Clean(pins); err != nil {
		return fmt.Errorf("-cleanup: %w", err)
	}
	return nil
}

// takeOverAsRoot asks the service to stop another user's tunnel (requestTakeOver) once an
// administrator confirmed it through pkexec (D66). It connects as root, which the service
// accepts from its own executable only and never makes the owner: the service lets go of the
// owner and restarts, and the administrator's UI then claims.
func takeOverAsRoot() error {
	if err := requireRoot("-take-over", os.Geteuid()); err != nil {
		return err
	}
	if err := ranByPkexec(os.Getenv("PKEXEC_UID")); err != nil {
		return err
	}
	return takeOver(product.LinuxSocket)
}

// ranByPkexec checks for the uid of the user who asked, which pkexec sets: only the polkit
// action, which has an administrator confirm, is to run the helper.
func ranByPkexec(pkexecUID string) error {
	if _, err := strconv.ParseUint(pkexecUID, 10, 32); err != nil {
		return errors.New("-take-over runs through pkexec, which sets PKEXEC_UID")
	}
	return nil
}

// takeOver sends the take-over on socket.
func takeOver(socket string) error {
	conn, err := net.DialTimeout("unix", socket, takeOverWait)
	if err != nil {
		return err
	}
	defer conn.Close()
	if err := conn.SetDeadline(time.Now().Add(takeOverWait)); err != nil {
		return err
	}
	return requestTakeOver(conn)
}
