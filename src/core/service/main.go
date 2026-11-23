// Command service is the LocalSystem service on Windows (docs/architecture.md §5),
// built as openweir-service.exe with go build -o, and the root systemd service of the Linux
// release with -system (docs/architecture.md "Linux (in progress)"). On macOS, and on Linux
// with -socket, it runs as a development server on a Unix socket (§11); the macOS release
// uses the system extension instead.
package main

import (
	"fmt"
	"os"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
