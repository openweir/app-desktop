package compose

import (
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/openweir/subconv"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	"github.com/openweir/app-desktop/src/core/internal/controlpb"
)

const singBoxSubscription = `{
  "outbounds": [
    { "type": "selector", "tag": "Proxy", "outbounds": ["香港 01", "Naive"] },
    { "type": "shadowsocks", "tag": "香港 01", "server": "hk.example.com", "server_port": 8388,
      "method": "aes-128-gcm", "password": "p" },
    { "type": "naive", "tag": "Naive", "server": "n.example.com", "server_port": 443,
      "username": "u", "password": "p", "tls": { "enabled": true, "server_name": "n.example.com" } },
    { "type": "direct", "tag": "direct" }
  ],
  "route": {
    "rules": [{ "domain_suffix": ["cn"], "outbound": "direct" }],
    "final": "Proxy"
  }
}`

func TestSingBoxSubscriptionsBecomeProfiles(t *testing.T) {
	parsed, err := parseSubscription([]byte(singBoxSubscription))
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, proxy := range parsed.proxies {
		names = append(names, proxy["name"].(string))
	}
	if !slices.Equal(names, []string{"香港 01", "Naive"}) {
		t.Fatalf("proxies = %v", names)
	}
	if parsed.proxies[1]["type"] != "naive" {
		t.Fatalf("Naive converted to %v", parsed.proxies[1]["type"])
	}
	// The profile carries the subscription's own groups and rules over the base section.
	profile := parsed.profile
	rules, _ := profile["rules"].([]any)
	if len(rules) != 2 || rules[0] != "DOMAIN-SUFFIX,cn,DIRECT" || rules[1] != "MATCH,Proxy" {
		t.Fatalf("rules = %v", rules)
	}
	if groups, _ := profile["proxy-groups"].([]any); len(groups) != 1 {
		t.Fatalf("groups = %v", groups)
	}
	if _, ok := profile["sniffer"]; !ok {
		t.Fatal("the base section is missing")
	}
	if parsed.read.Format != subconv.FormatSingBox || !parsed.read.HasRules() || parsed.read.Rules != nil {
		t.Fatalf("read as %+v", parsed.read)
	}
}

// A subscription in a format OpenWeir's own clients write, with an item of each kind that
// mihomo would refuse: the rest is read, and the warnings name what was left out.
const someItemsLeftOut = `mixed-port: 7890
dns: {enable: true, ipv6: false}
proxies:
  - {name: 香港 01, type: ss, server: hk.example.com, port: 8388, cipher: aes-128-gcm, password: p}
  - {name: 日本 01, type: vmess, server: jp.example.com, port: 443}
  - {name: 美国 01, type: carrier-pigeon, server: us.example.com, port: 1}
  - {name: 新加坡 01, type: trojan, server: sg.example.com, port: 443, password: p, sni: sg.example.com}
proxy-groups:
  - {name: 节点选择, type: select, proxies: [香港 01, 日本 01, 新加坡 01, DIRECT]}
rules:
  - DOMAIN-SUFFIX,example.org,节点选择
  - USER-AGENT,Telegram*,节点选择
  - RULE-SET,missing,REJECT
  - MATCH,节点选择
`

func TestSomeItemsLeftOut(t *testing.T) {
	inspection, err := Inspect([]byte(someItemsLeftOut), "", nil)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, node := range inspection.GetNodes() {
		names = append(names, node.GetName())
	}
	if !slices.Equal(names, []string{"香港 01", "新加坡 01"}) {
		t.Fatalf("nodes = %v", names)
	}
	var codes []string
	for _, warning := range inspection.GetWarnings() {
		codes = append(codes, warning.GetCode()+" "+strings.SplitN(warning.GetDetail(), ":", 2)[0])
	}
	want := []string{
		"invalid_credential 日本 01", "unsupported_proxy 美国 01",
		"unsupported_rule USER-AGENT,Telegram*,节点选择", "unsupported_rule RULE-SET,missing,REJECT",
	}
	if !slices.Equal(codes, want) {
		t.Fatalf("warnings = %v", inspection.GetWarnings())
	}
	if !inspection.GetHasRules() || inspection.GetRuleCount() != 2 || inspection.GetGroupCount() != 1 {
		t.Fatalf("inspection = %v", inspection)
	}

	// The profile keeps its own settings, and the group the nodes that are left.
	read, err := Normalize([]byte(someItemsLeftOut), nil)
	if err != nil {
		t.Fatal(err)
	}
	if read.Profile["mixed-port"] != 7890 || read.Profile["dns"] == nil || read.Profile["sniffer"] != nil {
		t.Fatalf("profile = %v", read.Profile)
	}
	group := read.Profile["proxy-groups"].([]any)[0].(map[string]any)
	if !slices.Equal(group["proxies"].([]any), []any{"香港 01", "新加坡 01", "DIRECT"}) {
		t.Fatalf("group = %v", group)
	}
	// ConvertToPlan reads the rules as written, and says why it leaves each one out.
	if rules, _ := read.Rules.([]any); len(rules) != 4 {
		t.Fatalf("rules as written = %v", read.Rules)
	}

	// In the pool, the source brings the nodes it has.
	composed, err := Compose(Composition{
		Sources: []Source{{ID: "a", Name: "A", YAML: []byte(someItemsLeftOut)}},
		Plan:    []byte(`{"version": 1}`),
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(composed.Report.GetNodes()) != 2 {
		t.Fatalf("pool = %v", composed.Report.GetNodes())
	}
}

func TestProfilesThatOnlyRoute(t *testing.T) {
	// Every request DIRECT: no proxies, which subconv alone takes for no subscription.
	read, err := Normalize([]byte("mode: rule\nallow-lan: true\nrules:\n  - DOMAIN,a.example,REJECT\n  - MATCH,DIRECT\n"), nil)
	if err != nil {
		t.Fatal(err)
	}
	rules, _ := read.Profile["rules"].([]any)
	if !slices.Equal(rules, []any{"DOMAIN,a.example,REJECT", "MATCH,DIRECT"}) || read.Profile["allow-lan"] != true || !read.HasRules() {
		t.Fatalf("read %+v", read)
	}
	// Groups and no rules: the MATCH subconv adds is not routing of the subscription's own.
	inspection, err := Inspect([]byte("proxies:\n  - {name: a, type: ss, server: s.example, port: 1, cipher: aes-128-gcm, password: p}\n"+
		"proxy-groups:\n  - {name: P, type: select, proxies: [a]}\n"), "", nil)
	if err != nil || inspection.GetHasRules() || inspection.GetRuleCount() != 1 {
		t.Fatalf("inspection = %v, %v", inspection, err)
	}
}

func TestNotSubscriptions(t *testing.T) {
	for body, want := range map[string]string{
		"": "empty",
		"<!DOCTYPE html><html><body>Login</body></html>": "a web page",
		`{"msg": "token expired"}`:                       "token expired",
		"mode: rule\n":                                   "not a subscription",
		"\x00\x01\x02 binary":                            "",
	} {
		_, err := Normalize([]byte(body), nil)
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("Normalize(%q) = %v, want an error about %q", body, err, want)
		}
	}
}

// Subscriptions in every format subconv reads, from its own vectors (github.com/openweir/
// subconv's testdata at v0.5.0): what InspectSource answers for each is in
// testdata/formats.json, which -update rewrites. The engine's TestEveryFormatIsAdmitted
// checks that mihomo loads each.
const formatsDir, formatsPath = "testdata/formats", "testdata/formats.json"

var formats = map[string]subconv.Format{
	"clash.yaml": subconv.FormatClash, "stash.yaml": subconv.FormatStash,
	"sing-box.json": subconv.FormatSingBox, "sing-box-routing.json": subconv.FormatSingBox,
	"xray.json": subconv.FormatXray, "links.txt": subconv.FormatLinks,
	"links-base64.txt": subconv.FormatLinks, "links-messy.txt": subconv.FormatLinks,
	"surge.conf": subconv.FormatSurge, "loon.conf": subconv.FormatLoon,
	"shadowrocket.conf": subconv.FormatShadowrocket, "quantumultx.conf": subconv.FormatQuantumultX,
	"sip008.json": subconv.FormatSIP008, "ssd.txt": subconv.FormatSSD,
}

func TestFormats(t *testing.T) {
	entries, err := os.ReadDir(formatsDir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != len(formats) {
		t.Fatalf("%d samples, %d formats", len(entries), len(formats))
	}
	want := map[string]json.RawMessage{}
	if raw, err := os.ReadFile(formatsPath); err == nil || !*update {
		if err := json.Unmarshal(raw, &want); err != nil {
			t.Fatal(err)
		}
	}
	got := map[string]json.RawMessage{}
	for _, entry := range entries {
		name := entry.Name()
		data, err := os.ReadFile(filepath.Join(formatsDir, name))
		if err != nil {
			t.Fatal(err)
		}
		read, err := Normalize(data, nil)
		if err != nil {
			t.Errorf("%s: %v", name, err)
			continue
		}
		if read.Format != formats[name] {
			t.Errorf("%s: read as %s, want %s", name, read.Format, formats[name])
		}
		inspection, err := Inspect(data, "", nil)
		if err != nil {
			t.Errorf("%s: %v", name, err)
			continue
		}
		if len(inspection.GetNodes()) == 0 {
			t.Errorf("%s: no nodes", name)
		}
		if got[name], err = (protojson.MarshalOptions{UseProtoNames: true}).Marshal(inspection); err != nil {
			t.Fatal(err)
		}
		if *update {
			continue
		}
		var expected controlpb.InspectSourceResponse
		if err := protojson.Unmarshal(want[name], &expected); err != nil {
			t.Errorf("%s: %v (go test ./internal/compose -run TestFormats -update)", name, err)
			continue
		}
		if !proto.Equal(inspection, &expected) {
			t.Errorf("%s: the core answers\n%s\nthe vector has\n%s\n(go test ./internal/compose -run TestFormats -update rewrites it)",
				name, protojson.Format(inspection), protojson.Format(&expected))
		}
	}
	if *update {
		out, err := json.MarshalIndent(got, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(formatsPath, append(out, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}
