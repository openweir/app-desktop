package admission

import (
	"errors"
	"reflect"
	"slices"
	"strings"
	"testing"

	LC "github.com/metacubex/mihomo/listener/config"

	"github.com/openweir/app-desktop/src/core/internal/tunclean"
)

var linuxPins = tunclean.Pins{Device: "openweir0", Table: 9347, RuleIndex: 9347}

// movedTun names another device, table and rules, and every option that pins delete.
const movedTun = `redir-port: 7892
tproxy-port: 7893
tun:
  device: elsewhere0
  mtu: 1400
  iproute2-table-index: 2022
  iproute2-rule-index: 9000
  include-uid: [0]
  include-uid-range: ['1000:1999']
  exclude-uid: [1000]
  exclude-uid-range: ['2000:2999']
  include-android-user: [0, 10]
  include-package: [com.example]
  exclude-package: [com.example.other]
  include-interface: [eth0]
  exclude-interface: [wlan0]
  route-address-set: [lan]
  route-exclude-address-set: [cn]
  auto-redirect: true
  auto-redirect-input-mark: 8227
  auto-redirect-output-mark: 8228
  auto-redirect-iproute2-fallback-rule-index: 32768
  exclude-dst-port: [5353]
`

func TestPinsHoldTheOwnedTUN(t *testing.T) {
	fromProfile := Profile{Config: []byte(minimal + movedTun)}
	fromOverride := Profile{Config: []byte(minimal), Overrides: [][]byte{[]byte(movedTun)}}
	for name, profile := range map[string]Profile{"profile": fromProfile, "override": fromOverride} {
		for _, on := range []bool{true, false} {
			profile.Options.TUN = on
			prepared, err := Prepare(profile, Host{Tun: TunOwned, Pins: linuxPins})
			if err != nil {
				t.Fatalf("%s, TUN %v: %v", name, on, err)
			}
			doc := prepared.Document
			tun := doc["tun"].(map[string]any)
			if tun["device"] != "openweir0" || tun["iproute2-table-index"] != 9347 || tun["iproute2-rule-index"] != 9347 {
				t.Errorf("%s, TUN %v: tun = %v", name, on, tun)
			}
			for _, key := range unpinnedTunKeys {
				if value, ok := tun[key]; ok {
					t.Errorf("%s, TUN %v: tun.%s = %v survived", name, on, key, value)
				}
			}
			if doc["redir-port"] != 0 || doc["tproxy-port"] != 0 {
				t.Errorf("%s, TUN %v: redir-port %v, tproxy-port %v", name, on, doc["redir-port"], doc["tproxy-port"])
			}
			// What does not choose the device, table, rules or users stays the profile's.
			if tun["mtu"] == nil || tun["exclude-dst-port"] == nil || tun["auto-redirect"] != false {
				t.Errorf("%s, TUN %v: tun = %v", name, on, tun)
			}
		}
	}
}

// Without pins, on Windows and in the TUN tests that name their own device, owned mode
// leaves the TUN's device and lists to the profile, as before.
func TestNoPinsLeaveTheTUNToTheProfile(t *testing.T) {
	profile := Profile{Config: []byte(minimal + movedTun), Options: RunOptions{TUN: true}}
	prepared, err := Prepare(profile, Host{Tun: TunOwned})
	if err != nil {
		t.Fatal(err)
	}
	tun := prepared.Document["tun"].(map[string]any)
	if tun["device"] != "elsewhere0" || tun["iproute2-table-index"] != 2022 || tun["include-uid"] == nil {
		t.Fatalf("tun = %v", tun)
	}
}

func TestPrepareRefusesPinsThatAreNotTheTunnels(t *testing.T) {
	for _, pins := range []tunclean.Pins{
		{Device: "openweir0", Table: 254, RuleIndex: 9347},
		{Device: "openweir0", Table: 9347},
		{Table: 9347, RuleIndex: 9347},
	} {
		_, err := Prepare(Profile{Config: []byte(minimal)}, Host{Tun: TunOwned, Pins: pins})
		if !errors.Is(err, ErrUnsupported) {
			t.Errorf("%+v: Prepare = %v", pins, err)
		}
	}
}

// Every option pins delete is one mihomo reads into the TUN, so a renamed one cannot slip
// past the patch.
func TestUnpinnedKeysAreMihomos(t *testing.T) {
	var keys []string
	fields := reflect.TypeFor[LC.Tun]()
	for i := range fields.NumField() {
		keys = append(keys, strings.Split(fields.Field(i).Tag.Get("yaml"), ",")[0])
	}
	for _, key := range append(slices.Clone(unpinnedTunKeys), "device", "iproute2-table-index", "iproute2-rule-index") {
		if !slices.Contains(keys, key) {
			t.Errorf("mihomo's TUN has no %s", key)
		}
	}
}

func TestVerifyHoldsTheParsedTUNToThePins(t *testing.T) {
	held := func() LC.Tun {
		return LC.Tun{Device: "openweir0", IPRoute2TableIndex: 9347, IPRoute2RuleIndex: 9347, AutoRoute: true, StrictRoute: true}
	}
	if err := verifyPins(held(), 0, 0, linuxPins); err != nil {
		t.Fatal(err)
	}
	moves := map[string]func(*LC.Tun){
		"device":                                     func(tun *LC.Tun) { tun.Device = "elsewhere0" },
		"iproute2-table-index":                       func(tun *LC.Tun) { tun.IPRoute2TableIndex = 2022 },
		"iproute2-rule-index":                        func(tun *LC.Tun) { tun.IPRoute2RuleIndex = 9000 },
		"include-uid":                                func(tun *LC.Tun) { tun.IncludeUID = []uint32{0} },
		"include-uid-range":                          func(tun *LC.Tun) { tun.IncludeUIDRange = []string{"1000:1999"} },
		"exclude-uid":                                func(tun *LC.Tun) { tun.ExcludeUID = []uint32{1000} },
		"exclude-uid-range":                          func(tun *LC.Tun) { tun.ExcludeUIDRange = []string{"2000:2999"} },
		"include-android-user":                       func(tun *LC.Tun) { tun.IncludeAndroidUser = []int{0} },
		"include-package":                            func(tun *LC.Tun) { tun.IncludePackage = []string{"com.example"} },
		"exclude-package":                            func(tun *LC.Tun) { tun.ExcludePackage = []string{"com.example"} },
		"include-interface":                          func(tun *LC.Tun) { tun.IncludeInterface = []string{"eth0"} },
		"exclude-interface":                          func(tun *LC.Tun) { tun.ExcludeInterface = []string{"wlan0"} },
		"route-address-set":                          func(tun *LC.Tun) { tun.RouteAddressSet = []string{"lan"} },
		"route-exclude-address-set":                  func(tun *LC.Tun) { tun.RouteExcludeAddressSet = []string{"cn"} },
		"auto-redirect":                              func(tun *LC.Tun) { tun.AutoRedirect = true },
		"auto-redirect-input-mark":                   func(tun *LC.Tun) { tun.AutoRedirectInputMark = 8227 },
		"auto-redirect-output-mark":                  func(tun *LC.Tun) { tun.AutoRedirectOutputMark = 8228 },
		"auto-redirect-iproute2-fallback-rule-index": func(tun *LC.Tun) { tun.AutoRedirectIPRoute2FallbackRuleIndex = 32768 },
	}
	for _, key := range unpinnedTunKeys {
		if moves[key] == nil {
			t.Errorf("no check that %s stays deleted", key)
		}
	}
	for key, move := range moves {
		tun := held()
		move(&tun)
		err := verifyPins(tun, 0, 0, linuxPins)
		if err == nil {
			t.Errorf("%s moved, and verifyPins passed", key)
		} else if slices.Contains(unpinnedTunKeys, key) && !strings.Contains(err.Error(), "tun."+key+" ") {
			t.Errorf("%s: verifyPins = %v", key, err)
		}
	}
	for _, ports := range [][2]int{{7892, 0}, {0, 7893}} {
		if err := verifyPins(held(), ports[0], ports[1], linuxPins); err == nil {
			t.Errorf("redir-port %d, tproxy-port %d passed", ports[0], ports[1])
		}
	}
}
