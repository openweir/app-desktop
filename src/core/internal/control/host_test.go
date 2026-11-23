package control

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"

	"github.com/openweir/app-desktop/src/core/internal/controlpb"
	"github.com/openweir/app-desktop/src/core/internal/engine"
	"github.com/openweir/app-desktop/src/core/internal/peer"
	"github.com/openweir/app-desktop/src/core/internal/porttest"
	"github.com/openweir/app-desktop/src/core/internal/server"
	"github.com/openweir/app-desktop/src/core/internal/sysproxy"
)

// fakeHost owns the service for owner and records what the dispatcher tells it.
type fakeHost struct {
	owner         string
	claimErr      error
	applied       []*controlpb.StartRequest
	stopped       int
	suspended     int
	systemProxy   bool
	recovery      controlpb.RecoveryState
	recoveryError string
	proxyErr      error
	stopErr       error
	// tookOver lists who asked to take over; the owner package decides who may.
	tookOver []string
}

func (h *fakeHost) Ownership(caller peer.Identity) controlpb.Ownership {
	if caller.UserID == h.owner {
		return controlpb.Ownership_OWNERSHIP_CALLER
	}
	return controlpb.Ownership_OWNERSHIP_OTHER
}

func (h *fakeHost) Admit(caller peer.Identity) error {
	switch caller.UserID {
	case h.owner:
		return nil
	case "idle":
		return ErrNotClaimed
	}
	return ErrOwnedByOtherUser
}

func (h *fakeHost) Claim(peer.Identity) error { return h.claimErr }

func (h *fakeHost) TakeOver(caller peer.Identity, stop func() error) error {
	h.tookOver = append(h.tookOver, caller.UserID)
	if err := stop(); err != nil {
		return err
	}
	return h.claimErr
}

func (h *fakeHost) Applied(start *controlpb.StartRequest, current func() bool) error {
	if !current() {
		return context.Canceled
	}
	h.applied = append(h.applied, start)
	h.systemProxy = start.GetOptions().GetSystemProxyEnabled()
	return h.proxyErr
}

func (h *fakeHost) StopRequested(caller peer.Identity, supersede func()) error {
	if err := h.Admit(caller); err != nil {
		return err
	}
	supersede()
	return nil
}

func (h *fakeHost) ResumeAllowed() bool { return true }

func (h *fakeHost) RecoveryStatus() (controlpb.RecoveryState, string) {
	return h.recovery, h.recoveryError
}

func TestStatusIncludesTheHostsRecoveryState(t *testing.T) {
	host := &fakeHost{recovery: controlpb.RecoveryState_RECOVERY_STATE_WAITING_NETWORK}
	d := New(Options{Engine: core, Host: host})
	defer d.Close()
	status := call(t, d, request(1, getStatus)).GetGetStatus()
	if status.GetRecoveryState() != host.recovery {
		t.Fatal(status)
	}
	host.recovery, host.recoveryError = controlpb.RecoveryState_RECOVERY_STATE_FAILED, "network_unavailable"
	status = call(t, d, request(2, getStatus)).GetGetStatus()
	if status.GetRecoveryError() != "network_unavailable" {
		t.Fatal(status)
	}
}

func (h *fakeHost) Stopped() error {
	h.stopped++
	h.systemProxy = false
	return h.stopErr
}

func (h *fakeHost) Suspended() error {
	h.suspended++
	h.systemProxy = false
	return nil
}

func (h *fakeHost) Suspending(controlpb.Mode) {}

func (h *fakeHost) SetSystemProxy(enabled bool) error {
	if h.proxyErr != nil {
		return h.proxyErr
	}
	h.systemProxy = enabled
	return nil
}

func (h *fakeHost) SystemProxyEnabled() bool { return h.systemProxy }

func claimRequest(r *controlpb.Request) {
	r.Method = &controlpb.Request_Claim{Claim: &controlpb.ClaimRequest{}}
}

func takeOverRequest(r *controlpb.Request) {
	r.Method = &controlpb.Request_Claim{Claim: &controlpb.ClaimRequest{TakeOver: true}}
}

func getStatus(r *controlpb.Request) {
	r.Method = &controlpb.Request_GetStatus{GetStatus: &controlpb.GetStatusRequest{}}
}

// Only the owner reaches the core; everyone may ask GetInfo and Claim (§5.4).
func TestHostAdmitsTheOwner(t *testing.T) {
	host := &fakeHost{owner: "S-1-5-21-1"}
	dispatcher := New(Options{Engine: unlinked{}, HostMethods: true, Host: host})
	t.Cleanup(dispatcher.Close)
	owner, other, idle := peer.Identity{UserID: "S-1-5-21-1"}, peer.Identity{UserID: "S-1-5-21-2"}, peer.Identity{UserID: "idle"}
	ctx := context.Background()

	if got := callAs(t, dispatcher, ctx, other, request(1, getInfo)).GetGetInfo().GetOwnership(); got != controlpb.Ownership_OWNERSHIP_OTHER {
		t.Fatalf("other's ownership = %v", got)
	}
	if got := callAs(t, dispatcher, ctx, owner, request(2, getInfo)).GetGetInfo().GetOwnership(); got != controlpb.Ownership_OWNERSHIP_CALLER {
		t.Fatalf("owner's ownership = %v", got)
	}
	for id, method := range []func(*controlpb.Request){getStatus, stopRequest, systemProxyRequest(false),
		func(r *controlpb.Request) {
			r.Method = &controlpb.Request_GetGeoUpdate{GetGeoUpdate: &controlpb.GetGeoUpdateRequest{}}
		},
		func(r *controlpb.Request) {
			r.Method = &controlpb.Request_UploadGeo{UploadGeo: &controlpb.UploadGeoRequest{}}
		},
	} {
		expectError(t, callAs(t, dispatcher, ctx, other, request(uint64(10+id), method)), uint64(10+id), CodeOwnedByOtherUser)
		expectError(t, callAs(t, dispatcher, ctx, idle, request(uint64(20+id), method)), uint64(20+id), CodeNotClaimed)
	}
	expectOK(t, callAs(t, dispatcher, ctx, other, request(30, claimRequest)))

	host.claimErr = ErrRestarting
	response := callAs(t, dispatcher, ctx, other, request(31, claimRequest))
	expectError(t, response, 31, CodeServiceRestarting)
	if !response.GetError().GetRetryable() {
		t.Fatal("service_restarting is not retryable")
	}
	host.claimErr = ErrOwnedByOtherUser
	expectError(t, callAs(t, dispatcher, ctx, other, request(32, claimRequest)), 32, CodeOwnedByOtherUser)
}

// A service without an owner has no engine yet, and answers GetInfo all the same.
func TestHostWithoutAnEngine(t *testing.T) {
	host := &fakeHost{owner: "S-1-5-21-1"}
	dispatcher := New(Options{HostMethods: true, Host: host})
	t.Cleanup(dispatcher.Close)
	info := callAs(t, dispatcher, context.Background(), peer.Identity{UserID: "idle"}, request(1, getInfo)).GetGetInfo()
	if info.GetCoreVersion() != engine.Version() || info.GetState() != controlpb.RunState_RUN_STATE_STOPPED {
		t.Fatalf("info = %v", info)
	}
	// Even an owner the host lets through finds no engine until the new process opens it.
	expectError(t, callAs(t, dispatcher, context.Background(), peer.Identity{UserID: "S-1-5-21-1"}, request(2, getStatus)), 2, CodeNotClaimed)
	if dispatcher.Running() {
		t.Fatal("running without an engine")
	}
}

// Start, Reload and Resume hand the host what the core took; Stop and Suspend tell it the
// tunnel went down, for good or until the owner signs in again.
func TestHostKeepsWhatTheCoreRuns(t *testing.T) {
	host := &fakeHost{owner: "S-1-5-21-1"}
	dispatcher := New(Options{Engine: core, HostMethods: true, Host: host})
	t.Cleanup(func() {
		dispatcher.Close()
		_ = core.Stop()
	})
	owner := peer.Identity{UserID: "S-1-5-21-1"}
	ctx := context.Background()
	port := porttest.Free(t)
	withProxy := &controlpb.RunOptions{SystemProxyEnabled: true, MixedPort: uint32(port)}

	expectOK(t, callAs(t, dispatcher, ctx, owner, request(1, startRequest(profileYAML(port), withProxy))))
	if len(host.applied) != 1 || host.applied[0].GetConfigYaml() != profileYAML(port) || !dispatcher.Running() {
		t.Fatalf("applied %v", host.applied)
	}
	if status := callAs(t, dispatcher, ctx, owner, request(2, getStatus)).GetGetStatus(); !status.GetSystemProxyEnabled() {
		t.Fatalf("status = %v", status)
	}
	expectOK(t, callAs(t, dispatcher, ctx, owner, request(3, reloadRequest(profileYAML(port), &controlpb.RunOptions{MixedPort: uint32(port)}))))
	want := &controlpb.StartRequest{
		Source:  &controlpb.StartRequest_ConfigYaml{ConfigYaml: profileYAML(port)},
		Options: &controlpb.RunOptions{MixedPort: uint32(port)},
	}
	if len(host.applied) != 2 || !proto.Equal(host.applied[1], want) {
		t.Fatalf("reload applied %v", host.applied)
	}
	expectOK(t, callAs(t, dispatcher, ctx, owner, request(4, systemProxyRequest(true))))
	if !host.systemProxy {
		t.Fatal("the system proxy is off")
	}
	host.proxyErr = errors.New("no session")
	expectError(t, callAs(t, dispatcher, ctx, owner, request(5, systemProxyRequest(false))), 5, CodeSystemProxyFailed)
	host.proxyErr = nil

	if err := dispatcher.SuspendContext(ctx); err != nil || host.suspended != 1 || dispatcher.Running() {
		t.Fatalf("suspend: %v, %d suspended", err, host.suspended)
	}
	if err := dispatcher.ResumeContext(ctx, host.applied[0]); err != nil || !dispatcher.Running() || len(host.applied) != 3 {
		t.Fatalf("resume: %v, applied %d", err, len(host.applied))
	}
	// Resuming a running core leaves it alone.
	if err := dispatcher.ResumeContext(ctx, host.applied[0]); err != nil || len(host.applied) != 3 {
		t.Fatalf("second resume: %v, applied %d", err, len(host.applied))
	}
	expectOK(t, callAs(t, dispatcher, ctx, owner, request(6, stopRequest)))
	if host.stopped != 1 || dispatcher.Running() {
		t.Fatalf("stopped %d times", host.stopped)
	}
}

// Claim leaves the core to its owner; a take-over (decision D5b) has the host stop it, under
// the lock that Claim already holds.
func TestHostTakeOver(t *testing.T) {
	host := &fakeHost{owner: "S-1-5-21-1"}
	dispatcher := New(Options{Engine: core, HostMethods: true, Host: host})
	t.Cleanup(func() {
		dispatcher.Close()
		_ = core.Stop()
	})
	owner, admin := peer.Identity{UserID: "S-1-5-21-1"}, peer.Identity{UserID: "S-1-5-21-2", Administrator: true}
	ctx := context.Background()
	port := porttest.Free(t)
	expectOK(t, callAs(t, dispatcher, ctx, owner, request(1, startRequest(profileYAML(port), &controlpb.RunOptions{MixedPort: uint32(port)}))))

	host.claimErr = ErrOwnedByOtherUser
	expectError(t, callAs(t, dispatcher, ctx, admin, request(2, claimRequest)), 2, CodeOwnedByOtherUser)
	if len(host.tookOver) != 0 || !dispatcher.Running() {
		t.Fatalf("a claim took over for %v", host.tookOver)
	}
	host.claimErr = ErrRestarting
	expectError(t, callAs(t, dispatcher, ctx, admin, request(3, takeOverRequest)), 3, CodeServiceRestarting)
	if len(host.tookOver) != 1 || host.tookOver[0] != admin.UserID || dispatcher.Running() {
		t.Fatalf("took over for %v, running %v", host.tookOver, dispatcher.Running())
	}
	// Taking over the development server is claiming it: its user owns it already.
	expectOK(t, call(t, development(t), request(4, takeOverRequest)))
}

// The development server's only user is its owner.
func TestDevelopmentOwnership(t *testing.T) {
	info := call(t, development(t), request(1, getInfo)).GetGetInfo()
	if info.GetOwnership() != controlpb.Ownership_OWNERSHIP_CALLER {
		t.Fatalf("ownership = %v", info.GetOwnership())
	}
}

var _ server.Gate = (*Dispatcher)(nil)

// The server refuses a large request from anyone but the owner unread; the answer is the
// one any method but GetInfo and Claim gets them, under the request's own id.
func TestRefusalOfALargeRequest(t *testing.T) {
	host := &fakeHost{owner: "S-1-5-21-1"}
	d := New(Options{Engine: unlinked{}, HostMethods: true, Host: host})
	t.Cleanup(d.Close)
	if d.Refusal(peer.Identity{UserID: "S-1-5-21-1"}) != nil {
		t.Fatal("the owner's large requests are refused")
	}
	payload, err := proto.Marshal(request(1<<40, startRequest(strings.Repeat("#", 1<<20), nil)))
	if err != nil {
		t.Fatal(err)
	}
	head := payload[:64]
	for user, code := range map[string]string{"S-1-5-21-2": CodeOwnedByOtherUser, "idle": CodeNotClaimed} {
		refusal := d.Refusal(peer.Identity{UserID: user})
		if refusal == nil {
			t.Fatalf("%s may send large requests", user)
		}
		expectError(t, decode(t, refusal(head)), 1<<40, code)
	}
	// A head without the id still gets an answer, for request 0.
	expectError(t, decode(t, d.Refusal(peer.Identity{UserID: "idle"})([]byte{0xff})), 0, CodeNotClaimed)
	// Hosts without owners take requests of any size.
	if development(t).Refusal(peer.Identity{UserID: "501"}) != nil {
		t.Fatal("the development server refuses large requests")
	}
}

// The host's failures used to come back as operation_failed with English the UI could only
// show as it was. Each now has a code the UI words itself; a start whose system proxy
// failed says that the tunnel is up.
func TestHostFailuresHaveTheirOwnCodes(t *testing.T) {
	host := &fakeHost{owner: "S-1-5-21-1"}
	dispatcher := New(Options{Engine: core, HostMethods: true, Host: host})
	t.Cleanup(func() {
		dispatcher.Close()
		_ = core.Stop()
	})
	owner, admin := peer.Identity{UserID: "S-1-5-21-1"}, peer.Identity{UserID: "S-1-5-21-2", Administrator: true}
	ctx := context.Background()
	port := porttest.Free(t)
	withProxy := &controlpb.RunOptions{SystemProxyEnabled: true, MixedPort: uint32(port)}

	host.proxyErr = errors.New("set the owner's system proxy: Access is denied.")
	started := callAs(t, dispatcher, ctx, owner, request(1, startRequest(profileYAML(port), withProxy)))
	expectError(t, started, 1, CodeStartedWithoutSystemProxy)
	if !dispatcher.Running() || !started.GetError().GetRetryable() {
		t.Fatalf("running %v after %v", dispatcher.Running(), started.GetError())
	}
	reloaded := callAs(t, dispatcher, ctx, owner, request(2, reloadRequest(profileYAML(port), withProxy)))
	expectError(t, reloaded, 2, CodeStartedWithoutSystemProxy)
	expectError(t, callAs(t, dispatcher, ctx, owner, request(3, systemProxyRequest(true))), 3, CodeSystemProxyFailed)
	// A Linux desktop that keeps its proxy settings nowhere the service can set them.
	host.proxyErr = fmt.Errorf("read the owner's proxy settings: %w", sysproxy.ErrUnsupportedDesktop)
	unsupported := callAs(t, dispatcher, ctx, owner, request(3, systemProxyRequest(true)))
	expectError(t, unsupported, 3, CodeSystemProxyUnavailable)
	if unsupported.GetError().GetRetryable() {
		t.Fatal("an unsupported desktop is worth another try")
	}
	host.proxyErr = nil

	host.stopErr = errors.New("restore the owner's proxy settings: the owner is not signed in")
	expectError(t, callAs(t, dispatcher, ctx, owner, request(4, stopRequest)), 4, CodeSystemProxyFailed)
	if dispatcher.Running() {
		t.Fatal("the core runs after a stop whose proxy failed")
	}
	host.stopErr = nil

	host.claimErr = errors.New("record the owner: disk full")
	expectError(t, callAs(t, dispatcher, ctx, admin, request(5, claimRequest)), 5, CodeClaimFailed)
	expectError(t, callAs(t, dispatcher, ctx, admin, request(6, takeOverRequest)), 6, CodeTakeOverFailed)
	// A refusal stays a refusal, wrapped or not.
	host.claimErr = fmt.Errorf("taking over needs an administrator's rights: %w", ErrOwnedByOtherUser)
	expectError(t, callAs(t, dispatcher, ctx, admin, request(7, takeOverRequest)), 7, CodeOwnedByOtherUser)
}

// A port another program holds is named, for the UI to say in its own words.
func TestPortInUseIsNamed(t *testing.T) {
	dispatcher := development(t)
	taken, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer taken.Close()
	port := taken.Addr().(*net.TCPAddr).Port
	response := call(t, dispatcher, request(1, startRequest(profileYAML(port), nil)))
	expectError(t, response, 1, CodeInboundFailed)
	if got, want := response.GetError().GetMessage(), fmt.Sprintf("mixed-port %d", port); got != want || !response.GetError().GetRetryable() {
		t.Fatalf("error = %v, want message %q", response.GetError(), want)
	}
}

// providerEngine fails to update its providers.
type providerEngine struct {
	unlinked
	err error
}

func (e providerEngine) UpdateProvider(controlpb.ProviderKind, string) error { return e.err }

func TestProviderUpdateFailure(t *testing.T) {
	update := func(r *controlpb.Request) {
		r.Method = &controlpb.Request_UpdateProvider{UpdateProvider: &controlpb.UpdateProviderRequest{Name: "ads"}}
	}
	failed := call(t, New(Options{Engine: providerEngine{err: errors.New("update provider ads: EOF")}}), request(1, update))
	expectError(t, failed, 1, CodeProviderUpdateFailed)
	if !failed.GetError().GetRetryable() {
		t.Fatalf("error = %v", failed.GetError())
	}
	expectError(t, call(t, New(Options{Engine: providerEngine{err: engine.ErrNotRunning}}), request(2, update)), 2, CodeNotRunning)
}
