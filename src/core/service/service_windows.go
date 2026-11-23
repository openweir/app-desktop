package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"
	"unsafe"

	"github.com/Microsoft/go-winio"
	"github.com/openweir/subconv/rules"
	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"

	"github.com/openweir/app-desktop/src/core/internal/admission"
	"github.com/openweir/app-desktop/src/core/internal/control"
	"github.com/openweir/app-desktop/src/core/internal/crashlog"
	"github.com/openweir/app-desktop/src/core/internal/engine"
	"github.com/openweir/app-desktop/src/core/internal/logfile"
	"github.com/openweir/app-desktop/src/core/internal/operation"
	"github.com/openweir/app-desktop/src/core/internal/owner"
	"github.com/openweir/app-desktop/src/core/internal/peer"
	"github.com/openweir/app-desktop/src/core/internal/power"
	"github.com/openweir/app-desktop/src/core/internal/product"
	"github.com/openweir/app-desktop/src/core/internal/server"
)

const (
	pipeBufferSize = 64 << 10
	logFile        = "service.log"
	// logLimit is when service.log moves aside to service.log.1, replacing it, at any write.
	logLimit = 1 << 20
	// geodataDir, beside the executable, holds the GEO data the installer ships.
	geodataDir = "geodata"
	// restartDelay lets the answer to a claim leave before the process stops.
	restartDelay = 200 * time.Millisecond
	// stopWait bounds how long the restart helper waits for the old process to stop.
	stopWait = time.Minute
	// dialWait bounds how long the take-over helper waits for the service's pipe.
	dialWait = 10 * time.Second
)

func run() error {
	console := flag.Bool("console", false, "run in the foreground from an administrator terminal")
	restart := flag.Bool("restart", false, "start the service again once it has stopped; the service runs this for a new owner")
	takeOver := flag.Bool("take-over", false, "stop another user's tunnel and make the user running this the owner; the UI runs this elevated")
	flag.Parse()
	if *restart {
		return restartService()
	}
	if *takeOver {
		return takeOverTunnel()
	}
	isService, err := svc.IsWindowsService()
	if err != nil {
		return err
	}
	if isService {
		return svc.Run(product.ServiceName, service{})
	}
	if !*console {
		return errors.New("not started by the Service Control Manager; pass -console to run in the foreground")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	return serve(ctx, nil, power.NewQueue(), func() {
		slog.Warn("the service has a new owner; start it again to serve them")
		time.AfterFunc(restartDelay, stop)
	})
}

// serviceRoot is %ProgramData%\OpenWeir, where only SYSTEM and Administrators get in
// (docs/architecture.md §5.1).
func serviceRoot() (string, error) {
	programData, err := windows.KnownFolderPath(windows.FOLDERID_ProgramData, 0)
	if err != nil {
		return "", err
	}
	return filepath.Join(programData, product.DisplayName), nil
}

// sessionEvent is a sign-in or sign-out the SCM reported.
type sessionEvent struct {
	session uint32
	signIn  bool
}

// serve runs the control pipe until ctx ends. restart asks for a new service process,
// which a new owner needs: mihomo's home is fixed for the life of a process (§3.2).
func serve(ctx context.Context, sessions <-chan sessionEvent, powerEvents *power.Queue, restart func()) error {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	root, err := serviceRoot()
	if err != nil {
		return err
	}
	platform := owner.Windows(restart)
	if err := platform.SecureDir(root, ""); err != nil {
		return err
	}
	logger, closeLog, err := openLog(filepath.Join(root, logFile))
	if err != nil {
		return err
	}
	defer closeLog()
	slog.SetDefault(logger)
	// Native service dumps may include startup state with no owner. Keep them SYSTEM-only,
	// outside the readable per-owner homes, and leave collection to an administrator.
	nativeCrashes := filepath.Join(root, "crashes", "native")
	if err := platform.SecureDir(filepath.Dir(nativeCrashes), ""); err != nil {
		logger.Warn("native crash directory unavailable", "error", err)
	} else if err := platform.SecureDir(nativeCrashes, ""); err != nil {
		logger.Warn("native crash directory unavailable", "error", err)
	} else if err := crashlog.PruneNative(nativeCrashes, time.Now()); err != nil {
		logger.Warn("prune native crash reports", "error", err)
	}
	host, home, err := owner.Open(ctx, root, filepath.Join(filepath.Dir(executable), geodataDir), platform, logger)
	if err != nil {
		logger.Error("open the owner's home", "error", err)
		return err
	}
	// A service with no owner yet has no engine: it answers GetInfo and Claim only.
	crashHome := home
	if crashHome == "" {
		crashHome = root
	}
	if closeCrashes, err := crashlog.Open(filepath.Join(crashHome, "crashes")); err != nil {
		logger.Warn("crash capture unavailable", "error", err)
	} else {
		defer closeCrashes()
	}
	if err := loadNativeRuntime(filepath.Dir(executable)); err != nil {
		// Keep other outbounds usable, but Naive will return this same load failure.
		logger.Warn("Naive native runtime unavailable", "error", err)
	}
	var core control.Engine
	if home != "" {
		opened, err := engine.New(engine.Options{HomeDir: home, Tun: admission.TunOwned, TemplateHosts: rules.DefaultHosts()})
		if err != nil {
			logger.Error("open the engine", "home", home, "error", err)
			return err
		}
		defer opened.Close()
		core = opened
	}
	var signer peer.Signer
	if product.SignedRelease == "true" {
		if product.WindowsSigner == "" || product.WindowsSignerEKU == "" {
			return errors.New("a signed release needs WindowsSigner and WindowsSignerEKU")
		}
		signer = peer.Signer{Name: product.WindowsSigner, EKU: product.WindowsSignerEKU}
	}
	// The service's own executable connects as the take-over helper (decision D5b).
	verify := peer.PipeClientVerifier(filepath.Join(filepath.Dir(executable), product.UIExecutable), executable, signer)
	dispatcher := control.New(control.Options{Engine: core, HostMethods: true, Host: host, OnUnresponsive: operation.Terminate})
	defer dispatcher.Close()
	defer host.Close()
	// The pipe is there before the owner's tunnel comes back, which Attach leaves to the
	// background: after a crash the UI can connect, and disconnect, while the core starts.
	listener, err := winio.ListenPipe(product.PipeName, &winio.PipeConfig{
		SecurityDescriptor: product.PipeSDDL,
		InputBufferSize:    pipeBufferSize,
		OutputBufferSize:   pipeBufferSize,
	})
	if err != nil {
		return fmt.Errorf("listen on %s: %w", product.PipeName, err)
	}
	host.Attach(dispatcher)
	watcher, err := watchPower(powerEvents)
	if err != nil {
		logger.Warn("native sleep/wake notifications unavailable; SCM events remain enabled", "error", err)
	} else {
		defer watcher.Close()
	}
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case event := <-sessions:
				host.SessionChanged(event.session, event.signIn)
			}
		}
	}()
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case <-powerEvents.Changed():
				host.PowerChanged(powerEvents.Latest())
			}
		}
	}()

	logger.Info("service started", "version", product.Version, "core", engine.Version(), "owned", home != "",
		"signed", product.SignedRelease == "true")
	return ending(ctx, server.Serve(ctx, listener, verify, dispatcher, logger))
}

// ending is what serve returns once the control server returned err. The server returns nil
// once ctx is done, as it is for a new owner too: when the restart helper did not start,
// errRestartHelper takes its place, so that the SCM's recovery starts the service again.
func ending(ctx context.Context, err error) error {
	if cause := context.Cause(ctx); err == nil && errors.Is(cause, errRestartHelper) {
		return cause
	}
	return err
}

// openLog appends to service.log, which moves aside whenever it outgrows logLimit.
func openLog(path string) (*slog.Logger, func(), error) {
	// The log moves aside whenever it outgrows logLimit, not only at start: the service runs
	// for months, and a client connecting again and again must not fill the disk.
	file, err := logfile.Open(path, logLimit)
	if err != nil {
		return nil, nil, err
	}
	// A service has no console; the foreground run also prints there.
	logger := slog.New(slog.NewTextHandler(io.MultiWriter(file, os.Stderr), nil))
	return logger, func() { file.Close() }, nil
}

// errRestartHelper stops a service whose restart helper did not start. The SCM's recovery
// starts a service again when it exits with an error (package/windows/setup.ps1 sets its
// failureflag), not when it exits cleanly.
var errRestartHelper = errors.New("the restart helper did not start")

// restartLater runs startHelper, which starts the service again once this process stopped,
// then stops the process. Without the helper, it stops with errRestartHelper as the cause.
func restartLater(stop context.CancelCauseFunc, startHelper func() error) {
	err := startHelper()
	if err != nil {
		err = fmt.Errorf("%w: %w", errRestartHelper, err)
		slog.Error("start the restart helper; the service stops with an error for the SCM's recovery to start it again", "error", err)
	}
	time.AfterFunc(restartDelay, func() { stop(err) })
}

// startRestartHelper runs this executable with -restart, apart from the service.
func startRestartHelper() error {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	helper := exec.Command(executable, "-restart")
	helper.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: windows.CREATE_NEW_PROCESS_GROUP | windows.DETACHED_PROCESS,
	}
	return helper.Start()
}

// exitCode is the service's exit code for the error serve returned. Any error but nil makes
// the SCM's recovery start the service again.
func exitCode(err error) uint32 {
	if err != nil {
		slog.Error("the service stopped with an error", "error", err)
		return 1
	}
	return 0
}

// restartService is the helper: it waits for the service to stop, then starts it.
func restartService() error {
	manager, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer manager.Disconnect()
	service, err := manager.OpenService(product.ServiceName)
	if err != nil {
		return err
	}
	defer service.Close()
	for deadline := time.Now().Add(stopWait); ; time.Sleep(100 * time.Millisecond) {
		status, err := service.Query()
		if err != nil {
			return err
		}
		if status.State == svc.Stopped {
			return service.Start()
		}
		if time.Now().After(deadline) {
			return errors.New("the service did not stop")
		}
	}
}

// takeOverTunnel is the take-over helper, which the UI runs elevated once the administrator
// confirmed it through UAC (decision D5b, docs/architecture.md §5.4). The service accepts
// this executable on its pipe only with an administrator's rights.
func takeOverTunnel() error {
	ctx, cancel := context.WithTimeout(context.Background(), dialWait)
	defer cancel()
	conn, err := winio.DialPipeContext(ctx, product.PipeName)
	if err != nil {
		return err
	}
	defer conn.Close()
	return requestTakeOver(conn)
}

type service struct{}

func (service) Execute(_ []string, requests <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	status <- svc.Status{State: svc.StartPending}
	ctx, cancel := context.WithCancelCause(context.Background())
	defer cancel(nil)
	sessions := make(chan sessionEvent, 64)
	powerEvents := power.NewQueue()
	done := make(chan error, 1)
	// A new owner restarts the service: the helper starts it again at once. Only an error
	// exit makes the SCM's recovery do it, so without the helper the service ends in one.
	go func() {
		done <- serve(ctx, sessions, powerEvents, func() { restartLater(cancel, startRestartHelper) })
	}()
	status <- svc.Status{
		State:   svc.Running,
		Accepts: svc.AcceptStop | svc.AcceptShutdown | svc.AcceptSessionChange | svc.AcceptPowerEvent,
	}
	for {
		select {
		case err := <-done:
			status <- svc.Status{State: svc.StopPending}
			return false, exitCode(err)
		case request := <-requests:
			switch request.Cmd {
			case svc.Interrogate:
				status <- request.CurrentStatus
			case svc.PowerEvent:
				powerEvents.Publish(request.EventType)
			case svc.Stop, svc.Shutdown:
				status <- svc.Status{State: svc.StopPending}
				cancel(nil)
				select {
				case err := <-done:
					return false, exitCode(err)
				case <-time.After(20 * time.Second):
					// Returning lets svc.Run and main exit even if library cleanup is stuck.
					// The SCM requested this stop: report a clean stop, not a recovery loop.
					slog.Error("service cleanup exceeded the shutdown deadline")
					return false, 0
				}
			case svc.SessionChange:
				// The owner signing out stops the tunnel and their signing in brings it back
				// (decision D5, docs/architecture.md §5.4); serve takes it from here.
				notification := *(**windows.WTSSESSION_NOTIFICATION)(unsafe.Pointer(&request.EventData))
				var event sessionEvent
				switch request.EventType {
				case windows.WTS_SESSION_LOGON:
					event = sessionEvent{session: notification.SessionID, signIn: true}
				case windows.WTS_SESSION_LOGOFF:
					event = sessionEvent{session: notification.SessionID}
				default:
					continue
				}
				select {
				case sessions <- event:
				default:
					slog.Warn("dropped a session change", "session", event.session)
				}
			}
		}
	}
}
