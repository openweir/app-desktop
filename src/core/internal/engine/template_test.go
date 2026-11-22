package engine

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/metacubex/mihomo/constant/features"
	"github.com/openweir/subconv/rules"

	"github.com/openweir/app-desktop/src/core/internal/admission"
	"github.com/openweir/app-desktop/src/core/internal/routingtemplate"
)

// Every format subconv reads becomes a profile mihomo loads (D54): the samples of
// compose's testdata/formats, admitted as a subscription's own rules. They carry the fork's
// protocols, Naive among them, so the native-tag CI build runs this too (TestFork*).
func TestForkEveryFormatIsAdmitted(t *testing.T) {
	const dir = "../compose/testdata/formats"
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		data, err := os.ReadFile(filepath.Join(dir, entry.Name()))
		if err != nil {
			t.Fatal(err)
		}
		admitted, err := admitProfile(admission.Profile{Config: data}, noTUN)
		switch {
		case err != nil && !features.WithNaive && strings.Contains(err.Error(), "naive support is not included"):
			// The native-tag CI build loads Naive nodes; ordinary test builds have none.
			t.Logf("%s: %v", entry.Name(), err)
			continue
		case err != nil && strings.Contains(err.Error(), "not found in GeoSite.dat"):
			// The test home's GEO data has a few categories; the bundled data has them all.
			t.Logf("%s: %v", entry.Name(), err)
			continue
		case err != nil:
			t.Errorf("%s: %v", entry.Name(), err)
			continue
		}
		if len(admitted.cfg.Proxies) <= len([]string{"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"}) {
			t.Errorf("%s: no nodes in %v", entry.Name(), admitted.cfg.Proxies)
		}
	}
}

// A routing template as the rules branch serves it, without rule providers, which mihomo
// would download.
const routingTemplate = `proxy-groups:
  - name: "🚀 Proxy"
    type: select
    proxies: [DIRECT]
    include-all: true
  - name: "🎯 Domestic"
    type: select
    proxies: [DIRECT, "🚀 Proxy"]
rules:
  - "DOMAIN-SUFFIX,cn.example,🎯 Domestic"
  - "MATCH,🚀 Proxy"
`

// serveTemplate serves a rules branch with routingTemplate.
func serveTemplate(t *testing.T) *httptest.Server {
	t.Helper()
	sum := sha256.Sum256([]byte(routingTemplate))
	manifest, err := json.Marshal(rules.Manifest{Version: 42, Files: []rules.File{{
		Path: rules.TemplatePath, Size: int64(len(routingTemplate)), SHA256: hex.EncodeToString(sum[:]),
		Updated: "2026-11-22", Kind: "template",
	}}})
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/" + rules.ManifestPath:
			w.Write(manifest)
		case "/" + rules.TemplatePath:
			w.Write([]byte(routingTemplate))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	return server
}

// A subscription without rules of its own routes by subconv's routing template (D54): the
// engine gets it as it inspects a subscription, keeps it in the home, and reads it from there
// when it admits the subscription, with no host answering.
func TestRoutingTemplate(t *testing.T) {
	server := serveTemplate(t)
	cache := filepath.Join(testHome, routingtemplate.Dir)
	t.Cleanup(func() { os.RemoveAll(cache) })
	engine, err := New(Options{
		HomeDir: testHome, Tun: admission.TunNone,
		TemplateHosts: []rules.Host{{Base: server.URL, Operator: "test"}}, TemplateHTTP: server.Client(),
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { engine.Close() })

	const links = "ss://YWVzLTEyOC1nY206cA@hk.example.com:8388#HK%2001\n" +
		"ss://YWVzLTEyOC1nY206cA@jp.example.com:8388#JP%2001\n"
	inspection, err := engine.InspectSource([]byte(links), "")
	if err != nil {
		t.Fatal(err)
	}
	var codes []string
	for _, warning := range inspection.GetWarnings() {
		codes = append(codes, warning.GetCode()+" "+warning.GetDetail())
	}
	if !inspection.GetHasRules() || inspection.GetGroupCount() != 2 || inspection.GetRuleCount() != 2 ||
		!slices.Equal(codes, []string{"template_applied no_rules"}) {
		t.Fatalf("inspection = %v", inspection)
	}
	if _, err := os.Stat(filepath.Join(cache, rules.TemplatePath)); err != nil {
		t.Fatalf("the template is not kept in the home: %v", err)
	}

	server.Close()
	admitted, err := engine.admit(admission.Profile{Config: []byte(links)})
	if err != nil {
		t.Fatal(err)
	}
	if len(admitted.cfg.Rules) != 2 || admitted.cfg.Rules[0].Payload() != "cn.example" ||
		admitted.cfg.Proxies["🚀 Proxy"] == nil || admitted.cfg.Proxies["HK 01"] == nil {
		t.Fatalf("rules %v, proxies %v", admitted.cfg.Rules, admitted.cfg.Proxies)
	}
	// Rules of its own, all of which convert, stay.
	own := "proxies:\n  - {name: a, type: ss, server: a.example, port: 1, cipher: aes-128-gcm, password: p}\n" +
		"proxy-groups:\n  - {name: P, type: select, proxies: [a]}\nrules:\n  - DOMAIN,a.example,P\n  - MATCH,P\n"
	inspection, err = engine.InspectSource([]byte(own), "")
	if err != nil || len(inspection.GetWarnings()) != 0 || inspection.GetGroupCount() != 1 || inspection.GetRuleCount() != 2 {
		t.Fatalf("own rules: %v, %v", inspection, err)
	}
}
