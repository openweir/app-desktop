//go:build cgo

package main

import (
	"context"
	"sync/atomic"
	"time"

	"github.com/openweir/app-desktop/src/core/internal/operation"
)

// A UI can disappear between bringing up NE and sending its configuration. Do not
// leave an empty TUN swallowing traffic forever. Each configuring message (session.go's
// configures) proves a configuration is still on its way, so long batches of profile
// inspection get a fresh idle budget; status polls do not, or a UI started again after
// the one that brought the tunnel up would keep it alive without ever configuring it.
// Start or detach cancels the guard permanently; running tunnels need no UI heartbeat.
func guardInitialConfiguration(limit time.Duration, running func() bool, fail func(operation.Failure)) (touch func(), stop func()) {
	var finished atomic.Bool
	started := time.Now()
	var lastContact atomic.Int64
	var timer *time.Timer
	var check func()
	check = func() {
		if finished.Load() {
			return
		}
		idle := time.Since(started) - time.Duration(lastContact.Load())
		if idle < limit {
			timer.Reset(limit - idle)
			return
		}
		if finished.CompareAndSwap(false, true) && !running() {
			fail(operation.Failure{Method: "await_initial_configuration", Cause: context.DeadlineExceeded})
		}
	}
	timer = time.AfterFunc(limit, check)
	return func() { lastContact.Store(time.Since(started).Nanoseconds()) }, func() {
		finished.Store(true)
		timer.Stop()
	}
}
