//go:build !windows

package owner

import "github.com/openweir/app-desktop/src/core/internal/peer"

// superuser is root's uid, as the take-over helper that pkexec runs connects.
const superuser = "0"

// validUserID accepts what names a user on this system: a decimal uid on Linux.
func validUserID(id string) bool { return ValidUID(id) }

// evicts reports whether caller ends the owner's claim instead of taking the tunnel over:
// root through pkexec, who must not become the owner (evict, D66).
func evicts(caller peer.Identity) bool {
	return caller.Administrator && caller.UserID == superuser
}
