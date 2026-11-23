//go:build !linux

package logind

import (
	"context"
	"errors"
)

// Watch is Linux's; elsewhere it reports errors.ErrUnsupported.
func Watch(ctx context.Context, onUser func(uid uint32, signedIn bool), onSleep func(sleeping bool)) (Inhibitor, error) {
	return nil, errors.ErrUnsupported
}
