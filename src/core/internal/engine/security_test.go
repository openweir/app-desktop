package engine

import (
	"context"
	"errors"
	"fmt"
	"go/ast"
	"go/build"
	"go/parser"
	"go/token"
	"go/types"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/metacubex/mihomo/adapter/provider"
	"github.com/metacubex/mihomo/common/yaml"
	C "github.com/metacubex/mihomo/constant"
	"github.com/metacubex/mihomo/constant/features"
	"github.com/metacubex/mihomo/hub/executor"
	"github.com/metacubex/mihomo/hub/route"

	"github.com/openweir/app-desktop/src/core/internal/admission"
	"github.com/openweir/app-desktop/src/core/internal/porttest"
	"github.com/openweir/app-desktop/src/core/internal/product"
	"github.com/openweir/app-desktop/src/core/internal/tunclean"
)

// Regression tests for the requirements of docs/architecture.md §3.3, in order.

// #1: no build carries the cmfa tag, which turns IsSafePath into "always true".
func TestSecurity1NoCMFABuild(t *testing.T) {
	if features.CMFA {
		t.Fatal("this binary was built with the cmfa tag")
	}
	// cmfa.go joins the package only under the tag, and does not type-check.
	tagged := build.Default
	tagged.BuildTags = []string{"cmfa"}
	if ok, err := tagged.MatchFile(".", "cmfa.go"); err != nil || !ok {
		t.Fatalf("cmfa.go is not part of a cmfa build: %v", err)
	}
	if ok, _ := build.Default.MatchFile(".", "cmfa.go"); ok {
		t.Fatal("cmfa.go is part of the normal build")
	}
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "cmfa.go", nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := (&types.Config{}).Check("engine", fset, []*ast.File{file}, nil); err == nil {
		t.Fatal("cmfa.go type-checks, so a cmfa build would succeed")
	}
	if err := checkPathSandbox(C.Path.HomeDir()); err != nil {
		t.Fatalf("the sandbox self-check fails in a normal build: %v", err)
	}
}

// #2: the controller fields are cleared last, after the overrides, in every mode.
func TestSecurity2ControllerIsAlwaysCleared(t *testing.T) {
	const controller = `
external-controller: 0.0.0.0:9090
external-controller-tls: 0.0.0.0:9443
external-controller-unix: mihomo.sock
external-controller-pipe: \\.\pipe\mihomo
external-doh-server: /dns-query
external-ui: ui
external-ui-name: zashboard
external-ui-url: https://example.invalid/ui.zip
secret: hunter2
`
	overrides := [][]byte{
		[]byte("external-controller: 0.0.0.0:9091\nexternal-controller-pipe: '\\\\.\\pipe\\override'\n"),
		[]byte("secret: again\nexternal-doh-server: /again\nexternal-ui: ui\n"),
	}
	for _, mode := range []admission.TunMode{admission.TunNone, admission.TunOwned} {
		cfg := admitted(t, mode, admission.Profile{Config: []byte(minimal + controller), Overrides: overrides})
		c := cfg.Controller
		if c.ExternalController != "" || c.ExternalControllerTLS != "" || c.ExternalControllerUnix != "" ||
			c.ExternalControllerPipe != "" || c.ExternalDohServer != "" || c.Secret != "" ||
			c.ExternalUI != "" || c.ExternalUIName != "" || c.ExternalUIURL != "" {
			t.Fatalf("%v: controller survived admission: %+v", mode, c)
		}
	}

	// And through Start, which hands the admitted configuration to hub.ApplyConfig: that
	// starts whatever controller the configuration names (#3 starts one the same way).
	engine := openEngine(t, admission.TunNone)
	dir, err := os.MkdirTemp("", "md-ctl-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	socket := filepath.Join(dir, "c.sock")
	api := porttest.Free(t)
	profile := directProfile(porttest.Free(t), "http://127.0.0.1:1/",
		fmt.Sprintf("external-controller: 127.0.0.1:%d\nexternal-controller-unix: '%s'", api, socket))
	if _, err := engine.Start(profile); err != nil {
		t.Fatal(err)
	}
	defer engine.Stop()
	// Controllers start in the background, a requested one within milliseconds.
	for deadline := time.Now().Add(300 * time.Millisecond); time.Now().Before(deadline); {
		if listening(api) || unixListening(socket) {
			t.Fatal("the started core opened the controller its profile asked for")
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// #3: embed mode is on, so even a controller started by mistake cannot restart the
// process, rewrite the configuration or upgrade the core.
func TestSecurity3EmbedModeGuardsAStrayController(t *testing.T) {
	openEngine(t, admission.TunNone)
	dir, err := os.MkdirTemp("", "md-ctl-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	socket := filepath.Join(dir, "c.sock")
	// The one controller these tests start, on purpose and through route, not the pipeline.
	route.ReCreateServer(&route.Config{UnixAddr: socket})
	defer stopStrayController(t, socket)

	client := &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socket)
		},
	}}
	status := func(method, path string) int {
		request, err := http.NewRequest(method, "http://controller"+path, strings.NewReader("{}"))
		if err != nil {
			t.Fatal(err)
		}
		response, err := client.Do(request)
		if err != nil {
			t.Fatalf("%s %s: %v", method, path, err)
		}
		response.Body.Close()
		return response.StatusCode
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		conn, err := net.Dial("unix", socket)
		if err == nil {
			conn.Close()
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the stray controller never listened: %v", err)
		}
		time.Sleep(20 * time.Millisecond)
	}
	if got := status(http.MethodGet, "/version"); got != http.StatusOK {
		t.Fatalf("GET /version = %d; the controller is not really up", got)
	}
	for _, call := range []struct{ method, path string }{
		{http.MethodPost, "/restart"},
		{http.MethodPut, "/configs"},
		{http.MethodPatch, "/configs"},
		{http.MethodPost, "/configs/geo"},
		{http.MethodPost, "/upgrade"},
		{http.MethodPost, "/upgrade/geo"},
		{http.MethodPatch, "/rules/disable"},
	} {
		if got := status(call.method, call.path); got < 400 {
			t.Errorf("%s %s = %d, want it refused", call.method, call.path, got)
		}
	}
}

// stopStrayController closes the controller and checks that its listener was released.
func stopStrayController(t *testing.T, socket string) {
	route.ReCreateServer(&route.Config{})
	for deadline := time.Now().Add(5 * time.Second); unixListening(socket); {
		if time.Now().After(deadline) {
			t.Error("the stray controller still listens")
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func unixListening(socket string) bool {
	conn, err := net.Dial("unix", socket)
	if err != nil {
		return false
	}
	conn.Close()
	return true
}

// #4: provider paths stay local by the platform's own rules, and IsSafePath stays on
// behind them: no SKIP_SAFE_PATH_CHECK, no SAFE_PATHS.
func TestSecurity4ProviderPathsStayInHome(t *testing.T) {
	escapes := []string{"../outside.yaml", "providers/../../outside.yaml", filepath.Join(os.TempDir(), "abs.yaml")}
	if runtime.GOOS == "windows" {
		escapes = append(escapes, `..\outside.yaml`, `providers\..\..\outside.yaml`, `C:\Windows\win.ini`,
			`\\server\share\p.yaml`, `C:relative.yaml`, `NUL`)
	}
	for _, kind := range []string{"proxy-providers", "rule-providers"} {
		for _, path := range escapes {
			source := minimal + kind + ":\n  p:\n    type: file\n    behavior: classical\n    path: '" + path + "'\n"
			if _, err := admit(profileOf(source), admission.TunNone); !errors.Is(err, ErrInvalidConfig) {
				t.Errorf("%s path %q: admit = %v, want ErrInvalidConfig", kind, path, err)
			}
		}
	}
	local := minimal + "proxy-providers:\n  p:\n    type: http\n    url: http://127.0.0.1:1/p.yaml\n    path: providers/p.yaml\n"
	cfg := admitted(t, admission.TunNone, profileOf(local))
	if cfg.Providers["p"] == nil {
		t.Fatal("a local provider path was refused")
	}

	for name, env := range map[string]string{
		"SKIP_SAFE_PATH_CHECK": "SKIP_SAFE_PATH_CHECK=true",
		"SAFE_PATHS":           "SAFE_PATHS=" + os.TempDir(),
	} {
		cmd := exec.Command(os.Args[0], "-test.run=^$")
		cmd.Env = append(os.Environ(), helperEnv+"=1", env)
		output, err := cmd.CombinedOutput()
		var exit *exec.ExitError
		if !errors.As(err, &exit) || exit.ExitCode() != 3 || !strings.Contains(string(output), "refused:") {
			t.Errorf("with %s the engine opened: %v\n%s", name, err, output)
		}
	}
}

// #5: node selections are stored, so a restart (and the next boot) returns to them.
func TestSecurity5SelectionSurvivesRestart(t *testing.T) {
	profile := profileOf(minimal + "profile: {store-selected: false}\n")
	profile.Overrides = [][]byte{[]byte("profile: {store-selected: false}\n")}
	if cfg := admitted(t, admission.TunNone, profile); !cfg.Profile.StoreSelected {
		t.Fatal("store-selected is off")
	}

	engine := openEngine(t, admission.TunNone)
	server := target(t)
	profile = directProfile(porttest.Free(t), server.URL+"/generate_204", "profile: {store-selected: false}")
	if _, err := engine.Start(profile); err != nil {
		t.Fatal(err)
	}
	if err := engine.SelectProxy("PROXY", "direct-b"); err != nil {
		t.Fatal(err)
	}
	if err := engine.Stop(); err != nil {
		t.Fatal(err)
	}
	if _, err := engine.Start(profile); err != nil {
		t.Fatal(err)
	}
	group, err := engine.GetGroup("PROXY", 0)
	if err != nil {
		t.Fatal(err)
	}
	if group.GetNow() != "direct-b" {
		t.Fatalf("after a restart PROXY uses %q, want direct-b", group.GetNow())
	}
	// Leave the stored choice where the other tests expect it.
	if err := engine.SelectProxy("PROXY", "direct-a"); err != nil {
		t.Fatal(err)
	}
}

// #6 (docs/mesh.md §4): mesh outbounds only go out. Their state stays in the home
// directory, and EasyTier's options that serve other devices are off, wherever the
// outbound comes from: the profile, an override, a provider's payload, or what a
// provider loads and rewrites later.
func TestSecurity6MeshOutboundsOnlyGoOut(t *testing.T) {
	escapes := []string{"../outside", filepath.Join(os.TempDir(), "mesh-state")}
	for _, outbound := range []string{
		"{name: ts, type: tailscale, state-dir: '%s'}",
		"{name: zt, type: zerotier, network: 8056c2e21c000001, state-dir: '%s'}",
		"{name: et, type: easytier, network-name: n, network-secret: s, peers: ['tcp://198.51.100.1:11010'], state-dir: '%s'}",
	} {
		for _, path := range escapes {
			// A profile's own outbound goes through subconv (D54), which drops the state
			// directory of Tailscale and ZeroTier as the client's setting: whatever it keeps,
			// nothing outside the home gets through.
			source := "mixed-port: 7890\nproxies:\n  - " + fmt.Sprintf(outbound, path) + "\nrules: ['MATCH,DIRECT']\n"
			if admitted, err := admitProfile(profileOf(source), noTUN); err == nil {
				for _, raw := range admitted.doc["proxies"].([]any) {
					if dir, ok := raw.(map[string]any)["state-dir"]; ok {
						t.Errorf("%s: state-dir %v admitted", fmt.Sprintf(outbound, path), dir)
					}
				}
			} else if !errors.Is(err, ErrInvalidConfig) {
				t.Errorf("%s: admit = %v", fmt.Sprintf(outbound, path), err)
			}
			// An override's, which subconv does not read, meets mihomo's own path check.
			profile := profileOf(minimal)
			profile.Overrides = [][]byte{[]byte("prepend-proxies:\n  - " + fmt.Sprintf(outbound, path) + "\n")}
			_, err := admit(profile, admission.TunNone)
			if !errors.Is(err, ErrInvalidConfig) {
				t.Errorf("%s: admit = %v, want ErrInvalidConfig", fmt.Sprintf(outbound, path), err)
				continue
			}
			// Without with_gvisor mihomo refuses every Tailscale outbound, so only the
			// other two show the path check here.
			if strings.Contains(outbound, "tailscale") && !features.WithGVisor {
				continue
			}
			if !strings.Contains(err.Error(), "not subpath of home") {
				t.Errorf("%s: refused for another reason: %v", fmt.Sprintf(outbound, path), err)
			}
		}
	}

	const serving = "{name: %s, type: easytier, network-name: n, network-secret: s, peers: ['tcp://198.51.100.1:11010'], " +
		"enable-exit-node: true, proxy-networks: [192.168.1.0/24], mapped-listeners: ['tcp://203.0.113.1:11010'], " +
		"listeners: ['tcp://0.0.0.0:11010']}"
	source := "mixed-port: 7890\nrules: ['MATCH,DIRECT']\nproxies:\n  - " + fmt.Sprintf(serving, "et-profile") + `
proxy-providers:
  inline:
    type: inline
    payload:
      - ` + fmt.Sprintf(serving, "et-inline") + `
  remote:
    type: http
    url: http://127.0.0.1:1/p.yaml
    path: providers/remote.yaml
    override:
      override-expr: ['.type = "easytier"', '.enable-exit-node = true', '.proxy-networks = ["0.0.0.0/0"]', '.no-listener = false']
`
	profile := profileOf(source)
	profile.Overrides = [][]byte{[]byte("prepend-proxies:\n  - " + fmt.Sprintf(serving, "et-override") + "\n")}
	admitted, err := admitProfile(profile, noTUN)
	if err != nil {
		t.Fatal(err)
	}
	providers := admitted.doc["proxy-providers"].(map[string]any)
	outbounds := append(slices.Clone(admitted.doc["proxies"].([]any)),
		providers["inline"].(map[string]any)["payload"].([]any)...)
	if len(outbounds) != 3 {
		t.Fatalf("outbounds = %v", outbounds)
	}
	for _, raw := range outbounds {
		outbound := raw.(map[string]any)
		if outbound["enable-exit-node"] != false || outbound["proxy-networks"] != nil || outbound["mapped-listeners"] != nil ||
			outbound["listeners"] != nil || outbound["no-listener"] != true {
			t.Errorf("%v still serves other devices", outbound)
		}
	}
	for _, name := range []string{"et-profile", "et-override"} {
		if _, ok := admitted.cfg.Proxies[name]; !ok {
			t.Errorf("%s did not build", name)
		}
	}

	// What the remote provider loads goes through its expressions, the guard last: an
	// outbound they make an EasyTier one, or one that came as one, ends up serving nobody.
	override := providers["remote"].(map[string]any)["override"].(map[string]any)
	if _, ok := providers["inline"].(map[string]any)["override"]; ok {
		t.Error("an inline provider without expressions got the guard")
	}
	for _, loaded := range []map[string]any{
		{"name": "ss", "type": "ss", "server": "a.example", "port": 1},
		{"name": "et", "type": "easytier", "mapped-listeners": []any{"tcp://203.0.113.1:1"}, "listeners": []any{"tcp://0.0.0.0:1"}},
	} {
		for _, raw := range override["override-expr"].([]any) {
			expression, err := provider.NewOverrideExpr(raw.(string))
			if err != nil {
				t.Fatalf("%q: %v", raw, err)
			}
			if err := expression.Apply(loaded); err != nil {
				t.Fatalf("%q: %v", raw, err)
			}
		}
		if loaded["type"] != "easytier" || loaded["enable-exit-node"] != false || loaded["no-listener"] != true ||
			len(loaded["proxy-networks"].([]any)) != 0 || len(loaded["mapped-listeners"].([]any)) != 0 ||
			len(loaded["listeners"].([]any)) != 0 {
			t.Errorf("loaded as %v", loaded)
		}
	}
}

// #7: the Linux service's TUN stays on the product's device, route table and rule priorities,
// so its -cleanup removes exactly what a crashed TUN left (D67), and it takes the whole
// machine's traffic (D66): neither the profile nor an override moves them, picks users, apps
// or interfaces, or opens redir and tproxy ports; and should mihomo ever read otherwise,
// Verify refuses the configuration. The TUN tests run it on a real TUN (tun_pins_linux_test.go).
func TestSecurity7LinuxTUNStaysPinned(t *testing.T) {
	pins := tunclean.Pins{Device: product.LinuxTunDevice, Table: product.LinuxRouteTable, RuleIndex: product.LinuxRuleIndex}
	host := admission.Host{Tun: admission.TunOwned, Pins: pins}
	const moved = `redir-port: 7892
tproxy-port: 7893
tun:
  device: elsewhere0
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
`
	for name, profile := range map[string]admission.Profile{
		"profile":  profileOf(minimal + moved),
		"override": {Config: []byte(minimal), Overrides: [][]byte{[]byte(moved), []byte("tun: {device: again0, iproute2-rule-index: 1}\n")}},
	} {
		for _, on := range []bool{true, false} {
			profile.Options.TUN = on
			admitted, err := admitProfile(profile, host)
			if err != nil {
				t.Fatalf("%s, TUN %v: %v", name, on, err)
			}
			general := admitted.cfg.General
			tun := general.Tun
			if tun.Device != product.LinuxTunDevice || tun.IPRoute2TableIndex != product.LinuxRouteTable ||
				tun.IPRoute2RuleIndex != product.LinuxRuleIndex {
				t.Errorf("%s, TUN %v: device %q, table %d, rules from %d", name, on, tun.Device, tun.IPRoute2TableIndex, tun.IPRoute2RuleIndex)
			}
			if len(tun.IncludeUID)+len(tun.IncludeUIDRange)+len(tun.ExcludeUID)+len(tun.ExcludeUIDRange)+
				len(tun.IncludeAndroidUser)+len(tun.IncludePackage)+len(tun.ExcludePackage)+
				len(tun.IncludeInterface)+len(tun.ExcludeInterface)+len(tun.RouteAddressSet)+len(tun.RouteExcludeAddressSet) != 0 {
				t.Errorf("%s, TUN %v: the TUN still picks users, apps, interfaces or sets: %+v", name, on, tun)
			}
			if tun.AutoRedirect || tun.AutoRedirectInputMark != 0 || tun.AutoRedirectOutputMark != 0 ||
				tun.AutoRedirectIPRoute2FallbackRuleIndex != 0 || general.RedirPort != 0 || general.TProxyPort != 0 {
				t.Errorf("%s, TUN %v: auto-redirect %v, marks %d/%d, fallback rule %d, redir-port %d, tproxy-port %d", name, on,
					tun.AutoRedirect, tun.AutoRedirectInputMark, tun.AutoRedirectOutputMark,
					tun.AutoRedirectIPRoute2FallbackRuleIndex, general.RedirPort, general.TProxyPort)
			}
			if !tun.AutoRoute || !tun.StrictRoute {
				t.Errorf("%s, TUN %v: auto-route %v, strict-route %v", name, on, tun.AutoRoute, tun.StrictRoute)
			}
		}
	}

	// Verify checks what mihomo parsed, not what the patch wrote: each of these, slipped in
	// after the patch, fails the configuration.
	prepared, err := admission.Prepare(admission.Profile{Config: []byte(minimal), Options: admission.RunOptions{TUN: true}}, host)
	if err != nil {
		t.Fatal(err)
	}
	for _, slipped := range []map[string]any{
		{"device": "elsewhere0"},
		{"iproute2-table-index": 2022},
		{"iproute2-rule-index": 9000},
		{"include-uid": []any{0}},
		{"exclude-uid-range": []any{"1000:1999"}},
		{"include-interface": []any{"eth0"}},
		{"route-exclude-address-set": []any{"cn"}},
		{"auto-redirect-output-mark": 8228},
		{"auto-redirect-iproute2-fallback-rule-index": 32768},
		{"redir-port": 7892},
	} {
		var doc map[string]any
		if err := yaml.Unmarshal(prepared.YAML, &doc); err != nil {
			t.Fatal(err)
		}
		for key, value := range slipped {
			if key == "redir-port" {
				doc[key] = value
			} else {
				doc["tun"].(map[string]any)[key] = value
			}
		}
		raw, err := yaml.Marshal(doc)
		if err != nil {
			t.Fatal(err)
		}
		cfg, err := executor.ParseWithBytes(raw)
		if err != nil {
			t.Fatalf("%v: %v", slipped, err)
		}
		if err := admission.Verify(cfg, prepared); !errors.Is(err, ErrInvalidConfig) {
			t.Errorf("%v slipped in, and Verify = %v", slipped, err)
		}
	}

	// The engine takes pins for owned mode only, and only pins that keep the clean-up off
	// the kernel's tables and rules.
	for _, options := range []Options{
		{HomeDir: testHome, Tun: admission.TunNone, Pins: pins},
		{HomeDir: testHome, Tun: admission.TunFD, TunFD: -1, Pins: pins},
		{HomeDir: testHome, Tun: admission.TunOwned, Pins: tunclean.Pins{Device: product.LinuxTunDevice, Table: 254, RuleIndex: product.LinuxRuleIndex}},
	} {
		engine, err := New(options)
		if err == nil {
			engine.Close()
			t.Errorf("%v with %+v: New passed", options.Tun, options.Pins)
		} else if !strings.Contains(err.Error(), "pins") && !strings.Contains(err.Error(), "table 254") {
			t.Errorf("%v with %+v: refused for another reason: %v", options.Tun, options.Pins, err)
		}
	}
}
