package main

import (
	"errors"
	"flag"
	"io"
	"testing"
)

func modesFor(t *testing.T, args ...string) (func() error, error) {
	t.Helper()
	flags := flag.NewFlagSet("openweir-service", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	socket := flags.String("socket", "", "")
	modes := registerModes(flags)
	if err := flags.Parse(args); err != nil {
		t.Fatal(err)
	}
	return modes.selected(*socket != "", flags.Args())
}

func TestTheDevelopmentServerKeepsItsFlags(t *testing.T) {
	for _, args := range [][]string{{"-socket", "/tmp/dev.sock"}, {}} {
		if release, err := modesFor(t, args...); release != nil || err != nil {
			t.Fatalf("%q: a release mode (%v)", args, err)
		}
	}
}

func TestTheReleaseModesAreNotImplementedYet(t *testing.T) {
	for _, args := range [][]string{
		{"-system"},
		{"-system", "-foreground"},
		{"-cleanup"},
		{"-take-over"},
		{"-install-update", "openweir.deb", "SHA256SUMS", "SHA256SUMS.sig"},
	} {
		release, err := modesFor(t, args...)
		if err != nil || release == nil {
			t.Fatalf("%q: no release mode (%v)", args, err)
		}
		if err := release(); !errors.Is(err, errNotImplemented) && !errors.Is(err, errors.ErrUnsupported) {
			t.Errorf("%q = %v, want not implemented", args, err)
		}
	}
}

func TestTheModesExcludeOneAnother(t *testing.T) {
	for _, args := range [][]string{
		{"-socket", "/tmp/dev.sock", "-system"},
		{"-system", "-cleanup"},
		{"-take-over", "-install-update", "a", "b", "c"},
		{"-foreground"},
		{"-install-update", "openweir.deb"},
	} {
		if _, err := modesFor(t, args...); err == nil {
			t.Errorf("%q: accepted", args)
		}
	}
}
