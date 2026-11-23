//go:build linux

package owner

import (
	"errors"

	"github.com/openweir/app-desktop/src/core/internal/peer"
	"github.com/openweir/app-desktop/src/core/internal/power"
	"github.com/openweir/app-desktop/src/core/internal/sysproxy"
)

// errLinuxPlatform is what the Linux Platform answers until it is written (D60–D67).
var errLinuxPlatform = errors.New("the Linux platform is not implemented yet")

// Linux is the Linux service's Platform; restart ends the process for systemd to start
// again. Users are decimal uids and sessions logind's users (docs/architecture.md "Linux (in
// progress)"). A stub until the owner's Linux side is written.
func Linux(restart func()) Platform { return linuxPlatform{restart: restart} }

type linuxPlatform struct{ restart func() }

func (p linuxPlatform) Restart() { p.restart() }

func (linuxPlatform) NetworkReady() bool { return power.NetworkAvailable() }

func (linuxPlatform) MayClaim(peer.Identity) error { return errLinuxPlatform }

func (linuxPlatform) SecureDir(string, string) error { return errLinuxPlatform }

func (linuxPlatform) SessionUser(uint32) (string, error) { return "", errLinuxPlatform }

func (linuxPlatform) SessionLogonTime(uint32) (int64, error) { return 0, errLinuxPlatform }

func (linuxPlatform) SignedIn(string) []uint32 { return nil }

func (linuxPlatform) ReadProxy(uint32) (sysproxy.Settings, error) {
	return sysproxy.Settings{}, errLinuxPlatform
}

func (linuxPlatform) WriteProxy(uint32, sysproxy.Settings) error { return errLinuxPlatform }
