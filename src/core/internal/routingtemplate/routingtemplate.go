// Package routingtemplate keeps subconv's routing template (D54) for subscriptions whose own
// routing is missing or too lossy: the template of github.com/openweir/subconv's rules branch,
// which jsDelivr-compatible CDNs and GitHub serve (subconv's docs/rules.md).
//
// The template is fetched apart from reading subscriptions. Refresh gets the latest version
// into a cache in the core's home, at most every RefreshEvery and within RefreshTimeout; Client
// is what subconv.Build gets, and it reads that cache only. So reading a subscription never
// waits for the network, and every read between two refreshes gives the same routing.
package routingtemplate

import (
	"context"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/openweir/subconv/rules"
)

const (
	// RefreshEvery is how often Refresh asks the hosts at most; the branch is rebuilt daily.
	RefreshEvery = 6 * time.Hour
	// RetryEvery is how soon it asks again after no host served the template.
	RetryEvery = 10 * time.Minute
	// RefreshTimeout bounds one refresh, within the budget of the request that runs it.
	RefreshTimeout = 15 * time.Second
	// Dir is where the cache lives in the core's home.
	Dir = "routing-template"
)

// Source gets the template from its hosts and keeps the last good one. A nil Source has none.
type Source struct {
	cache   *dirCache
	online  *rules.Client
	offline *rules.Client

	mu sync.Mutex
	// next is when Refresh asks the hosts again.
	next time.Time
}

// New keeps the template in dir, from hosts through client (nil: a client of its own).
func New(dir string, hosts []rules.Host, client *http.Client) *Source {
	if client == nil {
		client = &http.Client{}
	}
	cache := &dirCache{dir: dir}
	return &Source{
		cache:  cache,
		online: &rules.Client{HTTP: client, Hosts: hosts, Cache: cache},
		// The same hosts, for the URL the template's rule providers get, and no network.
		offline: &rules.Client{HTTP: &http.Client{Transport: offline{}}, Hosts: hosts, Cache: cache},
	}
}

// Client reads the cached template only; nil for a nil Source, which reads no template.
func (s *Source) Client() *rules.Client {
	if s == nil {
		return nil
	}
	return s.offline
}

// Refresh gets the latest template into the cache, unless it did less than RefreshEvery ago,
// or tried less than RetryEvery ago and no host served one. Without an answer the cache stays
// as it was.
func (s *Source) Refresh() {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if time.Now().Before(s.next) {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), RefreshTimeout)
	defer cancel()
	s.next = time.Now().Add(RetryEvery)
	if t, err := s.online.Template(ctx); err == nil && t.Origin == rules.FromNetwork {
		s.next = time.Now().Add(RefreshEvery)
	}
}

var errOffline = errors.New("the routing template is read from the cache only")

// offline fails every request at once, so that a rules.Client falls back to its cache.
type offline struct{}

func (offline) RoundTrip(*http.Request) (*http.Response, error) { return nil, errOffline }

// dirCache is a rules.Cache of files in dir: the manifest, the template and the floor.
type dirCache struct {
	dir string
	mu  sync.Mutex
}

func (c *dirCache) Get(key string) ([]byte, bool) {
	if !filepath.IsLocal(key) {
		return nil, false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	data, err := os.ReadFile(filepath.Join(c.dir, filepath.FromSlash(key)))
	return data, err == nil
}

// Put writes a value whole or not at all; one it cannot store is dropped.
func (c *dirCache) Put(key string, value []byte) {
	if !filepath.IsLocal(key) {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	path := filepath.Join(c.dir, filepath.FromSlash(key))
	if os.MkdirAll(filepath.Dir(path), 0o755) != nil {
		return
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".put-*")
	if err != nil {
		return
	}
	_, err = file.Write(value)
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(file.Name(), path)
	}
	if err != nil {
		os.Remove(file.Name())
	}
}
