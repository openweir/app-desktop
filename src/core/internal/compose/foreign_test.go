package compose

import (
	"slices"
	"testing"

	"github.com/metacubex/mihomo/common/yaml"
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
}

func TestNormalizeLeavesMihomoAndLinksAlone(t *testing.T) {
	for _, input := range []string{
		"proxies:\n  - {name: a, type: ss, server: s, port: 1, cipher: aes-128-gcm, password: p}\n",
		"vless://7c8c8c0e-0000-4000-8000-000000000001@example.com:443#a\n",
	} {
		out, err := Normalize([]byte(input))
		if err != nil || string(out) != input {
			t.Errorf("Normalize(%q) = %q, %v", input, out, err)
		}
	}
	out, err := Normalize([]byte(singBoxSubscription))
	if err != nil {
		t.Fatal(err)
	}
	var profile map[string]any
	if err := yaml.Unmarshal(out, &profile); err != nil {
		t.Fatalf("not YAML: %v", err)
	}
}
