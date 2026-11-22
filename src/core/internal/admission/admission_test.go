package admission

// These tests check the pipeline's steps on their own, without mihomo's parser. The tests
// of what mihomo reads from the result run the whole pipeline in internal/engine
// (admission_test.go, security_test.go).

import (
	"fmt"
	"net/netip"
	"slices"
	"testing"

	"github.com/metacubex/mihomo/common/yaml"
	C "github.com/metacubex/mihomo/constant"
	"github.com/metacubex/mihomo/constant/features"
	LC "github.com/metacubex/mihomo/listener/config"

	"github.com/openweir/app-desktop/src/core/internal/compose"
)

const minimal = "mixed-port: 7890\nproxies: []\nrules: ['MATCH,DIRECT']\n"

func TestOverridesExtendLists(t *testing.T) {
	read, err := decodeDocument([]byte(`proxies: [{name: a, type: direct}]
proxy-groups: [{name: G, type: select, proxies: [a]}]
rules: ['DOMAIN,a.example,G', 'MATCH,G']
`), nil)
	if err != nil {
		t.Fatal(err)
	}
	doc := read.Profile
	err = applyOverrides(doc, [][]byte{
		[]byte(`prepend-rules: ['DOMAIN,first.example,DIRECT']
append-rules: ['MATCH,DIRECT']
prepend-proxies: [{name: p0, type: direct}]
append-proxies: [{name: p9, type: direct}]
prepend-proxy-groups: [{name: G0, type: select, proxies: [p0]}]
append-proxy-groups: [{name: G9, type: select, proxies: [p9]}]
`),
		// A later override replaces the list and extends the replacement.
		[]byte("rules: ['MATCH,G']\nprepend-rules: ['DOMAIN,second.example,DIRECT']\n"),
	})
	if err != nil {
		t.Fatal(err)
	}
	names := func(key string) []string {
		var out []string
		for _, item := range doc[key].([]any) {
			out = append(out, item.(map[string]any)["name"].(string))
		}
		return out
	}
	if got := fmt.Sprint(names("proxies")); got != "[p0 a p9]" {
		t.Errorf("proxies = %s", got)
	}
	if got := fmt.Sprint(names("proxy-groups")); got != "[G0 G G9]" {
		t.Errorf("proxy-groups = %s", got)
	}
	if got := fmt.Sprint(doc["rules"]); got != "[DOMAIN,second.example,DIRECT MATCH,G]" {
		t.Errorf("rules = %s", got)
	}
	for _, key := range []string{"prepend-rules", "append-proxies"} {
		if _, ok := doc[key]; ok {
			t.Errorf("%s reached the configuration", key)
		}
	}
}

// What goes to the parser is the document, and the options are the ones the host's mode
// left; a profile's own YAML brings no composition and no mesh networks.
func TestPreparedIsWhatGoesToTheParser(t *testing.T) {
	profile := Profile{Config: []byte(minimal), Options: RunOptions{TUN: true, AllowLAN: true, MixedPort: 17890}}
	for mode, want := range map[TunMode]RunOptions{
		TunOwned: {TUN: true, AllowLAN: true, MixedPort: 17890},
		TunFD:    {TUN: true, AllowLAN: true, MixedPort: 17890},
	} {
		prepared, err := Prepare(profile, Host{Tun: mode})
		if err != nil {
			t.Fatalf("%v: %v", mode, err)
		}
		if prepared.Options != want || prepared.Host.Tun != mode {
			t.Errorf("%v: options = %+v, host = %+v", mode, prepared.Options, prepared.Host)
		}
		var decoded map[string]any
		if err := yaml.Unmarshal(prepared.YAML, &decoded); err != nil {
			t.Fatal(err)
		}
		if fmt.Sprint(decoded) != fmt.Sprint(prepared.Document) {
			t.Errorf("%v: the YAML is not the document:\n%s", mode, prepared.YAML)
		}
		if prepared.Composed != nil || prepared.Networks != nil {
			t.Errorf("%v: composed %v, networks %v", mode, prepared.Composed, prepared.Networks)
		}
	}
	profile.Options.TUN = false
	prepared, err := Prepare(profile, Host{Tun: TunNone})
	if err != nil {
		t.Fatal(err)
	}
	if want := (RunOptions{MixedPort: 17890}); prepared.Options != want {
		t.Errorf("no TUN: options = %+v, want %+v", prepared.Options, want)
	}
}

// D19: replacing the stack is a warning for the engine to log; mihomo's runtime, and its
// log, are not this package's.
func TestFDModeWarnsWhenItReplacesTheStack(t *testing.T) {
	fallback := "mips"
	if features.WithGVisor {
		fallback = "gvisor"
	}
	var warnings []string
	warn := func(message string) { warnings = append(warnings, message) }
	profile := Profile{Config: []byte(minimal + "tun: {stack: system}\n")}
	prepared, err := Prepare(profile, Host{Tun: TunFD, IncludeAllNetworks: true, Warn: warn})
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"the system TUN stack does not work while the tunnel includes all networks; using " + fallback}
	if !slices.Equal(warnings, want) {
		t.Fatalf("warnings = %q, want %q", warnings, want)
	}
	if stack := prepared.Document["tun"].(map[string]any)["stack"]; stack != fallback {
		t.Fatalf("tun.stack = %v, want %s", stack, fallback)
	}
	mips := Profile{Config: []byte(minimal + "tun: {stack: mips}\n")}
	for name, run := range map[string]struct {
		profile Profile
		host    Host
	}{
		"without includeAllNetworks": {profile, Host{Tun: TunFD, Warn: warn}},
		"owned TUN":                  {profile, Host{Tun: TunOwned, IncludeAllNetworks: true, Warn: warn}},
		"mips":                       {mips, Host{Tun: TunFD, IncludeAllNetworks: true, Warn: warn}},
	} {
		warnings = nil
		if _, err := Prepare(run.profile, run.host); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if warnings != nil {
			t.Errorf("%s: warnings = %q", name, warnings)
		}
	}
}

// The extension makes DNSServer the system's resolver; mihomo hijacks the address after
// its TUN's own, which is the same one only while the TUN sits on FDNetwork.Inet4.
func TestFDNetworkDNSIsTheHijackedAddress(t *testing.T) {
	if FDNetwork.Inet4.String() != "198.18.0.1/30" || FDNetwork.DNSServer().String() != "198.18.0.2" {
		t.Fatalf("network = %+v, dns = %s", FDNetwork, FDNetwork.DNSServer())
	}
	if err := verifyFDTun(LC.Tun{Inet4Address: []netip.Prefix{netip.MustParsePrefix("28.0.0.1/30")}, MTU: FDNetwork.MTU}, false); err == nil {
		t.Fatal("a TUN off FDNetwork passed")
	}
}

func TestFDModeVerifiesTheStackForAllNetworks(t *testing.T) {
	tun := LC.Tun{Stack: C.TunMixed, Inet4Address: []netip.Prefix{FDNetwork.Inet4}, MTU: FDNetwork.MTU}
	if err := verifyFDTun(tun, false); err != nil {
		t.Fatalf("mixed without includeAllNetworks: %v", err)
	}
	if err := verifyFDTun(tun, true); err == nil {
		t.Fatal("mixed passed while the tunnel includes all networks")
	}
}

// The base section's TLS and HTTP sniffing override the destination only without the TUN
// (docs/routing.md §8.6): under it, fake-ip DNS names every resolved flow, and the override
// would only send bare-IP flows that borrow a name to the site they disguise as. A
// subscription's own sniffing, and what an override sets, stay.
func TestSniffingFollowsTheTUN(t *testing.T) {
	overrides := func(profile map[string]any) []any {
		sniff := profile["sniffer"].(map[string]any)["sniff"].(map[string]any)
		return []any{sniff["HTTP"].(map[string]any)["override-destination"],
			sniff["TLS"].(map[string]any)["override-destination"], sniff["QUIC"].(map[string]any)["override-destination"]}
	}
	composition := &compose.Composition{Plan: []byte(`{"version": 1}`)}
	links := []byte("trojan://secret@t.example:443#SG\n")
	own := []byte(minimal + "dns: {enable: true, enhanced-mode: fake-ip}\n" +
		"sniffer: {enable: true, sniff: {TLS: {ports: [443], override-destination: true}}}\n")
	for _, run := range []struct {
		name    string
		host    Host
		options RunOptions
		want    []any
	}{
		{"no TUN", Host{Tun: TunNone}, RunOptions{}, []any{true, true, false}},
		{"system proxy only", Host{Tun: TunOwned}, RunOptions{TUN: false}, []any{true, true, false}},
		{"owned TUN", Host{Tun: TunOwned}, RunOptions{TUN: true}, []any{false, false, false}},
		{"fd mode", Host{Tun: TunFD}, RunOptions{}, []any{false, false, false}},
	} {
		for _, profile := range []Profile{{Composition: composition}, {Config: links}} {
			profile.Options = run.options
			prepared, err := Prepare(profile, run.host)
			if err != nil {
				t.Fatalf("%s: %v", run.name, err)
			}
			if got := overrides(prepared.Document); !slices.Equal(got, run.want) {
				t.Errorf("%s, composed %v: override-destination = %v, want %v", run.name, profile.Composition != nil, got, run.want)
			}
		}
		prepared, err := Prepare(Profile{Config: own, Options: run.options}, run.host)
		if err != nil {
			t.Fatalf("%s: %v", run.name, err)
		}
		tls := prepared.Document["sniffer"].(map[string]any)["sniff"].(map[string]any)["TLS"].(map[string]any)
		if tls["override-destination"] != true {
			t.Errorf("%s: the subscription's own sniffing changed: %v", run.name, tls)
		}
	}
	override := Profile{Composition: composition, Options: RunOptions{TUN: true},
		Overrides: [][]byte{[]byte("sniffer: {sniff: {TLS: {ports: [443], override-destination: true}}}\n")}}
	prepared, err := Prepare(override, Host{Tun: TunOwned})
	if err != nil {
		t.Fatal(err)
	}
	if tls := prepared.Document["sniffer"].(map[string]any)["sniff"].(map[string]any)["TLS"].(map[string]any); tls["override-destination"] != true {
		t.Errorf("an override's sniffing changed: %v", tls)
	}
}
