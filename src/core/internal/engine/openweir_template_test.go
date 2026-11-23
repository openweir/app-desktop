package engine

// api-edge's OpenWeir template (testdata/openweir-template.yaml, docs/accounts.md §5.4): an
// OpenWeir provider serves it to the app's `OpenWeir/<version> (<platform>) clash.meta` (D48),
// on a subscription link and on the signed-in config endpoint alike. These tests hold the core
// to admitting it whole, so that a change on either side that breaks provider subscriptions
// fails here. The names start with TestFork so that the native-tag CI builds, which have
// Naive, run them too.

import (
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"

	"github.com/metacubex/mihomo/config"
	C "github.com/metacubex/mihomo/constant"
	"github.com/metacubex/mihomo/constant/features"
	"github.com/openweir/subconv/rules"

	"github.com/openweir/app-desktop/src/core/internal/admission"
	"github.com/openweir/app-desktop/src/core/internal/compose"
	"github.com/openweir/app-desktop/src/core/internal/routingtemplate"
)

const (
	openweirTemplatePath = "../../../../testdata/openweir-template.yaml"
	// The template's built-in routing: api-edge's resources/install/client-routing.json.
	openweirGroups    = 28
	openweirProviders = 67
	openweirRules     = 69
	// The template's mixed port; the app's setting takes its place.
	openweirMixedPort = 9347
)

type openweirNode struct {
	name   string
	kind   C.AdapterType
	region string
}

// openweirNodes are the template's placeholder nodes in its order, one for every shape
// node.openweir writes.
var openweirNodes = []openweirNode{
	{"日本 01", C.Vless, "JP"}, {"日本 02", C.Vmess, "JP"}, {"日本 03", C.Trojan, "JP"}, {"日本 04", C.Vless, "JP"},
	{"香港 01", C.Shadowsocks, "HK"}, {"香港 02", C.ShadowsocksR, "HK"}, {"香港 03", C.Hysteria2, "HK"},
	{"新加坡 01", C.Tuic, "SG"}, {"新加坡 02", C.AnyTLS, "SG"}, {"新加坡 03", C.AnyTLS, "SG"},
	{"美国 01", C.Naive, "US"}, {"美国 02", C.Naive, "US"}, {"美国 03", C.Nowhere, "US"},
	{"德国 01", C.Mieru, "DE"}, {"德国 02", C.Snell, "DE"}, {"德国 03", C.Snell, "DE"},
	{"英国 01", C.Socks5, "GB"}, {"英国 02", C.Http, "GB"}, {"英国 03", C.Http, "GB"},
}

var naiveLine = regexp.MustCompile(`(?m)^  - \{"name":"([^"]+)",[^\n]*"type":"naive"[^\n]*\n`)

// openweirTemplate reads the template, and the nodes of it this build runs. Ordinary test
// builds lack Naive, whose nodes mihomo refuses there, so they are taken out with their group
// memberships; the native-tag CI builds and the release builds keep them.
func openweirTemplate(t *testing.T) (source []byte, nodes []openweirNode) {
	t.Helper()
	data, err := os.ReadFile(openweirTemplatePath)
	if err != nil {
		t.Fatal(err)
	}
	if features.WithNaive {
		return data, openweirNodes
	}
	text := string(data)
	var removed []string
	for _, match := range naiveLine.FindAllStringSubmatch(text, -1) {
		name := match[1]
		text = strings.Replace(text, match[0], "", 1)
		text = strings.ReplaceAll(text, `,"`+name+`"`, "")
		if strings.Contains(text, `"`+name+`"`) {
			t.Fatalf("%s is still named after it was taken out", name)
		}
		removed = append(removed, name)
	}
	for _, node := range openweirNodes {
		if node.kind != C.Naive {
			nodes = append(nodes, node)
		} else if !slices.Contains(removed, node.name) {
			t.Fatalf("Naive node %s was not taken out", node.name)
		}
	}
	t.Logf("this build has no Naive: the template without %v", removed)
	return []byte(text), nodes
}

func nodeNames(nodes []openweirNode) []string {
	var names []string
	for _, node := range nodes {
		names = append(names, node.name)
	}
	return names
}

// The core reads the template as it is, through subconv (D54): no node, group, rule or rule
// provider left out or renamed, and its own routing, not the routing template.
func TestForkOpenWeirTemplateReadsWhole(t *testing.T) {
	source, nodes := openweirTemplate(t)
	read, err := compose.Normalize(source, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(read.Warnings) != 0 {
		t.Fatalf("warnings: %v", read.Warnings)
	}
	if read.Template != nil || read.Base || !read.HasRules() {
		t.Fatalf("template %v, base %v, has rules %v", read.Template, read.Base, read.HasRules())
	}
	if read.Stats.Total != openweirRules || read.Stats.Dropped != 0 || !read.Stats.Match {
		t.Fatalf("rule stats = %+v", read.Stats)
	}
	profile := read.Profile
	for key, want := range map[string]int{
		"proxies": len(nodes), "proxy-groups": openweirGroups, "rules": openweirRules,
	} {
		if got := len(profile[key].([]any)); got != want {
			t.Errorf("%d %s, want %d", got, key, want)
		}
	}
	if got := len(profile["rule-providers"].(map[string]any)); got != openweirProviders {
		t.Errorf("%d rule providers, want %d", got, openweirProviders)
	}
	// What subconv does not convert stays the template's own.
	if profile["mixed-port"] != openweirMixedPort || profile["geodata-mode"] != true {
		t.Errorf("mixed-port %v, geodata-mode %v", profile["mixed-port"], profile["geodata-mode"])
	}
}

// openweirCase is one way a host runs the template.
type openweirCase struct {
	name      string
	mode      admission.TunMode
	options   admission.RunOptions
	overrides [][]byte
	// port is the mixed port the configuration ends up with.
	port int
	// extraRules come from the overrides, ahead of the template's.
	extraRules int
}

func TestForkOpenWeirTemplateIsAdmitted(t *testing.T) {
	extensionHasIPv6(t)
	source, nodes := openweirTemplate(t)
	// Overrides as the UI passes them, the global one first: a port of their own, which the
	// app's setting still beats, and a rule.
	overrides := [][]byte{
		[]byte("mixed-port: 7891\nlog-level: warning\ndns: {nameserver: [https://1.1.1.1/dns-query]}\n"),
		[]byte("prepend-rules: ['DOMAIN-SUFFIX,corp.example,DIRECT']\n"),
	}
	app := uint16(17890)
	for _, c := range []openweirCase{
		{name: "no TUN host", mode: admission.TunNone, port: openweirMixedPort},
		{name: "no TUN host, the app's port", mode: admission.TunNone, options: admission.RunOptions{MixedPort: app}, port: 17890},
		{name: "TUN off", mode: admission.TunOwned, options: admission.RunOptions{MixedPort: app}, port: 17890},
		{name: "TUN on", mode: admission.TunOwned, options: admission.RunOptions{TUN: true, MixedPort: app}, port: 17890},
		{name: "TUN on, LAN", mode: admission.TunOwned, options: admission.RunOptions{TUN: true, MixedPort: app, AllowLAN: true}, port: 17890},
		{name: "fd TUN", mode: admission.TunFD, options: admission.RunOptions{MixedPort: app}, port: 17890},
		{name: "overrides", mode: admission.TunOwned, options: admission.RunOptions{TUN: true, MixedPort: app},
			overrides: overrides, port: 17890, extraRules: 1},
		{name: "overrides, TUN off", mode: admission.TunOwned, options: admission.RunOptions{MixedPort: app},
			overrides: overrides, port: 17890, extraRules: 1},
		// Without the app's port, the override's wins over the template's.
		{name: "overrides, no app port", mode: admission.TunNone, overrides: overrides, port: 7891, extraRules: 1},
	} {
		t.Run(c.name, func(t *testing.T) {
			profile := admission.Profile{Config: source, Overrides: c.overrides, Options: c.options}
			admitted, err := admitProfile(profile, admission.Host{Tun: c.mode})
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { closeProxies(admitted.cfg) })
			checkOpenWeirTemplate(t, c, admitted.cfg, nodes)
		})
	}
}

func closeProxies(cfg *config.Config) {
	for _, proxy := range cfg.Proxies {
		_ = proxy.Close()
	}
}

func checkOpenWeirTemplate(t *testing.T, c openweirCase, cfg *config.Config, nodes []openweirNode) {
	t.Helper()
	for _, node := range nodes {
		if proxy := cfg.Proxies[node.name]; proxy == nil || proxy.Type() != node.kind {
			t.Errorf("node %s = %v, want %v", node.name, proxy, node.kind)
		}
	}
	groups := 0
	for _, proxy := range cfg.Proxies {
		if _, ok := groupOf(proxy); ok {
			groups++
		}
	}
	if groups != openweirGroups+1 { // and mihomo's GLOBAL
		t.Errorf("%d groups, want %d and GLOBAL", groups, openweirGroups)
	}
	names := nodeNames(nodes)
	for group, head := range map[string][]string{
		"Proxy": {"Auto - UrlTest", "DIRECT"}, "Auto - UrlTest": nil,
		"AdBlock": {"REJECT", "DIRECT", "Proxy"}, "Others": {"Proxy", "DIRECT"},
	} {
		if got := members(t, cfg, group); !slices.Equal(got, append(head, names...)) {
			t.Errorf("%s holds %v", group, got)
		}
	}
	if auto := cfg.Proxies["Auto - UrlTest"]; auto.Type() != C.URLTest {
		t.Errorf("Auto - UrlTest is a %v", auto.Type())
	}
	if len(cfg.RuleProviders) != openweirProviders {
		t.Errorf("%d rule providers, want %d", len(cfg.RuleProviders), openweirProviders)
	}
	rules := cfg.Rules
	if len(rules) != openweirRules+c.extraRules {
		t.Fatalf("%d rules, want %d", len(rules), openweirRules+c.extraRules)
	}
	first, last := rules[c.extraRules], rules[len(rules)-1]
	if first.RuleType() != C.RuleSet || first.Payload() != "adblock" || first.Adapter() != "AdBlock" {
		t.Errorf("first rule %v,%s,%s", first.RuleType(), first.Payload(), first.Adapter())
	}
	if last.RuleType() != C.MATCH || last.Adapter() != "Others" {
		t.Errorf("last rule %v,%s", last.RuleType(), last.Adapter())
	}

	general := cfg.General
	// The app's mixed port takes the place of the template's 9347, and the mixed port is the
	// only inbound: nothing else listens, on 9347 or anywhere.
	if general.MixedPort != c.port {
		t.Errorf("mixed-port = %d, want %d", general.MixedPort, c.port)
	}
	if general.Port != 0 || general.SocksPort != 0 || general.RedirPort != 0 || general.TProxyPort != 0 || len(cfg.Listeners) != 0 {
		t.Errorf("other inbounds: port %d socks %d redir %d tproxy %d listeners %v",
			general.Port, general.SocksPort, general.RedirPort, general.TProxyPort, cfg.Listeners)
	}
	tun := c.options.TUN || c.mode == admission.TunFD
	if general.Tun.Enable != tun {
		t.Errorf("tun.enable = %v, want %v", general.Tun.Enable, tun)
	}
	if general.AllowLan != c.options.AllowLAN {
		t.Errorf("allow-lan = %v, want %v", general.AllowLan, c.options.AllowLAN)
	}
	// The template's DNS, sniffing and profile settings stay, but for what fd mode pins.
	dns := cfg.DNS
	if !dns.Enable || dns.EnhancedMode != C.DNSFakeIP || len(dns.NameServerPolicy) != 1 {
		t.Errorf("dns enable %v mode %v policy %v", dns.Enable, dns.EnhancedMode, dns.NameServerPolicy)
	}
	if c.mode != admission.TunFD && dns.FakeIPRange.String() != "198.18.0.1/16" {
		t.Errorf("fake-ip-range = %s", dns.FakeIPRange)
	}
	if !cfg.Sniffer.Enable || !cfg.Profile.StoreSelected || !cfg.Profile.StoreFakeIP {
		t.Errorf("sniffer %v, profile %+v", cfg.Sniffer.Enable, cfg.Profile)
	}
}

// With a routing template at hand, as the engine always has one, the template is inspected as
// the UI shows it, and keeps its own routing when it runs.
func TestForkOpenWeirTemplateKeepsItsRouting(t *testing.T) {
	source, nodes := openweirTemplate(t)
	server := serveTemplate(t)
	t.Cleanup(func() { os.RemoveAll(filepath.Join(testHome, routingtemplate.Dir)) })
	engine, err := New(Options{
		HomeDir: testHome, Tun: admission.TunNone,
		TemplateHosts: []rules.Host{{Base: server.URL, Operator: "test"}}, TemplateHTTP: server.Client(),
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { engine.Close() })

	// Reading needs no Naive: the inspection reads the whole template in every build.
	whole, err := os.ReadFile(openweirTemplatePath)
	if err != nil {
		t.Fatal(err)
	}
	inspection, err := engine.InspectSource(whole, "剩余|到期")
	if err != nil {
		t.Fatal(err)
	}
	if !inspection.GetHasRules() || inspection.GetGroupCount() != openweirGroups ||
		inspection.GetRuleCount() != openweirRules || len(inspection.GetWarnings()) != 0 {
		t.Fatalf("inspection: has rules %v, %d groups, %d rules, warnings %v", inspection.GetHasRules(),
			inspection.GetGroupCount(), inspection.GetRuleCount(), inspection.GetWarnings())
	}
	seen := map[string]bool{}
	for _, node := range inspection.GetNodes() {
		seen[node.GetName()] = true
		if node.GetInfo() || node.GetMesh() {
			t.Errorf("%s: info %v, mesh %v", node.GetName(), node.GetInfo(), node.GetMesh())
		}
	}
	for _, node := range openweirNodes {
		if !seen[node.name] {
			t.Errorf("the inspection lacks %s", node.name)
		}
	}
	for _, node := range inspection.GetNodes() {
		i := slices.IndexFunc(openweirNodes, func(n openweirNode) bool { return n.name == node.GetName() })
		if i < 0 || node.GetType() != openweirNodes[i].kind.String() || node.GetRegion() != openweirNodes[i].region {
			t.Errorf("node %s: %s in %s", node.GetName(), node.GetType(), node.GetRegion())
		}
	}

	admitted, err := engine.admit(admission.Profile{Config: source, Options: admission.RunOptions{MixedPort: 17890}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { closeProxies(admitted.cfg) })
	checkOpenWeirTemplate(t, openweirCase{port: 17890}, admitted.cfg, nodes)
}

// With "my plan" (docs/routing.md §8) the template's nodes go to the pool, all of them.
func TestForkOpenWeirTemplateFeedsThePool(t *testing.T) {
	source, nodes := openweirTemplate(t)
	admitted, err := admitProfile(admission.Profile{Composition: &compose.Composition{
		Sources:    []compose.Source{{ID: "provider", Name: "Example Provider", YAML: source}},
		InfoFilter: "剩余|到期",
		Plan:       []byte(`{"version": 1}`),
	}}, noTUN)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { closeProxies(admitted.cfg) })
	var pool []string
	for _, node := range admitted.report.GetNodes() {
		i := slices.IndexFunc(nodes, func(n openweirNode) bool { return n.name == node.GetOriginalName() })
		if i < 0 || node.GetType() != nodes[i].kind.String() || node.GetRegion() != nodes[i].region {
			t.Errorf("pool node %s: %s in %s", node.GetOriginalName(), node.GetType(), node.GetRegion())
		}
		pool = append(pool, node.GetName())
	}
	if !slices.Equal(pool, nodeNames(nodes)) {
		t.Errorf("pool = %v", pool)
	}
	if forkNodeNamed(admitted.cfg, "新加坡 03") == nil {
		t.Error("AnyTLS with REALITY is not in the composed configuration")
	}
}

// forkNodeNamed finds a node of a composed configuration, which keeps them in providers.
func forkNodeNamed(cfg *config.Config, name string) C.Proxy {
	for _, provider := range cfg.Providers {
		for _, proxy := range provider.Proxies() {
			if proxy.Name() == name {
				return proxy
			}
		}
	}
	return nil
}
