package server

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/openweir/app-desktop/src/core/internal/frame"
	"github.com/openweir/app-desktop/src/core/internal/peer"
)

type echo struct{}

func (echo) Handle(_ context.Context, caller peer.Identity, payload []byte) []byte {
	return append([]byte(caller.UserID+":"), payload...)
}

func start(t *testing.T, verify peer.Verifier) (string, context.CancelFunc, <-chan error) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	go func() { done <- Serve(ctx, listener, verify, echo{}, logger) }()
	return listener.Addr().String(), cancel, done
}

func dial(t *testing.T, address string) net.Conn {
	t.Helper()
	conn, err := net.Dial("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	conn.SetDeadline(time.Now().Add(5 * time.Second))
	return conn
}

func TestServesConcurrentRequestsOnOneConnection(t *testing.T) {
	address, cancel, done := start(t, func(conn net.Conn) (net.Conn, peer.Identity, error) {
		return conn, peer.Identity{UserID: "user"}, nil
	})
	conn := dial(t, address)
	for _, payload := range []string{"a", "b", "c"} {
		if err := frame.Write(conn, frame.KindRequest, []byte(payload)); err != nil {
			t.Fatal(err)
		}
	}
	var got []string
	for range 3 {
		response, err := frame.Read(conn, frame.KindResponse)
		if err != nil {
			t.Fatal(err)
		}
		got = append(got, string(response))
	}
	sort.Strings(got)
	if want := []string{"user:a", "user:b", "user:c"}; !equal(got, want) {
		t.Fatalf("responses = %v, want %v", got, want)
	}

	cancel()
	if err := <-done; err != nil {
		t.Fatalf("Serve returned %v after cancellation", err)
	}
}

func TestRejectedClientIsDisconnected(t *testing.T) {
	address, cancel, done := start(t, func(net.Conn) (net.Conn, peer.Identity, error) {
		return nil, peer.Identity{}, errors.New("not the UI")
	})
	defer func() { cancel(); <-done }()
	conn := dial(t, address)
	if _, err := conn.Read(make([]byte, 1)); !errors.Is(err, io.EOF) {
		t.Fatalf("read after rejection = %v, want EOF", err)
	}
}

func TestBadHeaderClosesConnection(t *testing.T) {
	address, cancel, done := start(t, func(conn net.Conn) (net.Conn, peer.Identity, error) {
		return conn, peer.Identity{UserID: "user"}, nil
	})
	defer func() { cancel(); <-done }()
	conn := dial(t, address)
	if _, err := conn.Write(bytes.Repeat([]byte{'x'}, frame.HeaderSize)); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.Read(make([]byte, 1)); !errors.Is(err, io.EOF) {
		t.Fatalf("read after bad header = %v, want EOF", err)
	}
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

type waitingHandler struct{ entered, cancelled chan struct{} }

func (h waitingHandler) Handle(ctx context.Context, _ peer.Identity, _ []byte) []byte {
	close(h.entered)
	<-ctx.Done()
	close(h.cancelled)
	return []byte("cancelled")
}

func TestClientEOFImmediatelyCancelsQueuedWork(t *testing.T) {
	client, server := net.Pipe()
	defer client.Close()
	handler := waitingHandler{make(chan struct{}), make(chan struct{})}
	done := make(chan struct{})
	go func() {
		defer close(done)
		s := newServer(func(conn net.Conn) (net.Conn, peer.Identity, error) {
			return conn, peer.Identity{}, nil
		}, handler, slog.New(slog.NewTextHandler(io.Discard, nil)))
		s.open()
		s.serve(context.Background(), server)
	}()
	if err := frame.Write(client, frame.KindRequest, []byte("wait")); err != nil {
		t.Fatal(err)
	}
	<-handler.entered
	client.Close()
	select {
	case <-handler.cancelled:
	case <-time.After(time.Second):
		t.Fatal("EOF waited for a request before cancelling it")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("connection cleanup did not finish")
	}
}

// served reports whether the server answers on conn, or has closed it.
func served(t *testing.T, conn net.Conn) bool {
	t.Helper()
	if err := frame.Write(conn, frame.KindRequest, []byte("ping")); err != nil {
		return false
	}
	_, err := frame.Read(conn, frame.KindResponse)
	return err == nil
}

// Any interactive user may run the UI and connect to the SYSTEM service. One user gets a
// few connections at a time, and everyone together a bounded number.
func TestConnectionLimits(t *testing.T) {
	var next atomic.Int32
	address, cancel, done := start(t, func(conn net.Conn) (net.Conn, peer.Identity, error) {
		// The first maxUserConnections+1 are one user's, then everyone has their own.
		n := next.Add(1)
		if n <= maxUserConnections+1 {
			return conn, peer.Identity{UserID: "S-1-5-21-1"}, nil
		}
		return conn, peer.Identity{UserID: fmt.Sprintf("S-1-5-21-%d", n)}, nil
	})
	defer func() { cancel(); <-done }()
	var mine []net.Conn
	for range maxUserConnections {
		conn := dial(t, address)
		if !served(t, conn) {
			t.Fatal("a user's own connection was refused")
		}
		mine = append(mine, conn)
	}
	if served(t, dial(t, address)) {
		t.Fatal("one user had more than maxUserConnections")
	}
	others := maxConnections - maxUserConnections
	for range others {
		if !served(t, dial(t, address)) {
			t.Fatal("another user's connection was refused")
		}
	}
	if served(t, dial(t, address)) {
		t.Fatal("more than maxConnections were open")
	}
	// A closed connection makes room again.
	mine[0].Close()
	deadline := time.Now().Add(5 * time.Second)
	for !served(t, dial(t, address)) {
		if time.Now().After(deadline) {
			t.Fatal("a closed connection still counted")
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// gated serves owner in full and refuses anyone else, as the Windows service's dispatcher.
type gated struct {
	mu      sync.Mutex
	handled []string
}

func (g *gated) Handle(_ context.Context, caller peer.Identity, payload []byte) []byte {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.handled = append(g.handled, fmt.Sprintf("%s:%d", caller.UserID, len(payload)))
	return []byte("handled")
}

func (g *gated) Refusal(caller peer.Identity) func(head []byte) []byte {
	if caller.UserID == "owner" {
		return nil
	}
	return func(head []byte) []byte { return append([]byte("refused:"), head...) }
}

func (g *gated) calls() []string {
	g.mu.Lock()
	defer g.mu.Unlock()
	return append([]string(nil), g.handled...)
}

// Someone other than the owner may only call GetInfo and Claim, which take a few bytes. A
// larger request of theirs is refused unread: before, each connection could have sixteen
// 16 MiB requests read and decoded before the dispatcher turned them away.
func TestNonOwnersCannotSendLargeRequests(t *testing.T) {
	handler := &gated{}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	var next atomic.Int32
	verify := func(conn net.Conn) (net.Conn, peer.Identity, error) {
		if next.Add(1) == 1 {
			return conn, peer.Identity{UserID: "other"}, nil
		}
		return conn, peer.Identity{UserID: "owner"}, nil
	}
	go func() { done <- Serve(ctx, listener, verify, handler, slog.New(slog.NewTextHandler(io.Discard, nil))) }()
	defer func() { cancel(); <-done }()
	other, owner := dial(t, listener.Addr().String()), dial(t, listener.Addr().String())

	large := bytes.Repeat([]byte{'x'}, 1<<20)
	copy(large, "the request's first bytes")
	if err := frame.Write(other, frame.KindRequest, large); err != nil {
		t.Fatal(err)
	}
	answer, err := frame.Read(other, frame.KindResponse)
	if err != nil {
		t.Fatal(err)
	}
	if want := "refused:the request's first bytes"; !strings.HasPrefix(string(answer), want) || len(answer) != len("refused:")+headBytes {
		t.Fatalf("answer = %.40q (%d bytes)", answer, len(answer))
	}
	// The connection stays usable, and a small request goes through.
	small := bytes.Repeat([]byte{'y'}, smallRequestBytes)
	for _, request := range [][]byte{small, large} {
		if err := frame.Write(owner, frame.KindRequest, request); err != nil {
			t.Fatal(err)
		}
		if _, err := frame.Read(owner, frame.KindResponse); err != nil {
			t.Fatal(err)
		}
	}
	if err := frame.Write(other, frame.KindRequest, small); err != nil {
		t.Fatal(err)
	}
	if answer, err := frame.Read(other, frame.KindResponse); err != nil || string(answer) != "handled" {
		t.Fatalf("small request: %q, %v", answer, err)
	}
	got := handler.calls()
	sort.Strings(got)
	want := []string{"other:4096", "owner:1048576", "owner:4096"}
	if !equal(got, want) {
		t.Fatalf("handled %v, want %v", got, want)
	}
}

// Every client the service turns away used to add a line to service.log, which only moves
// aside when the service starts: anyone could fill the disk by connecting in a loop.
func TestTurnedAwayClientsAreLoggedSparingly(t *testing.T) {
	var log bytes.Buffer
	var mu sync.Mutex
	logger := slog.New(slog.NewTextHandler(writerFunc(func(p []byte) (int, error) {
		mu.Lock()
		defer mu.Unlock()
		return log.Write(p)
	}), nil))
	s := newServer(func(net.Conn) (net.Conn, peer.Identity, error) {
		return nil, peer.Identity{}, errors.New("not the UI")
	}, echo{}, logger)
	now := time.Unix(1_000_000, 0)
	s.now = func() time.Time { return now }
	reject := func(times int) {
		for range times {
			client, conn := net.Pipe()
			if !s.open() {
				t.Fatal("rejected connections still count")
			}
			s.serve(context.Background(), conn)
			client.Close()
		}
	}
	reject(100)
	now = now.Add(logEvery)
	reject(1)
	mu.Lock()
	defer mu.Unlock()
	lines := strings.Split(strings.TrimSpace(log.String()), "\n")
	if len(lines) != 2 || !strings.Contains(lines[0], "rejected control client") || !strings.Contains(lines[1], "unlogged=99") {
		t.Fatalf("log:\n%s", log.String())
	}
}

type writerFunc func([]byte) (int, error)

func (f writerFunc) Write(p []byte) (int, error) { return f(p) }
