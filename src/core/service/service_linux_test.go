package main

import (
	"context"
	"errors"
	"flag"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"

	"github.com/openweir/app-desktop/src/core/internal/control"
	"github.com/openweir/app-desktop/src/core/internal/controlpb"
	"github.com/openweir/app-desktop/src/core/internal/frame"
	"github.com/openweir/app-desktop/src/core/internal/logind"
	"github.com/openweir/app-desktop/src/core/internal/owner"
	"github.com/openweir/app-desktop/src/core/internal/peer"
	"github.com/openweir/app-desktop/src/core/internal/product"
)

func modesFor(t *testing.T, args ...string) (func() error, error) {
	t.Helper()
	flags := flag.NewFlagSet("openweir-service", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	socket := flags.String("socket", "", "")
	modes := registerModes(flags)
	if err := flags.Parse(args); err != nil {
		t.Fatal(err)
	}
	return modes.selected(*socket != "", flags.Args())
}

func TestTheDevelopmentServerKeepsItsFlags(t *testing.T) {
	for _, args := range [][]string{{"-socket", "/tmp/dev.sock"}, {}} {
		if release, err := modesFor(t, args...); release != nil || err != nil {
			t.Fatalf("%q: a release mode (%v)", args, err)
		}
	}
}

// The modes are only selected here: run as root, they would serve, clean up or take over.
func TestTheReleaseModesAreSelected(t *testing.T) {
	for _, args := range [][]string{
		{"-system"},
		{"-system", "-foreground"},
		{"-cleanup"},
		{"-take-over"},
		{"-install-update", "openweir.deb", "SHA256SUMS", "SHA256SUMS.sig"},
	} {
		if release, err := modesFor(t, args...); err != nil || release == nil {
			t.Fatalf("%q: no release mode (%v)", args, err)
		}
	}
}

func TestTheModesExcludeOneAnother(t *testing.T) {
	for _, args := range [][]string{
		{"-socket", "/tmp/dev.sock", "-system"},
		{"-system", "-cleanup"},
		{"-take-over", "-install-update", "a", "b", "c"},
		{"-foreground"},
		{"-install-update", "openweir.deb"},
	} {
		if _, err := modesFor(t, args...); err == nil {
			t.Errorf("%q: accepted", args)
		}
	}
}

func TestTheReleaseModesRunAsRoot(t *testing.T) {
	if err := requireRoot("-system", 1000); err == nil || !strings.Contains(err.Error(), "-system") {
		t.Fatalf("requireRoot(1000) = %v", err)
	}
	if err := requireRoot("-system", 0); err != nil {
		t.Fatalf("requireRoot(0) = %v", err)
	}
}

func TestTheStateRootIsSystemds(t *testing.T) {
	for _, tc := range []struct{ variable, want string }{
		{"", product.LinuxStateDir},
		{"/var/lib/openweir", "/var/lib/openweir"},
		{"/var/lib/openweir/", "/var/lib/openweir"},
		{"/var/lib/openweir:/var/lib/other", "/var/lib/openweir"},
	} {
		if root, err := stateRoot(tc.variable); err != nil || root != tc.want {
			t.Errorf("stateRoot(%q) = %q, %v; want %q", tc.variable, root, err, tc.want)
		}
	}
	if root, err := stateRoot("openweir"); err == nil {
		t.Errorf("a relative STATE_DIRECTORY gave %q", root)
	}
}

// Only pkexec runs the take-over helper, once an administrator confirmed it.
func TestTheTakeOverHelperRunsThroughPkexec(t *testing.T) {
	for _, uid := range []string{"", "root", "-1"} {
		if err := ranByPkexec(uid); err == nil {
			t.Errorf("PKEXEC_UID=%q accepted", uid)
		}
	}
	if err := ranByPkexec("1000"); err != nil {
		t.Fatal(err)
	}
}

// The helper sends Claim{take_over} on the service's socket; service_restarting means the
// service let go of the owner.
func TestTheTakeOverHelperAsksTheService(t *testing.T) {
	socket := filepath.Join(shortDir(t), "control.sock")
	if err := takeOver(socket); err == nil {
		t.Fatal("took over with no service")
	}
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	for _, tc := range []struct {
		response *controlpb.Response
		fails    bool
	}{
		{response: failure(control.CodeServiceRestarting)},
		{response: failure(control.CodeOwnedByOtherUser), fails: true},
	} {
		served := make(chan *controlpb.Request, 1)
		go func() {
			defer close(served)
			conn, err := listener.Accept()
			if err != nil {
				t.Error(err)
				return
			}
			defer conn.Close()
			requests := answer(t, conn, tc.response)
			request := <-requests
			// answer closes requests once the response is written.
			for range requests {
			}
			served <- request
		}()
		err := takeOver(socket)
		if request := <-served; !request.GetClaim().GetTakeOver() {
			t.Fatalf("request = %v", request)
		}
		if (err != nil) != tc.fails {
			t.Fatalf("answered %v: %v", tc.response, err)
		}
	}
}

// ownerless stands in for the Linux platform of a service that has no owner yet, which asks
// it for its state directories only; the embedded nil Platform answers anything else with a
// panic.
type ownerless struct{ owner.Platform }

func (ownerless) SecureDir(dir, _ string) error { return os.MkdirAll(dir, 0o700) }

// fakeMachine stands in for root's machine: a listener of the test's own, any caller verified as
// uid 1000, no logind. ready is told once the service serves.
func fakeMachine(t *testing.T, ready func() error) (net.Listener, system) {
	t.Helper()
	dir := shortDir(t)
	listener, err := net.Listen("unix", filepath.Join(dir, "control.sock"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	return listener, system{
		root:     filepath.Join(dir, "state"),
		platform: ownerless{},
		verify: func(conn net.Conn) (net.Conn, peer.Identity, error) {
			return conn, peer.Identity{UserID: "1000", SessionID: 1000, ProcessID: 4242}, nil
		},
		watch: func(context.Context, func(uint32, bool), func(bool)) (logind.Inhibitor, error) {
			return nil, errors.ErrUnsupported
		},
		ready: ready,
	}
}

// With no owner yet the service tells systemd it is ready, answers GetInfo on its socket, and
// ends cleanly when systemd stops it.
func TestTheSystemServiceServesItsSocket(t *testing.T) {
	ready := make(chan struct{})
	listener, machine := fakeMachine(t, func() error { close(ready); return nil })
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	served := make(chan error, 1)
	go func() { served <- serveSystem(ctx, listener, slog.New(slog.DiscardHandler), machine) }()
	select {
	case <-ready:
	case err := <-served:
		t.Fatalf("the service ended before it was ready: %v", err)
	case <-time.After(30 * time.Second):
		t.Fatal("the service never became ready")
	}

	conn, err := net.Dial("unix", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	request, err := proto.Marshal(&controlpb.Request{ProtocolVersion: product.ProtocolVersion, Id: 1,
		Method: &controlpb.Request_GetInfo{GetInfo: &controlpb.GetInfoRequest{}}})
	if err != nil {
		t.Fatal(err)
	}
	if err := frame.Write(conn, frame.KindRequest, request); err != nil {
		t.Fatal(err)
	}
	payload, err := frame.Read(conn, frame.KindResponse)
	if err != nil {
		t.Fatal(err)
	}
	var response controlpb.Response
	if err := proto.Unmarshal(payload, &response); err != nil {
		t.Fatal(err)
	}
	info := response.GetGetInfo()
	if info.GetServiceVersion() != product.Version || info.GetOwnership() != controlpb.Ownership_OWNERSHIP_AVAILABLE {
		t.Fatalf("GetInfo = %v", &response)
	}
	if info, err := os.Stat(filepath.Join(machine.root, "crashes")); err != nil || !info.IsDir() {
		t.Fatalf("no crash directory in the state root: %v", err)
	}

	cancel()
	select {
	case err := <-served:
		if err != nil {
			t.Fatalf("stopped with %v", err)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("the service did not stop")
	}
}

// Had systemd not heard of it, it would stop the service; failing to tell is an error at once.
func TestTheSystemServiceEndsWhenSystemdCannotBeTold(t *testing.T) {
	refused := errors.New("refused")
	listener, machine := fakeMachine(t, func() error { return refused })
	if err := serveSystem(context.Background(), listener, slog.New(slog.DiscardHandler), machine); !errors.Is(err, refused) {
		t.Fatalf("serveSystem = %v", err)
	}
}
