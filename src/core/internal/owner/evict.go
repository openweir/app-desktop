package owner

import (
	"errors"
	"fmt"

	"github.com/openweir/app-desktop/src/core/internal/peer"
)

// evict is to be TakeOver's way for root on Linux (docs/architecture.md "Linux (in
// progress)", D66). There the administrator confirms through pkexec, and the helper the UI
// runs that way connects as root (caller.Administrator, UserID "0"), not as the user who
// asked; root must not become the owner, whose home mihomo runs in. So evict ends the current
// owner's claim without making anyone the owner: stop brings their tunnel down as after Stop,
// their proxy settings come back (now, or at their next sign-in), owner.json goes, and the
// service restarts with no owner, answering service_restarting. The administrator's UI then
// claims as on a fresh install.
//
// Not implemented yet, and TakeOver does not call it yet.
func (s *Service) evict(caller peer.Identity, stop func() error) error {
	return fmt.Errorf("evict the owner for %q: not implemented yet: %w", caller.UserID, errors.ErrUnsupported)
}
