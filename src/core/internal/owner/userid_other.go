//go:build !windows

package owner

// validUserID accepts what names a user on this system: a decimal uid on Linux.
func validUserID(id string) bool { return ValidUID(id) }
