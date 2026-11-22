package control

import (
	"context"
	"sync"
	"time"

	"github.com/openweir/app-desktop/src/core/internal/controlpb"
	"github.com/openweir/app-desktop/src/core/internal/operation"
	"github.com/openweir/app-desktop/src/core/internal/peer"
)

const (
	CodeTimeout      = "timeout"
	CodeCancelled    = "cancelled"
	CodeUnresponsive = "core_unresponsive"
)

type connectionKey struct{}

// minimumWork is the least time serialized work needs left as it starts. The watchdog takes
// a mutation that outlives its deadline for a hung core, and the UI may pass a deadline
// only a millisecond away, so work with less time left than this never starts and times
// out instead.
const minimumWork = time.Second

// minimumApply is what Start and Reload, and the service's resume, need left as they enter
// the core: applying a profile opens the ports, the TUN and the DNS server, and loads
// providers, which may download. One that starts with seconds left would only run out its
// deadline inside mihomo, and the watchdog would replace the host, taking the tunnel down
// with it. Tests shorten it.
var minimumApply = 20 * time.Second

// minimum is the least time method needs left to start.
func minimum(method string) time.Duration {
	switch method {
	case "start", "reload":
		return minimumApply
	}
	return minimumWork
}

// tooLate says whether serialized work should no longer start with the time it has left.
func tooLate(work context.Context, least time.Duration) bool {
	deadline, ok := work.Deadline()
	return ok && time.Until(deadline) < least
}

// hostBudget is bounded independently of anything the caller asks for. The core gets a
// smaller budget, and the UI leaves another margin around this host budget. The last chunk
// of a GEO upload reads, hashes and parses up to 256 MiB, as long as a large configuration
// takes to admit.
func hostBudget(method string) time.Duration {
	switch method {
	case "validate", "start", "reload", "inspect_source", "preview_group", "match_rule", "convert_to_plan", "export_plan",
		"upload_geo":
		return 55 * time.Second
	case "stop", "claim", "set_system_proxy", "login_mesh":
		return 25 * time.Second
	case "health_check", "test_delay", "update_provider", "register_warp":
		return 110 * time.Second
	case "list_vpn_gate":
		return 70 * time.Second
	default:
		return 12 * time.Second
	}
}

func requestBudget(request *controlpb.Request) time.Duration {
	budget := hostBudget(methodName(request))
	if milliseconds := request.GetTimeoutMs(); milliseconds != 0 {
		budget = min(budget, time.Duration(milliseconds)*time.Millisecond)
	}
	if deadline := request.GetDeadlineUnixMs(); deadline != 0 {
		budget = min(budget, time.Until(time.UnixMilli(deadline)))
	}
	return budget
}

// serialized requests take the operation lock. Besides the mutations, that is every method
// which takes the engine's lifecycle lock: a read waiting behind a 50-second Start would
// outlive its own deadline inside the engine, and the watchdog would take it for a hung core.
func serialized(request *controlpb.Request) bool {
	switch request.Method.(type) {
	case *controlpb.Request_Claim, *controlpb.Request_Validate, *controlpb.Request_Start,
		*controlpb.Request_UploadGeo, *controlpb.Request_GetGeoUpdate, *controlpb.Request_ListGeoCategories,
		*controlpb.Request_Reload, *controlpb.Request_Stop, *controlpb.Request_SetSystemProxy,
		*controlpb.Request_SetMode, *controlpb.Request_SelectProxy, *controlpb.Request_UpdateProvider,
		*controlpb.Request_CloseConnection, *controlpb.Request_CloseAllConnections,
		*controlpb.Request_FlushFakeIp, *controlpb.Request_FlushDnsCache,
		*controlpb.Request_InspectSource, *controlpb.Request_PreviewGroup,
		*controlpb.Request_MatchRule, *controlpb.Request_ConvertToPlan, *controlpb.Request_ExportPlan:
		return true
	default:
		return false
	}
}

// discardable serialized requests take the operation lock, but nothing they do is left half
// done when they return after their caller gave up: the checks and previews that run
// mihomo's parser, the GEO reads that wait for the engine's lifecycle lock, and a GEO
// upload, which persists nothing once its deadline passed. One that runs late keeps the
// lock until it returns and its answer is dropped. The watchdog takes it for a hung core
// only once it outlived its method's host budget, whatever shorter deadline the caller
// gave it: a parse does not speed up for a caller in a hurry, and replacing the host
// would take the tunnel down for a read.
func discardable(request *controlpb.Request) bool {
	switch request.Method.(type) {
	case *controlpb.Request_Validate, *controlpb.Request_InspectSource, *controlpb.Request_PreviewGroup,
		*controlpb.Request_MatchRule, *controlpb.Request_ConvertToPlan, *controlpb.Request_ExportPlan,
		*controlpb.Request_ListGeoCategories, *controlpb.Request_GetGeoUpdate, *controlpb.Request_UploadGeo:
		return true
	default:
		return false
	}
}

func (d *Dispatcher) fail(failure operation.Failure) {
	d.unresponsive.Store(true)
	d.failOnce.Do(func() {
		if d.onUnresponsive != nil {
			go d.onUnresponsive(failure)
		}
	})
}

// schedule is how a request with budget runs: the executor it waits for, the margin kept
// from its budget for the answer, how long past its deadline the work may run before the
// watchdog takes it for a hung core, and the least time it needs left to start at all.
func (d *Dispatcher) schedule(request *controlpb.Request, budget time.Duration) (executor *operation.Executor, margin, grace, least time.Duration) {
	margin = min(5*time.Second, budget/5)
	if !serialized(request) {
		return d.reads, margin, margin, 0
	}
	method := methodName(request)
	if discardable(request) {
		grace = max(margin, hostBudget(method)-(budget-margin))
	}
	return d.operations, margin, grace, minimum(method)
}

func (d *Dispatcher) execute(callerCtx context.Context, caller peer.Identity, request *controlpb.Request) *controlpb.Response {
	_, stopping := request.Method.(*controlpb.Request_Stop)
	if d.unresponsive.Load() && !stopping {
		return engineFailure(operation.ErrUnresponsive)
	}
	method := methodName(request)
	budget := requestBudget(request)
	executor, margin, grace, least := d.schedule(request, budget)
	// Serialized work without the time to run is refused outright, so a Stop that could not
	// stop the core does not keep its intent either.
	if budget <= 0 || budget-margin < least {
		return engineFailure(context.DeadlineExceeded)
	}
	// Keep the connection's lifetime for subscriptions. The operation deadline must not
	// destroy a successfully created subscription when its request returns.
	ctx, cancel := context.WithTimeout(callerCtx, budget)
	defer cancel()
	detach := context.AfterFunc(d.lifetime, cancel)
	defer detach()
	if d.lifetime.Err() != nil {
		return engineFailure(context.Canceled)
	}
	intent := requestIntent{generation: d.stopGeneration.Load()}
	if stopping {
		var err error
		intent, err = d.prepareStop(ctx, caller)
		if err != nil {
			return hostFailure(err)
		}
	}
	ctx = context.WithValue(ctx, connectionKey{}, callerCtx)
	ctx = context.WithValue(ctx, intentKey{}, intent)
	response, err := operation.Run(executor, ctx, method, budget-margin, grace,
		func(work context.Context) (*controlpb.Response, error) {
			if d.unresponsive.Load() {
				return nil, operation.ErrUnresponsive
			}
			// Waiting for its turn may have used up serialized work's time.
			if least != 0 && tooLate(work, least) {
				return nil, context.DeadlineExceeded
			}
			return d.dispatch(work, caller, request), nil
		})
	if err != nil {
		return engineFailure(err)
	}
	return response
}

func connectionContext(ctx context.Context) context.Context {
	if connection, ok := ctx.Value(connectionKey{}).(context.Context); ok {
		return connection
	}
	return ctx
}

// runLifecycle is the same admission and watchdog for service-initiated operations:
// sign-in, sign-out, sleep and wake must serialize with requests from the UI too.
// Cancelling ctx, or closing the dispatcher, withdraws an operation still waiting for its
// turn. One that has started is waited for, so the host learns how it ended: an automatic
// resume keeps its crash-loop guard until its Start is over, not just abandoned.
func (d *Dispatcher) runLifecycle(ctx context.Context, method string, run func(context.Context) error) error {
	if d.unresponsive.Load() {
		return operation.ErrUnresponsive
	}
	budget := hostBudget(method)
	queue, withdraw := context.WithCancel(context.WithoutCancel(ctx))
	defer withdraw()
	var mu sync.Mutex
	started := false
	cancel := func() {
		mu.Lock()
		defer mu.Unlock()
		if !started {
			withdraw()
		}
	}
	detach := context.AfterFunc(ctx, cancel)
	defer detach()
	detachLifetime := context.AfterFunc(d.lifetime, cancel)
	defer detachLifetime()
	if ctx.Err() != nil || d.lifetime.Err() != nil {
		return context.Canceled
	}
	queue = context.WithValue(queue, intentKey{}, requestIntent{generation: d.stopGeneration.Load()})
	_, err := operation.Run(d.operations, queue, method, budget-min(5*time.Second, budget/5), 0,
		func(work context.Context) (struct{}, error) {
			mu.Lock()
			started = queue.Err() == nil
			mu.Unlock()
			switch {
			case !started:
				return struct{}{}, context.Canceled
			case d.unresponsive.Load():
				return struct{}{}, operation.ErrUnresponsive
			case tooLate(work, minimum(method)):
				return struct{}{}, context.DeadlineExceeded
			}
			return struct{}{}, run(work)
		})
	return err
}
