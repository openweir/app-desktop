package main

import "fmt"

// installUpdate is to install the package at pkg as root through pkexec once sums, a
// release's SHA256SUMS, checks out against signature with product.LinuxUpdateKey and pkg
// against sums (docs/architecture.md "Linux (in progress)", D62). The UI's shell then starts
// the UI again with --update-result=<this process's exit code>.
func installUpdate(pkg, sums, signature string) error {
	return fmt.Errorf("-install-update: %w", errNotImplemented)
}
