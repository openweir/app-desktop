//go:build !windows

package owner

import (
	"errors"
	"testing"

	"github.com/openweir/app-desktop/src/core/internal/control"
	"github.com/openweir/app-desktop/src/core/internal/peer"
	"github.com/openweir/app-desktop/src/core/internal/sysproxy"
)

// helper is the take-over helper that pkexec runs as root for an administrator on Linux (D66).
var helper = peer.Identity{UserID: superuser, SessionID: 0, Administrator: true}

// The helper ends the owner's claim instead of taking the tunnel over: it stops as after Stop,
// its owner gets their proxy settings back, and the service restarts with no owner, for the
// administrator's UI to claim as on a fresh install. Root never becomes the owner.
func TestRootEndsTheOwnersClaim(t *testing.T) {
	dir, m := t.TempDir(), newMachine()
	if err := SaveRecord(dir, Record{SID: alice, Session: 1000}); err != nil {
		t.Fatal(err)
	}
	theirs := sysproxy.Settings{Flags: sysproxy.Direct | sysproxy.AutoDetect}
	m.sessions[1000], m.proxies[alice] = alice, theirs
	m.sessions[1001] = bob
	service, core, home := start(t, dir, m)
	core.running = true
	if err := service.Applied(profile(true), current); err != nil {
		t.Fatal(err)
	}
	// The helper never asks the platform, whose gate is for owners: it has no session.
	m.refused = map[string]error{superuser: errors.New("root has no session")}

	if err := service.Claim(helper); err == nil || errors.Is(err, control.ErrRestarting) {
		t.Fatalf("root's claim = %v", err)
	}
	core.stopErr = errors.New("stuck")
	if err := service.TakeOver(helper, core.stop); err == nil || errors.Is(err, control.ErrRestarting) {
		t.Fatalf("an eviction that could not stop the tunnel: %v", err)
	}
	if record, ok, _ := LoadRecord(dir); !ok || record.SID != alice || m.restarts != 0 {
		t.Fatalf("record %+v after %d restarts", record, m.restarts)
	}
	core.stopErr = nil
	if err := service.TakeOver(helper, core.stop); !errors.Is(err, control.ErrRestarting) {
		t.Fatalf("eviction = %v", err)
	}
	if core.stops != 2 || core.running || m.restarts != 1 {
		t.Fatalf("%d stops, running %v, %d restarts", core.stops, core.running, m.restarts)
	}
	if m.proxies[alice] != theirs || (kept{home}).wasRunning() {
		t.Fatalf("alice's proxy %+v, was_running %v", m.proxies[alice], (kept{home}).wasRunning())
	}
	if _, ok, err := LoadRecord(dir); ok || err != nil {
		t.Fatalf("owner.json is still there (%v)", err)
	}
	if err := service.Admit(as(alice, 1000)); !errors.Is(err, control.ErrRestarting) {
		t.Fatalf("admit the former owner: %v", err)
	}
	if err := service.TakeOver(helper, core.stop); !errors.Is(err, control.ErrRestarting) || core.stops != 2 {
		t.Fatalf("a second eviction = %v after %d stops", err, core.stops)
	}
	if err := service.Claim(as(bob, 1001)); !errors.Is(err, control.ErrRestarting) || m.restarts != 1 {
		t.Fatalf("a claim while restarting = %v after %d restarts", err, m.restarts)
	}
	service.Close()

	// The new process has no owner; the administrator's UI claims as anyone may.
	service, core, home = start(t, dir, m)
	if home != "" {
		t.Fatalf("home = %q", home)
	}
	if err := service.Claim(as(bob, 1001)); !errors.Is(err, control.ErrRestarting) || m.restarts != 2 {
		t.Fatalf("bob's claim = %v after %d restarts", err, m.restarts)
	}
	if record, _, _ := LoadRecord(dir); record != (Record{SID: bob, Session: 1001}) {
		t.Fatalf("record = %+v", record)
	}
}

// With no tunnel up there is nothing to take over: the helper changes nothing, and the
// administrator's UI claims the stopped tunnel as anyone may.
func TestRootLeavesAStoppedTunnelAlone(t *testing.T) {
	dir, m := t.TempDir(), newMachine()
	service, core, _ := start(t, dir, m)
	if err := service.TakeOver(helper, core.stop); err != nil || core.stops != 0 || m.restarts != 0 {
		t.Fatalf("eviction with no owner = %v after %d stops and %d restarts", err, core.stops, m.restarts)
	}
	if err := SaveRecord(dir, Record{SID: alice, Session: 1000}); err != nil {
		t.Fatal(err)
	}
	m.sessions[1000] = alice
	service, core, _ = start(t, dir, m)
	if err := service.TakeOver(helper, core.stop); err != nil || core.stops != 0 || m.restarts != 0 {
		t.Fatalf("eviction of a stopped tunnel = %v after %d stops and %d restarts", err, core.stops, m.restarts)
	}
	if record, ok, _ := LoadRecord(dir); !ok || record.SID != alice {
		t.Fatalf("record = %+v", record)
	}
	// Someone with an administrator's rights but not root takes over as on Windows.
	if evicts(elevated(bob, 1001)) || evicts(as(superuser, 0)) {
		t.Fatal("evicts a caller other than the helper")
	}
}
