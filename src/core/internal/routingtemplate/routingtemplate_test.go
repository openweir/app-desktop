package routingtemplate

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"

	"github.com/openweir/subconv/rules"
)

const template = `proxy-groups:
  - {name: Proxy, type: select, proxies: [DIRECT], include-all: true}
rules:
  - MATCH,Proxy
`

// branch serves a rules branch with template, counting the requests; down makes it fail.
func branch(t *testing.T) (*httptest.Server, *atomic.Int32, *atomic.Bool) {
	t.Helper()
	sum := sha256.Sum256([]byte(template))
	manifest, err := json.Marshal(rules.Manifest{Version: 7, Files: []rules.File{{
		Path: rules.TemplatePath, Size: int64(len(template)), SHA256: hex.EncodeToString(sum[:]),
		Updated: "2026-11-22", Kind: "template",
	}}})
	if err != nil {
		t.Fatal(err)
	}
	var requests atomic.Int32
	var down atomic.Bool
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		switch {
		case down.Load():
			w.WriteHeader(http.StatusServiceUnavailable)
		case r.URL.Path == "/"+rules.ManifestPath:
			w.Write(manifest)
		case r.URL.Path == "/"+rules.TemplatePath:
			w.Write([]byte(template))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	return server, &requests, &down
}

func TestRefreshKeepsTheTemplateAndReadsItOffline(t *testing.T) {
	server, requests, _ := branch(t)
	dir := filepath.Join(t.TempDir(), Dir)
	hosts := []rules.Host{{Base: server.URL, Operator: "test"}}
	source := New(dir, hosts, server.Client())

	// Before a refresh the client has no template, and asks no host.
	if _, err := source.Client().Template(context.Background()); err == nil || requests.Load() != 0 {
		t.Fatalf("template before a refresh: %v, %d requests", err, requests.Load())
	}
	source.Refresh()
	asked := requests.Load()
	if asked == 0 {
		t.Fatal("the refresh asked no host")
	}
	got, err := source.Client().Template(context.Background())
	if err != nil || string(got.YAML) != template || got.Origin != rules.FromCache || got.Base != server.URL {
		t.Fatalf("template = %+v, %v", got, err)
	}
	if requests.Load() != asked {
		t.Fatal("the client went to the network")
	}
	// A second refresh within RefreshEvery asks nothing.
	source.Refresh()
	if requests.Load() != asked {
		t.Fatal("refreshed again at once")
	}
	// A new Source on the same directory, as after a restart, has the template.
	if got, err := New(dir, hosts, server.Client()).Client().Template(context.Background()); err != nil || got.Version != 7 {
		t.Fatalf("after a restart: %+v, %v", got, err)
	}
	var none *Source
	if none.Client() != nil {
		t.Fatal("a nil source has a client")
	}
	none.Refresh()
}

func TestAFailedRefreshWaitsBeforeTheNext(t *testing.T) {
	server, requests, down := branch(t)
	down.Store(true)
	source := New(t.TempDir(), []rules.Host{{Base: server.URL, Operator: "test"}}, server.Client())
	source.Refresh()
	asked := requests.Load()
	source.Refresh()
	if asked == 0 || requests.Load() != asked {
		t.Fatalf("%d then %d requests", asked, requests.Load())
	}
}

func TestTheCacheStaysInItsDirectory(t *testing.T) {
	dir := t.TempDir()
	cache := &dirCache{dir: filepath.Join(dir, "cache")}
	cache.Put("../outside", []byte("x"))
	cache.Put("/etc/x", []byte("x"))
	if _, err := os.Stat(filepath.Join(dir, "outside")); err == nil {
		t.Fatal("a key wrote outside the cache")
	}
	cache.Put(rules.ManifestPath, []byte("m"))
	if data, ok := cache.Get(rules.ManifestPath); !ok || string(data) != "m" {
		t.Fatalf("get = %q, %v", data, ok)
	}
	if _, ok := cache.Get("../cache/" + rules.ManifestPath); ok {
		t.Fatal("read through a key outside the cache")
	}
}
