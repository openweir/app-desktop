//go:build windows

package owner

import "github.com/openweir/app-desktop/src/core/internal/peer"

// validUserID accepts what names a user on this system: a SID on Windows.
func validUserID(id string) bool { return ValidSID(id) }

// evicts reports whether caller ends the owner's claim instead of taking the tunnel over. On
// Windows an administrator who confirmed UAC takes it over as themselves.
func evicts(peer.Identity) bool { return false }
