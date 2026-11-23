//go:build (windows || linux) && with_naive && with_purego

package main

import (
	"path/filepath"
	"runtime"

	cronet "github.com/openweir/cronet-go"
)

// Pin the privileged service to its installed library, never PATH or LD_LIBRARY_PATH.
// LoadLibrary remembers failures too, so a missing package cannot later fall back to an
// unrelated library.
func loadNativeRuntime(directory string) error {
	return cronet.LoadLibrary(filepath.Join(directory, nativeLibrary()))
}

// nativeLibrary is Cronet's library, which the installer or package puts beside the service.
func nativeLibrary() string {
	if runtime.GOOS == "windows" {
		return "libcronet.dll"
	}
	return "libcronet.so"
}
