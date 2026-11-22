//go:build cgo

package main

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"sync"
	"time"

	"github.com/openweir/subconv/rules"

	"github.com/openweir/app-desktop/src/core/internal/admission"
	"github.com/openweir/app-desktop/src/core/internal/control"
	"github.com/openweir/app-desktop/src/core/internal/controlpb"
	"github.com/openweir/app-desktop/src/core/internal/engine"
	"github.com/openweir/app-desktop/src/core/internal/geodata"
	"github.com/openweir/app-desktop/src/core/internal/operation"
	"github.com/openweir/app-desktop/src/core/internal/peer"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// session is one attached tunnel: an engine in fd mode and its dispatcher.
type session struct {
	engine     *engine.Engine
	dispatcher *control.Dispatcher
	// ctx ends the requests in flight and the subscriptions when the tunnel detaches.
	ctx        context.Context
	cancel     context.CancelFunc
	configured func()
	// contact refreshes the initial configuration guard; handle calls it for the
	// configuring methods only.
	contact func()
}

var (
	// lifecycle serializes attach and detach.
	lifecycle sync.Mutex
	// mu guards current. handle holds the read lock while it dispatches, so that detach
	// waits for the requests in flight.
	mu      sync.RWMutex
	current *session
)

// attach seeds home with the GEO data in resources, unless resources is empty, then opens
// the engine and the dispatcher on the descriptor. includeAllNetworks is the tunnel's.
func attach(fd int, home, resources string, includeAllNetworks bool) error {
	defer operation.Guard("attach_tunnel", 50*time.Second, operation.Terminate)()
	lifecycle.Lock()
	defer lifecycle.Unlock()
	mu.RLock()
	attached := current != nil
	mu.RUnlock()
	if attached {
		return errors.New("a tunnel is already attached")
	}
	if resources != "" {
		if err := geodata.Seed(resources, home); err != nil {
			return err
		}
	}
	core, err := engine.New(engine.Options{
		HomeDir:            home,
		Tun:                admission.TunFD,
		TunFD:              fd,
		IncludeAllNetworks: includeAllNetworks,
		TemplateHosts:      rules.DefaultHosts(),
	})
	if err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(context.Background())
	opened := &session{
		engine:     core,
		dispatcher: control.New(control.Options{Engine: core, OnUnresponsive: operation.Terminate}),
		ctx:        ctx,
		cancel:     cancel,
	}
	opened.contact, opened.configured = guardInitialConfiguration(2*time.Minute, func() bool {
		return core.State() == controlpb.RunState_RUN_STATE_RUNNING
	}, operation.Terminate)
	mu.Lock()
	current = opened
	mu.Unlock()
	return nil
}

func detach() {
	guard := operation.Guard("detach_tunnel", detachLimit, operation.Terminate)
	defer func() { guard() }()
	lifecycle.Lock()
	defer lifecycle.Unlock()
	mu.RLock()
	detaching := current
	mu.RUnlock()
	if detaching == nil {
		return
	}
	detaching.cancel()
	detaching.configured()
	mu.Lock()
	current = nil
	mu.Unlock()
	detaching.dispatcher.Close()
	// Work already in the core keeps the engine's lifecycle lock for as long as its deadline
	// allows, 50 s for a Start: longer than detachLimit, which would replace the extension
	// for waiting on it. That deadline and the dispatcher's watchdog bound the wait; the
	// limit is for detaching itself.
	guard()
	detaching.dispatcher.Wait()
	guard = operation.Guard("detach_tunnel", detachLimit, operation.Terminate)
	// Close stops the core; mihomo closes the duplicate its TUN runs on.
	_ = detaching.engine.Close()
}

// detachLimit bounds detaching the tunnel, apart from waiting for the work in flight.
const detachLimit = 20 * time.Second

// handle answers one Request with the attached tunnel's dispatcher. The system restricts
// provider messages to this app, so there is no peer identity.
func handle(request []byte) []byte {
	mu.RLock()
	defer mu.RUnlock()
	if current == nil {
		return control.EncodeFailure(0, control.CodeNotRunning, "the extension has no tunnel attached")
	}
	if configures(request) {
		current.contact()
	}
	response := current.dispatcher.Handle(current.ctx, peer.Identity{}, request)
	if current.engine.State() == controlpb.RunState_RUN_STATE_RUNNING {
		current.configured()
	}
	return response
}

// configuring holds the Request fields of the methods that work toward the first
// configuration: reading the profiles stored while no core could (D27), validating, and
// Start itself. Status reads, polls and the other queries are not among them: a UI that
// came back after its predecessor brought the tunnel up polls the status every second,
// which says nothing about a configuration on its way.
var configuring = requestFields("inspect_source", "validate", "start")

func requestFields(names ...protoreflect.Name) map[protowire.Number]bool {
	fields := (&controlpb.Request{}).ProtoReflect().Descriptor().Fields()
	numbers := make(map[protowire.Number]bool, len(names))
	for _, name := range names {
		numbers[fields.ByName(name).Number()] = true
	}
	return numbers
}

// configures reports whether request calls one of the configuring methods. It reads the
// top-level tags only, rather than decode a configuration that may be megabytes long; the
// dispatcher still decodes and checks the whole request.
func configures(request []byte) bool {
	for len(request) > 0 {
		number, kind, n := protowire.ConsumeTag(request)
		if n < 0 {
			return false
		}
		if configuring[number] {
			return true
		}
		request = request[n:]
		n = protowire.ConsumeFieldValue(number, kind, request)
		if n < 0 {
			return false
		}
		request = request[n:]
	}
	return false
}

// network is admission.FDNetwork in the shape the extension's network settings take.
type network struct {
	IPv4Address      string `json:"ipv4_address"`
	IPv4SubnetMask   string `json:"ipv4_subnet_mask"`
	IPv6Address      string `json:"ipv6_address"`
	IPv6PrefixLength int    `json:"ipv6_prefix_length"`
	DNSServer        string `json:"dns_server"`
	MTU              uint32 `json:"mtu"`
}

func networkJSON() []byte {
	fd := admission.FDNetwork
	data, err := json.Marshal(network{
		IPv4Address:      fd.Inet4.Addr().String(),
		IPv4SubnetMask:   net.IP(net.CIDRMask(fd.Inet4.Bits(), 32)).String(),
		IPv6Address:      fd.Inet6.Addr().String(),
		IPv6PrefixLength: fd.Inet6.Bits(),
		DNSServer:        fd.DNSServer().String(),
		MTU:              fd.MTU,
	})
	if err != nil {
		panic(err)
	}
	return data
}
