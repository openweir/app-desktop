package owner

import (
	"fmt"

	"github.com/openweir/app-desktop/src/core/internal/control"
	"github.com/openweir/app-desktop/src/core/internal/peer"
)

// evict is TakeOver's way for root on Linux (docs/architecture.md "Linux", D66).
// There the administrator confirms through pkexec, and the helper the UI runs that way
// connects as root (caller.Administrator, UserID "0"), not as the user who asked; root must
// not become the owner, whose home mihomo runs in. So evict ends the current owner's claim
// without making anyone the owner: stop brings their tunnel down as after Stop, their proxy
// settings come back (now, or at their next sign-in), owner.json goes, and the service
// restarts with no owner, answering service_restarting. The administrator's UI then claims as
// on a fresh install.
//
// With no tunnel up there is nothing to take over, and evict changes nothing: the
// administrator's UI may claim a stopped tunnel as anyone may.
func (s *Service) evict(caller peer.Identity, stop func() error) error {
	s.mu.Lock()
	switch {
	case s.closed, s.restarting:
		s.mu.Unlock()
		return control.ErrRestarting
	case s.owner == nil || !s.running():
		s.mu.Unlock()
		return nil
	}
	former := s.owner.SID
	s.mu.Unlock()
	// As in TakeOver, the core stops and the former owner's proxy settings come back outside
	// mu, while the dispatcher's operation lock keeps any claim or start out.
	if err := stop(); err != nil {
		return fmt.Errorf("stop the other user's tunnel: %w", err)
	}
	if err := s.Stopped(); err != nil {
		s.log.Warn("restore the former owner's proxy settings", "error", err)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed || s.restarting {
		return control.ErrRestarting
	}
	if err := RemoveRecord(s.root); err != nil {
		return fmt.Errorf("forget the owner: %w", err)
	}
	s.log.Info("an administrator ended the owner's claim; restarting with no owner", "from", former, "by", caller.UserID)
	s.cancelPowerLocked()
	s.owner, s.restarting = nil, true
	s.platform.Restart()
	return control.ErrRestarting
}
