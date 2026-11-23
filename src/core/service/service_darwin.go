package main

import "flag"

// The macOS release is the system extension; this binary only runs as the development
// server there.
const noMode = "-socket is required: on macOS the service only runs as a development server"

// releaseModes are Linux's; macOS has none.
type releaseModes struct{}

func registerModes(*flag.FlagSet) releaseModes { return releaseModes{} }

func (releaseModes) selected(bool, []string) (func() error, error) { return nil, nil }
