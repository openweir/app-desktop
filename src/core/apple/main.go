// Command apple builds the C archive linked into the macOS system extension
// (docs/architecture.md §6). The extension reads the tunnel's network with
// openweir_tunnel_network, attaches the utun descriptor with openweir_attach_tun once its
// network settings are in place, hands each provider message to openweir_handle_message,
// and detaches with openweir_detach_tun before the tunnel goes down.
package main

/*
#include <stdbool.h>
#include <stdlib.h>
*/
import "C"

import (
	"path/filepath"
	"sync"
	"unsafe"

	"github.com/openweir/app-desktop/src/core/internal/control"
	"github.com/openweir/app-desktop/src/core/internal/crashlog"
	"github.com/openweir/app-desktop/src/core/internal/frame"
)

var crashOnce sync.Once

// openweir_crash_logs installs the runtime sink before the provider opens the TUN.
// The system extension is a process, so the sink stays alive across tunnel sessions.
//
//export openweir_crash_logs
func openweir_crash_logs(home *C.char) {
	crashOnce.Do(func() { _, _ = crashlog.Open(filepath.Join(C.GoString(home), "crashes")) })
}

// openweir_tunnel_network describes the tunnel's network (admission.FDNetwork) as JSON for
// the extension's network settings. The caller releases the buffer with openweir_free.
//
//export openweir_tunnel_network
func openweir_tunnel_network(length *C.size_t) unsafe.Pointer {
	data := networkJSON()
	*length = C.size_t(len(data))
	return C.CBytes(data)
}

// openweir_attach_tun opens the engine in fd mode on the utun descriptor, with home as
// mihomo's home directory, and the dispatcher that answers openweir_handle_message; the
// core runs once a Start message brings its configuration. Before that, it copies the GEO
// data in resources, the extension's resource directory, into home where home lacks it.
// include_all_networks is the VPN configuration's includeAllNetworks: while it is set, the
// engine replaces the system and mixed TUN stacks with gVisor (D19).
// The descriptor stays the caller's: Go never closes it, and mihomo runs on duplicates of
// it that Go closes. It returns NULL, or an error the caller releases with openweir_free.
//
//export openweir_attach_tun
func openweir_attach_tun(fd C.int, home *C.char, resources *C.char, include_all_networks C.bool) *C.char {
	if err := attach(int(fd), C.GoString(home), C.GoString(resources), bool(include_all_networks)); err != nil {
		return C.CString(err.Error())
	}
	return nil
}

// openweir_detach_tun stops the core and closes the dispatcher and every duplicate of the
// descriptor. Once it returns, Go no longer uses the descriptor, and the caller may close it.
//
//export openweir_detach_tun
func openweir_detach_tun() {
	detach()
}

// openweir_handle_message answers one Request. The returned buffer belongs to the
// caller, who releases it with openweir_free.
//
//export openweir_handle_message
func openweir_handle_message(request unsafe.Pointer, requestLength C.size_t, responseLength *C.size_t) unsafe.Pointer {
	var response []byte
	if requestLength > frame.MaxRequestBytes {
		response = control.EncodeFailure(0, control.CodeInvalidRequest, "request exceeds the size limit")
	} else {
		response = handle(C.GoBytes(request, C.int(requestLength)))
	}
	*responseLength = C.size_t(len(response))
	return C.CBytes(response)
}

//export openweir_free
func openweir_free(buffer unsafe.Pointer) {
	C.free(buffer)
}

func main() {}
