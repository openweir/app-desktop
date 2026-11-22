package main

import (
	"context"
	"errors"
	"testing"
)

// A service that restarts for a new owner exits cleanly when its helper will start it again,
// and with an error when the helper did not start: only that makes the SCM's recovery start
// it. A stop the SCM asked for is clean.
func TestRestartWithoutItsHelperExitsWithAnError(t *testing.T) {
	for _, tc := range []struct {
		name   string
		helper error
		code   uint32
	}{
		{"helper started", nil, 0},
		{"helper did not start", errors.New("Access is denied."), 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, stop := context.WithCancelCause(context.Background())
			defer stop(nil)
			restartLater(stop, func() error { return tc.helper })
			<-ctx.Done()
			err := ending(ctx, nil)
			if code := exitCode(err); code != tc.code {
				t.Fatalf("exit code %d (%v), want %d", code, err, tc.code)
			}
			if tc.helper != nil && !errors.Is(err, tc.helper) {
				t.Fatalf("serve ended with %v", err)
			}
		})
	}
	ctx, stop := context.WithCancelCause(context.Background())
	stop(nil)
	if code := exitCode(ending(ctx, nil)); code != 0 {
		t.Fatalf("exit code %d after a stop", code)
	}
	failed := errors.New("listener closed")
	if err := ending(ctx, failed); !errors.Is(err, failed) || exitCode(err) != 1 {
		t.Fatalf("serve ended with %v", err)
	}
}
