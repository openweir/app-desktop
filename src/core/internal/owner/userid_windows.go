//go:build windows

package owner

// validUserID accepts what names a user on this system: a SID on Windows.
func validUserID(id string) bool { return ValidSID(id) }
