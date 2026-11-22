package control

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"

	"github.com/openweir/app-desktop/src/core/internal/admission"
	"github.com/openweir/app-desktop/src/core/internal/controlpb"
	"github.com/openweir/app-desktop/src/core/internal/operation"
	"github.com/openweir/app-desktop/src/core/internal/peer"
)

type blockedCore struct {
	unlinked
	entered, release chan struct{}
	enter            sync.Once
	starts, stops    atomic.Int32
}

func (b *blockedCore) Start(admission.Profile) (*controlpb.CompositionReport, error) {
	b.starts.Add(1)
	b.enter.Do(func() { close(b.entered) })
	<-b.release
	return nil, nil
}

func (b *blockedCore) Stop() error { b.stops.Add(1); return nil }

type deadlineHost struct {
	fakeHost
	writes atomic.Int32
}

func (h *deadlineHost) Applied(*controlpb.StartRequest, func() bool) error {
	h.writes.Add(1)
	return nil
}

// lifecycleCore holds one lock across Start, as the engine holds its lifecycle lock, and
// ListGeoCategories takes it too.
type lifecycleCore struct {
	unlinked
	lifecycle        sync.Mutex
	entered, release chan struct{}
}

func (c *lifecycleCore) Start(admission.Profile) (*controlpb.CompositionReport, error) {
	c.lifecycle.Lock()
	defer c.lifecycle.Unlock()
	close(c.entered)
	<-c.release
	return nil, nil
}

func (c *lifecycleCore) ListGeoCategories() (*controlpb.ListGeoCategoriesResponse, error) {
	c.lifecycle.Lock()
	defer c.lifecycle.Unlock()
	return &controlpb.ListGeoCategoriesResponse{}, nil
}

func listGeoCategories(r *controlpb.Request) {
	r.Method = &controlpb.Request_ListGeoCategories{ListGeoCategories: &controlpb.ListGeoCategoriesRequest{}}
}

// The rule editor lists the GEO categories while a slow Start holds the lifecycle lock. The
// read waits for its turn and times out there; it never outlives its deadline inside the
// engine, where the watchdog would take it for a hung core.
func TestGeoCategoriesWaitBehindASlowStart(t *testing.T) {
	slow := &lifecycleCore{entered: make(chan struct{}), release: make(chan struct{})}
	d := New(Options{Engine: slow})
	defer d.Close()
	payload, _ := proto.Marshal(request(1, startRequest("proxies: []", nil)))
	first := make(chan []byte, 1)
	go func() { first <- d.Handle(context.Background(), peer.Identity{}, payload) }()
	<-slow.entered
	r := request(2, listGeoCategories)
	r.TimeoutMs = 1500 // 1.2 s for the core, then 0.3 s of grace for a read
	expectError(t, call(t, d, r), 2, CodeTimeout)
	time.Sleep(500 * time.Millisecond)
	if d.unresponsive.Load() {
		t.Fatal("a read waiting for the lifecycle lock was taken for a hung core")
	}
	close(slow.release)
	expectOK(t, decode(t, <-first))
	if call(t, d, request(3, listGeoCategories)).GetListGeoCategories() == nil {
		t.Fatal("the categories were not listed once the Start finished")
	}
}

func TestExpiredRequestCannotStart(t *testing.T) {
	blocked := &blockedCore{entered: make(chan struct{}), release: make(chan struct{})}
	d := New(Options{Engine: blocked})
	defer d.Close()
	r := request(1, startRequest("proxies: []", nil))
	r.DeadlineUnixMs = time.Now().Add(-time.Second).UnixMilli()
	expectError(t, call(t, d, r), 1, CodeTimeout)
	if blocked.starts.Load() != 0 || d.unresponsive.Load() {
		t.Fatal("an expired request entered the engine or poisoned the host")
	}
}

func TestStopIntentSurvivesItsCallerTimeout(t *testing.T) {
	blocked := &blockedCore{entered: make(chan struct{}), release: make(chan struct{})}
	d := New(Options{Engine: blocked})
	defer d.Close()
	payload, _ := proto.Marshal(request(1, startRequest("proxies: []", nil)))
	first := make(chan []byte, 1)
	go func() { first <- d.Handle(context.Background(), peer.Identity{}, payload) }()
	<-blocked.entered
	// Long enough to keep the intent, too short to wait out the Start.
	ctx, cancel := context.WithTimeout(context.Background(), 1500*time.Millisecond)
	defer cancel()
	expectError(t, callAs(t, d, ctx, peer.Identity{}, request(2, stopRequest)), 2, CodeTimeout)
	if blocked.stops.Load() != 0 || d.unresponsive.Load() {
		t.Fatal("a cancelled queue entry ran or interrupted the preceding transaction")
	}
	close(blocked.release)
	expectError(t, decode(t, <-first), 1, CodeCancelled)
	if blocked.stops.Load() != 1 {
		t.Fatal("the late Start did not honor the owner's Stop intent")
	}
	expectOK(t, call(t, d, request(3, stopRequest)))
	if blocked.stops.Load() != 2 {
		t.Fatal("an explicit later Stop did not run")
	}
}

// The UI may send a request with a millisecond left. A mutation that short of time does not
// start, which would only run out its deadline in the engine and take the host down with it;
// nor does a Stop keep an intent it has no time to act on.
func TestMutationWithoutTimeToRunNeverStarts(t *testing.T) {
	blocked := &blockedCore{entered: make(chan struct{}), release: make(chan struct{})}
	d := New(Options{Engine: blocked})
	defer d.Close()
	for id, method := range []func(*controlpb.Request){startRequest("proxies: []", nil), stopRequest} {
		for _, milliseconds := range []uint32{1, 1200} {
			r := request(uint64(id+1), method)
			r.TimeoutMs = milliseconds
			expectError(t, call(t, d, r), uint64(id+1), CodeTimeout)
		}
	}
	if blocked.starts.Load() != 0 || blocked.stops.Load() != 0 || d.stopGeneration.Load() != 0 || d.unresponsive.Load() {
		t.Fatal("a mutation without the time to finish started")
	}
}

// applyWithin lets Start and Reload enter the core with as little as least left, so that a
// test need not wait out the realistic minimum.
func applyWithin(t *testing.T, least time.Duration) {
	t.Helper()
	saved := minimumApply
	minimumApply = least
	t.Cleanup(func() { minimumApply = saved })
}

// Waiting for its turn can use up most of a mutation's time, too.
func TestMutationThatWaitedTooLongNeverStarts(t *testing.T) {
	applyWithin(t, minimumWork)
	blocked := &blockedCore{entered: make(chan struct{}), release: make(chan struct{})}
	d := New(Options{Engine: blocked})
	defer d.Close()
	payload, _ := proto.Marshal(request(1, startRequest("proxies: []", nil)))
	first := make(chan []byte, 1)
	go func() { first <- d.Handle(context.Background(), peer.Identity{}, payload) }()
	<-blocked.entered
	second := request(2, startRequest("proxies: []", nil))
	second.TimeoutMs = 2500 // 2 s for the core, of which the first Start takes 1.3
	late := make(chan *controlpb.Response, 1)
	go func() { late <- call(t, d, second) }()
	time.Sleep(1300 * time.Millisecond)
	close(blocked.release)
	expectOK(t, decode(t, <-first))
	expectError(t, <-late, 2, CodeTimeout)
	if blocked.starts.Load() != 1 || d.unresponsive.Load() {
		t.Fatalf("starts=%d unresponsive=%v", blocked.starts.Load(), d.unresponsive.Load())
	}
}

// The service's own operations give up on their context only while they wait for their turn.
func TestCancelledLifecycleOperationIsWithdrawnWhileQueued(t *testing.T) {
	blocked := &blockedCore{entered: make(chan struct{}), release: make(chan struct{})}
	d := New(Options{Engine: blocked, Host: &fakeHost{}})
	defer d.Close()
	payload, _ := proto.Marshal(request(1, startRequest("proxies: []", nil)))
	first := make(chan []byte, 1)
	go func() { first <- d.Handle(context.Background(), peer.Identity{}, payload) }()
	<-blocked.entered
	ctx, cancel := context.WithCancel(context.Background())
	suspended := make(chan error, 1)
	go func() { suspended <- d.SuspendContext(ctx) }()
	cancel()
	select {
	case err := <-suspended:
		if !errors.Is(err, context.Canceled) {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("a cancelled suspend kept waiting for its turn")
	}
	close(blocked.release)
	expectOK(t, decode(t, <-first))
	if blocked.stops.Load() != 0 {
		t.Fatal("a withdrawn suspend stopped the core")
	}
}

func TestClosingDispatcherCancelsQueuedRequests(t *testing.T) {
	blocked := &blockedCore{entered: make(chan struct{}), release: make(chan struct{})}
	d := New(Options{Engine: blocked})
	first, second := make(chan []byte, 1), make(chan []byte, 1)
	payload, _ := proto.Marshal(request(1, startRequest("proxies: []", nil)))
	go func() { first <- d.Handle(context.Background(), peer.Identity{}, payload) }()
	<-blocked.entered
	other, _ := proto.Marshal(request(2, startRequest("proxies: []", nil)))
	go func() { second <- d.Handle(context.Background(), peer.Identity{}, other) }()
	d.Close()
	select {
	case response := <-second:
		expectError(t, decode(t, response), 2, CodeCancelled)
	case <-time.After(time.Second):
		t.Fatal("closing did not cancel queued work")
	}
	close(blocked.release)
	<-first
	if blocked.starts.Load() != 1 {
		t.Fatal("another start ran while closing")
	}
}

func TestHungStartCannotPersistLateOrAdmitAnotherMutation(t *testing.T) {
	applyWithin(t, minimumWork)
	blocked := &blockedCore{entered: make(chan struct{}), release: make(chan struct{})}
	host := &deadlineHost{}
	hung := make(chan operation.Failure, 1)
	d := New(Options{Engine: blocked, Host: host, OnUnresponsive: func(f operation.Failure) { hung <- f }})
	defer d.Close()
	r := request(1, startRequest("proxies: []", nil))
	r.TimeoutMs = 1500 // the core gets 1.2 s, enough for a mutation to start
	expectError(t, call(t, d, r), 1, CodeTimeout)
	select {
	case <-hung:
	case <-time.After(time.Second):
		t.Fatal("the host recovery was not requested")
	}
	expectError(t, call(t, d, request(2, stopRequest)), 2, CodeUnresponsive)
	close(blocked.release)
	// The worker can return late, but it must never write a successful running profile.
	time.Sleep(30 * time.Millisecond)
	if host.writes.Load() != 0 || blocked.stops.Load() != 0 {
		t.Fatalf("late mutation: persisted=%d stops=%d", host.writes.Load(), blocked.stops.Load())
	}
}

func TestPoisonedRuntimeDoesNotRunLateRollbackOrPersistence(t *testing.T) {
	blocked, host := &blockedCore{}, &deadlineHost{}
	d := New(Options{Engine: blocked, Host: host})
	defer d.Close()
	ctx := context.WithValue(context.Background(), intentKey{}, requestIntent{generation: 0})
	d.stopGeneration.Store(1) // a later Stop would normally require rolling Start back
	d.unresponsive.Store(true)
	if err := d.commitApplied(ctx, &controlpb.StartRequest{}); !errors.Is(err, operation.ErrUnresponsive) {
		t.Fatal(err)
	}
	if blocked.stops.Load() != 0 || host.writes.Load() != 0 {
		t.Fatal("poisoned runtime was reused")
	}
}

func TestRequestDeadlineDoesNotExpireItsSubscription(t *testing.T) {
	d := New(Options{Engine: &sampler{}})
	defer d.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	r := request(1, func(r *controlpb.Request) {
		r.Method = &controlpb.Request_SubscribeTraffic{SubscribeTraffic: &controlpb.SubscribeTrafficRequest{}}
	})
	r.TimeoutMs = 40
	id := callAs(t, d, ctx, peer.Identity{}, r).GetSubscribe().GetSubscriptionId()
	if id == 0 {
		t.Fatal("subscription not created")
	}
	time.Sleep(60 * time.Millisecond)
	response := call(t, d, request(2, func(r *controlpb.Request) {
		r.Method = &controlpb.Request_PollEvents{PollEvents: &controlpb.PollEventsRequest{SubscriptionId: id}}
	}))
	if response.GetPollEvents() == nil {
		t.Fatalf("subscription died with its request: %v", response)
	}
}

// slowParser takes 1.3 s to read a subscription, as a large one may, and counts the
// serialized work running at once.
type slowParser struct {
	unlinked
	running, most, parsed atomic.Int32
}

func (p *slowParser) enter() func() {
	if now := p.running.Add(1); now > p.most.Load() {
		p.most.Store(now)
	}
	return func() { p.running.Add(-1) }
}

func (p *slowParser) InspectSource([]byte, string) (*controlpb.InspectSourceResponse, error) {
	defer p.enter()()
	time.Sleep(1300 * time.Millisecond)
	p.parsed.Add(1)
	return &controlpb.InspectSourceResponse{}, nil
}

func (p *slowParser) SelectProxy(string, string) error {
	defer p.enter()()
	return nil
}

func inspectSource(r *controlpb.Request) {
	r.Method = &controlpb.Request_InspectSource{InspectSource: &controlpb.InspectSourceRequest{Yaml: "proxies: []"}}
}

func selectProxy(r *controlpb.Request) {
	r.Method = &controlpb.Request_SelectProxy{SelectProxy: &controlpb.SelectProxyRequest{Group: "g", Proxy: "p"}}
}

// A read that finishes after its caller's deadline used to poison the dispatcher, and the
// host replaced itself, tunnel and all: the service, or the macOS extension. Its answer is
// dropped instead, and it keeps the operation lock until it returns.
func TestLateReadIsDroppedWithoutReplacingTheHost(t *testing.T) {
	parser := &slowParser{}
	hung := make(chan operation.Failure, 1)
	d := New(Options{Engine: parser, OnUnresponsive: func(f operation.Failure) { hung <- f }})
	defer d.Close()
	r := request(9, inspectSource)
	r.TimeoutMs = 1400 // 1.12 s for the core: the parse returns 0.18 s late
	expectError(t, call(t, d, r), 9, CodeTimeout)
	// A mutation waits for the late parse rather than run beside it.
	expectOK(t, call(t, d, request(10, selectProxy)))
	if parser.parsed.Load() != 1 || parser.most.Load() != 1 {
		t.Fatalf("parsed %d, ran %d at once", parser.parsed.Load(), parser.most.Load())
	}
	select {
	case failure := <-hung:
		t.Fatalf("the host was replaced for %s", failure.Method)
	case <-time.After(300 * time.Millisecond):
	}
	if d.unresponsive.Load() || call(t, d, request(11, getInfo)).GetGetInfo() == nil {
		t.Fatal("the dispatcher stopped answering after a late read")
	}
}

// What counts as hung depends on the method, not on how little time its caller gave it.
func TestSchedule(t *testing.T) {
	d := New(Options{Engine: unlinked{}})
	defer d.Close()
	upload := func(r *controlpb.Request) {
		r.Method = &controlpb.Request_UploadGeo{UploadGeo: &controlpb.UploadGeoRequest{Complete: true}}
	}
	listGroups := func(r *controlpb.Request) {
		r.Method = &controlpb.Request_ListGroups{ListGroups: &controlpb.ListGroupsRequest{}}
	}
	for _, test := range []struct {
		name          string
		method        func(*controlpb.Request)
		budget        time.Duration
		reads         bool
		grace, least  time.Duration
		watchdogAfter time.Duration
	}{
		{"short inspect", inspectSource, 1400 * time.Millisecond, false, 55*time.Second - 1120*time.Millisecond, time.Second, 55 * time.Second},
		{"full inspect", inspectSource, 55 * time.Second, false, 5 * time.Second, time.Second, 55 * time.Second},
		{"GEO upload", upload, 13 * time.Second, false, 55*time.Second - 10400*time.Millisecond, time.Second, 55 * time.Second},
		{"GEO categories", listGeoCategories, 2 * time.Second, false, 12*time.Second - 1600*time.Millisecond, time.Second, 12 * time.Second},
		{"start", startRequest("proxies: []", nil), 55 * time.Second, false, 0, 20 * time.Second, 50 * time.Second},
		{"reload", reloadRequest("proxies: []", nil), 30 * time.Second, false, 0, 20 * time.Second, 25 * time.Second},
		{"stop", stopRequest, 25 * time.Second, false, 0, time.Second, 20 * time.Second},
		{"select", selectProxy, 12 * time.Second, false, 0, time.Second, 9600 * time.Millisecond},
		{"list groups", listGroups, 12 * time.Second, true, 2400 * time.Millisecond, 0, 12 * time.Second},
	} {
		executor, margin, grace, least := d.schedule(request(1, test.method), test.budget)
		if (executor == d.reads) != test.reads || grace != test.grace || least != test.least {
			t.Errorf("%s: reads %v, grace %v, least %v", test.name, executor == d.reads, grace, least)
		}
		if after := test.budget - margin + grace; after != test.watchdogAfter {
			t.Errorf("%s: the watchdog fires after %v, want %v", test.name, after, test.watchdogAfter)
		}
	}
	if hostBudget("upload_geo") != hostBudget("validate") {
		t.Error("the last GEO chunk has an ordinary read's budget")
	}
}

// Start and Reload apply a whole profile. With seconds left they never enter the core,
// where running out the deadline would replace the host.
func TestApplyNeedsARealisticBudget(t *testing.T) {
	blocked := &blockedCore{entered: make(chan struct{}), release: make(chan struct{})}
	close(blocked.release)
	d := New(Options{Engine: blocked})
	defer d.Close()
	for id, method := range []func(*controlpb.Request){startRequest("proxies: []", nil), reloadRequest("proxies: []", nil)} {
		r := request(uint64(id+1), method)
		r.TimeoutMs = 15_000
		expectError(t, call(t, d, r), uint64(id+1), CodeTimeout)
	}
	if blocked.starts.Load() != 0 || d.unresponsive.Load() {
		t.Fatal("a start with 12 s left entered the core")
	}
	r := request(3, startRequest("proxies: []", nil))
	r.TimeoutMs = 26_000 // 20.8 s for the core
	expectOK(t, call(t, d, r))
	if blocked.starts.Load() != 1 {
		t.Fatal("a start with time enough did not run")
	}
}

// Detaching the macOS tunnel closes the engine, which waits for the lifecycle lock that a
// Start in the core holds. Wait lets the host wait for that work without timing it.
func TestWaitReturnsOnceTheWorkInTheCoreDid(t *testing.T) {
	blocked := &blockedCore{entered: make(chan struct{}), release: make(chan struct{})}
	d := New(Options{Engine: blocked})
	first := make(chan []byte, 1)
	payload, _ := proto.Marshal(request(1, startRequest("proxies: []", nil)))
	go func() { first <- d.Handle(context.Background(), peer.Identity{}, payload) }()
	<-blocked.entered
	d.Close()
	waited := make(chan struct{})
	go func() {
		d.Wait()
		close(waited)
	}()
	select {
	case <-waited:
		t.Fatal("Wait returned while the Start was in the core")
	case <-time.After(100 * time.Millisecond):
	}
	close(blocked.release)
	select {
	case <-waited:
	case <-time.After(time.Second):
		t.Fatal("Wait did not return after the Start")
	}
	<-first
}
