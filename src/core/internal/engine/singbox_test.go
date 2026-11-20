package engine

import (
	"testing"

	"github.com/openweir/app-desktop/src/core/internal/admission"
)

// A sing-box subscription, converted by github.com/openweir/subconv, must be a profile mihomo
// itself accepts: parsed by executor.ParseWithBytes and verified by the admission pipeline.
const singBoxProfile = `{
  "outbounds": [
    { "type": "selector", "tag": "Proxy", "outbounds": ["Auto", "SS", "VLESS REALITY", "Trojan WS", "direct"] },
    { "type": "urltest", "tag": "Auto", "outbounds": ["SS", "Hy2", "TUIC"] },
    { "type": "shadowsocks", "tag": "SS", "server": "127.0.0.1", "server_port": 8388, "method": "aes-128-gcm", "password": "p",
      "multiplex": { "enabled": true, "protocol": "h2mux", "max_connections": 4 } },
    { "type": "vless", "tag": "VLESS REALITY", "server": "127.0.0.1", "server_port": 443, "uuid": "7c8c8c0e-0000-4000-8000-000000000002",
      "flow": "xtls-rprx-vision", "packet_encoding": "xudp",
      "tls": { "enabled": true, "server_name": "www.microsoft.com", "reality": { "enabled": true, "public_key": "SGVsbG9Xb3JsZEhlbGxvV29ybGRIZWxsb1dvcmxkSGk", "short_id": "abcd" } } },
    { "type": "trojan", "tag": "Trojan WS", "server": "127.0.0.1", "server_port": 443, "password": "pw",
      "tls": { "enabled": true, "server_name": "t.example.com" }, "transport": { "type": "ws", "path": "/ws" } },
    { "type": "hysteria2", "tag": "Hy2", "server": "127.0.0.1", "server_port": 443, "password": "hy",
      "obfs": { "type": "salamander", "password": "ob" }, "tls": { "enabled": true, "server_name": "h.example.com" } },
    { "type": "tuic", "tag": "TUIC", "server": "127.0.0.1", "server_port": 443, "uuid": "7c8c8c0e-0000-4000-8000-000000000004",
      "password": "tu", "congestion_control": "bbr", "tls": { "enabled": true, "server_name": "u.example.com", "alpn": ["h3"] } },
    { "type": "direct", "tag": "direct" },
    { "type": "block", "tag": "block" }
  ],
  "route": {
    "rule_set": [
      { "tag": "geosite-netflix", "type": "remote", "format": "binary", "url": "https://example.com/geosite-netflix.srs" },
      { "tag": "lan", "type": "inline", "rules": [{ "ip_cidr": ["192.168.0.0/16"] }] }
    ],
    "rules": [
      { "action": "sniff" },
      { "domain_suffix": ["example.org"], "action": "reject" },
      { "rule_set": "geosite-netflix", "outbound": "Proxy" },
      { "rule_set": "lan", "outbound": "direct" },
      { "type": "logical", "mode": "or", "rules": [{ "port": 22 }, { "port_range": ["6000:6100"] }], "outbound": "direct" }
    ],
    "final": "Proxy"
  }
}`

func TestSingBoxSubscriptionsAreAdmitted(t *testing.T) {
	admitted, err := admitProfile(admission.Profile{Config: []byte(singBoxProfile)}, admission.Host{Tun: admission.TunNone})
	if err != nil {
		t.Fatal(err)
	}
	cfg := admitted.cfg
	for _, name := range []string{"SS", "VLESS REALITY", "Trojan WS", "Hy2", "TUIC", "Proxy", "Auto"} {
		if _, ok := cfg.Proxies[name]; !ok {
			t.Errorf("%s is missing from the parsed config", name)
		}
	}
	if got := len(cfg.Rules); got != 5 {
		t.Errorf("%d rules, want 5 (four and MATCH)", got)
	}
	if _, ok := cfg.RuleProviders["geosite-netflix"]; !ok {
		t.Error("the geosite rule provider is missing")
	}
}
