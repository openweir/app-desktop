// Package server runs the framed control protocol over a stream listener: the Windows
// service pipe, or the development socket elsewhere (docs/architecture.md §4.2).
package server

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"sync"
	"time"

	"github.com/openweir/app-desktop/src/core/internal/frame"
	"github.com/openweir/app-desktop/src/core/internal/operation"
	"github.com/openweir/app-desktop/src/core/internal/peer"
)

type Handler interface {
	Handle(ctx context.Context, caller peer.Identity, payload []byte) []byte
}

// Gate is a Handler that serves some callers only a few small requests: the Windows service
// answers anyone but its owner GetInfo and Claim alone (docs/architecture.md §5.4).
type Gate interface {
	// Refusal is nil for a caller who may send requests as large as a frame allows. For
	// anyone else it answers a request over smallRequestBytes, which the server skips unread
	// but for its first bytes, head.
	Refusal(caller peer.Identity) func(head []byte) []byte
}

const (
	// maxInFlight bounds concurrent requests on one connection; reading pauses until a slot frees.
	maxInFlight = 16
	// maxConnections bounds the connections open at once, verified or not, and
	// maxUserConnections those of one user: the UI keeps one, any interactive user may run
	// it, and the service runs as SYSTEM.
	maxConnections     = 64
	maxUserConnections = 8
	// smallRequestBytes is the most a caller the Gate refuses may send in one request:
	// GetInfo and Claim, all such a caller can be served, take a few bytes. A larger request
	// is never read into memory, let alone decoded.
	smallRequestBytes = 4 << 10
	// headBytes of a refused request are read for its id, which protobuf encoders write
	// ahead of the method.
	headBytes = 64
	// logEvery spaces out the lines that a caller could otherwise add to the service's log
	// at will, by connecting again and again.
	logEvery = time.Minute
)

const acceptRetryDelay = 100 * time.Millisecond

// Serve accepts connections until ctx is cancelled, then waits for open connections to finish.
func Serve(ctx context.Context, listener net.Listener, verify peer.Verifier, handler Handler, logger *slog.Logger) error {
	stopAccepting := context.AfterFunc(ctx, func() { listener.Close() })
	defer stopAccepting()
	s := newServer(verify, handler, logger)
	var connections sync.WaitGroup
	defer connections.Wait()
	for {
		conn, err := listener.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			if errors.Is(err, net.ErrClosed) {
				return err
			}
			logger.Warn("accept control connection", "error", err)
			time.Sleep(acceptRetryDelay)
			continue
		}
		if !s.open() {
			conn.Close()
			s.note(&s.crowded, slog.LevelWarn, "refused a control connection", "reason", "too many connections")
			continue
		}
		connections.Go(func() { s.serve(ctx, conn) })
	}
}

type server struct {
	verify  peer.Verifier
	handler Handler
	gate    Gate
	log     *slog.Logger
	now     func() time.Time

	mu    sync.Mutex
	conns int
	users map[string]int

	rejected, crowded, closed throttle
}

func newServer(verify peer.Verifier, handler Handler, logger *slog.Logger) *server {
	gate, _ := handler.(Gate)
	return &server{verify: verify, handler: handler, gate: gate, log: logger, now: time.Now, users: map[string]int{}}
}

// open counts a connection just accepted, unless there are too many already.
func (s *server) open() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.conns >= maxConnections {
		return false
	}
	s.conns++
	return true
}

func (s *server) close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.conns--
}

// join counts a verified connection to its user, unless they have too many already.
func (s *server) join(user string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.users[user] >= maxUserConnections {
		return false
	}
	s.users[user]++
	return true
}

func (s *server) leave(user string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.users[user]--; s.users[user] == 0 {
		delete(s.users, user)
	}
}

// throttle lets one log line through per logEvery and counts the rest for the next one.
type throttle struct {
	mu      sync.Mutex
	next    time.Time
	skipped int
}

func (t *throttle) allow(now time.Time) (bool, int) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if now.Before(t.next) {
		t.skipped++
		return false, 0
	}
	skipped := t.skipped
	t.next, t.skipped = now.Add(logEvery), 0
	return true, skipped
}

// note logs a line about something a caller may repeat at will, unless one like it went to
// the log within logEvery; the next line that does says how many did not.
func (s *server) note(t *throttle, level slog.Level, message string, args ...any) {
	ok, skipped := t.allow(s.now())
	if !ok {
		return
	}
	if skipped != 0 {
		args = append(args, "unlogged", skipped)
	}
	s.log.Log(context.Background(), level, message, args...)
}

// serve answers the requests on a connection that open counted. It takes the connection
// off the counts before closing it, so that a client which saw it close finds room again.
func (s *server) serve(ctx context.Context, accepted net.Conn) {
	conn, caller, err := s.verify(accepted)
	if err != nil {
		s.close()
		accepted.Close()
		s.note(&s.rejected, slog.LevelWarn, "rejected control client", "error", err)
		return
	}
	defer conn.Close()
	defer s.close()
	if !s.join(caller.UserID) {
		s.note(&s.crowded, slog.LevelWarn, "refused a control connection", "reason", "too many connections",
			"user", caller.UserID)
		return
	}
	defer s.leave(caller.UserID)
	ctx, cancel := context.WithCancel(ctx)
	context.AfterFunc(ctx, func() { conn.Close() })

	var writes operation.Gate
	var requests sync.WaitGroup
	defer func() {
		// In particular, EOF must cancel queued work before waiting for it. Waiting
		// first would let requests execute after their client has disconnected.
		cancel()
		requests.Wait()
	}()
	slots := make(chan struct{}, maxInFlight)
	for {
		payload, refused, err := s.read(conn, caller)
		if err != nil {
			if !errors.Is(err, io.EOF) && ctx.Err() == nil {
				s.note(&s.closed, slog.LevelInfo, "control connection closed", "user", caller.UserID, "error", err)
			}
			return
		}
		select {
		case slots <- struct{}{}:
		case <-ctx.Done():
			return
		}
		requests.Go(func() {
			defer func() { <-slots }()
			response := refused
			if response == nil {
				response = s.handler.Handle(ctx, caller, payload)
			}
			writeCtx, stopWrite := context.WithTimeout(ctx, 5*time.Second)
			defer stopWrite()
			if err := writes.Lock(writeCtx); err != nil {
				cancel()
				return
			}
			defer writes.Unlock()
			deadline, _ := writeCtx.Deadline()
			if err := conn.SetWriteDeadline(deadline); err != nil {
				cancel()
				return
			}
			if err := frame.Write(conn, frame.KindResponse, response); err != nil {
				cancel()
			}
		})
	}
}

// read receives the next request from caller. A request larger than the Gate lets caller
// send is skipped, and comes back as the answer that refuses it instead.
func (s *server) read(conn net.Conn, caller peer.Identity) (payload, refused []byte, err error) {
	length, err := frame.ReadHeader(conn, frame.KindRequest)
	if err != nil {
		return nil, nil, err
	}
	if s.gate != nil && length > smallRequestBytes {
		if refusal := s.gate.Refusal(caller); refusal != nil {
			head, err := frame.Skip(conn, length, make([]byte, headBytes))
			if err != nil {
				return nil, nil, err
			}
			return nil, refusal(head), nil
		}
	}
	payload, err = frame.ReadPayload(conn, length)
	return payload, nil, err
}
