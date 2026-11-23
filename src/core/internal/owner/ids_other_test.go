//go:build !windows

package owner

// Linux users are uids.
const (
	alice = "1000"
	bob   = "1001"
)
