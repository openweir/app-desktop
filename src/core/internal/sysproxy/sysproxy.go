// Package sysproxy reads and writes a user's WinINet proxy settings: the system proxy the
// Windows service points at the core's mixed port for its owner (docs/architecture.md §5.5).
package sysproxy

import (
	"net"
	"strconv"
)

// WinINet's PROXY_TYPE_* bits for Settings.Flags.
const (
	Direct       = 1
	Proxy        = 2
	AutoProxyURL = 4
	AutoDetect   = 8
)

// Settings are the proxy settings of a user's default connection.
type Settings struct {
	Flags         uint32 `json:"flags"`
	Server        string `json:"server"`
	Bypass        string `json:"bypass"`
	AutoConfigURL string `json:"autoconfig_url"`
}

// Bypass keeps loopback and private addresses off the proxy; "<local>" covers plain host
// names.
const Bypass = "localhost;127.*;10.*;172.16.*;172.17.*;172.18.*;172.19.*;172.20.*;172.21.*;" +
	"172.22.*;172.23.*;172.24.*;172.25.*;172.26.*;172.27.*;172.28.*;172.29.*;172.30.*;" +
	"172.31.*;192.168.*;<local>"

// Core sends a user's traffic to the core's mixed port on port.
func Core(port uint16) Settings {
	return Settings{
		Flags:  Direct | Proxy,
		Server: net.JoinHostPort("127.0.0.1", strconv.Itoa(int(port))),
		Bypass: Bypass,
	}
}

// Ours reports whether settings are what Core writes, on any port: the service's own, left
// behind on a port the core no longer opens (7890 before 9347, D53), never a user's.
func Ours(settings Settings) bool {
	host, port, err := net.SplitHostPort(settings.Server)
	if err != nil || host != "127.0.0.1" {
		return false
	}
	number, err := strconv.ParseUint(port, 10, 16)
	return err == nil && number != 0 && settings == Core(uint16(number))
}

// Owned is what to keep as a user's own settings, and to give back: settings of the
// service's own become no proxy, so that the user never gets a proxy to a closed port back.
func Owned(settings Settings) Settings {
	if Ours(settings) {
		return Settings{Flags: Direct}
	}
	return settings
}
