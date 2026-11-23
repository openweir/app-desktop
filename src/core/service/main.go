// Command service is the LocalSystem service on Windows (docs/architecture.md §5),
// built as openweir-service.exe with go build -o, and the root systemd service of the Linux
// release with -system (docs/architecture.md "Linux"). On macOS, and on Linux
// with -socket, it runs as a development server on a Unix socket (§11); the macOS release
// uses the system extension instead.
package main

import (
	"errors"
	"fmt"
	"os"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		code := 1
		if exit := (*exitError)(nil); errors.As(err, &exit) {
			code = exit.code
		}
		os.Exit(code)
	}
}

// exitError ends the process with code rather than 1, for a caller that tells failures apart:
// the Linux UI reads -install-update's (update_linux.go).
type exitError struct {
	code int
	err  error
}

func (e *exitError) Error() string { return e.err.Error() }

func (e *exitError) Unwrap() error { return e.err }
