package control

import (
	"context"
	"fmt"
	"io"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"

	"github.com/openweir/app-desktop/src/core/internal/controlpb"
	"github.com/openweir/app-desktop/src/core/internal/frame"
)

// manyRules runs a profile with a hundred thousand inline rules, whose list encodes to
// about 11 MB.
type manyRules struct{ unlinked }

func (manyRules) State() controlpb.RunState { return controlpb.RunState_RUN_STATE_RUNNING }

func (manyRules) ListRules() []*controlpb.Rule {
	rules := make([]*controlpb.Rule, 100_000)
	for i := range rules {
		rules[i] = &controlpb.Rule{
			Type:    "DOMAIN-SUFFIX",
			Payload: "some-advertising-domain.example.com",
			Proxy:   "🚫 广告拦截",
			EntryId: "6f1c1e3a-0000-4000-8000-000000000000",
		}
	}
	return rules
}

// An answer the pipe cannot carry becomes an error the UI can show, instead of a frame the
// server refuses to write: that would close the connection, and every other request and
// subscription on it with it.
func TestOversizedAnswerBecomesAnError(t *testing.T) {
	d := New(Options{Engine: manyRules{}})
	defer d.Close()
	payload, err := proto.Marshal(request(7, func(r *controlpb.Request) {
		r.Method = &controlpb.Request_ListRules{ListRules: &controlpb.ListRulesRequest{}}
	}))
	if err != nil {
		t.Fatal(err)
	}
	answer := d.Handle(context.Background(), alice, payload)
	if err := frame.Write(io.Discard, frame.KindResponse, answer); err != nil {
		t.Fatalf("the pipe cannot carry the answer: %v", err)
	}
	response := decode(t, answer)
	expectError(t, response, 7, CodeResponseTooLarge)
	if response.GetError().GetRetryable() {
		t.Fatal("asking again would only get the same answer")
	}
	// The dispatcher goes on answering.
	if call(t, d, request(8, getInfo)).GetGetInfo() == nil {
		t.Fatal("no answer after an oversized one")
	}
}

// crowd has tens of thousands of connections, more than one snapshot can carry.
type crowd struct {
	sampler
	count int
}

func (c *crowd) Connections() *controlpb.ConnectionSnapshot {
	snapshot := &controlpb.ConnectionSnapshot{Connections: make([]*controlpb.Connection, c.count)}
	for i := range snapshot.Connections {
		snapshot.Connections[i] = &controlpb.Connection{
			Id:              fmt.Sprintf("%08d-0000-4000-8000-000000000000", i),
			Network:         "tcp",
			Source:          "198.18.0.1:51234",
			Destination:     "203.0.113.10:443",
			Host:            fmt.Sprintf("tracker-%d.example.com", i),
			Process:         `C:\Program Files\Some Client\client.exe`,
			Chains:          []string{"节点选择", "香港 01"},
			Rule:            "DOMAIN-SUFFIX",
			RulePayload:     "example.com",
			UploadBytes:     1 << 20,
			DownloadBytes:   1 << 30,
			StartedAtUnixMs: uint64(1_700_000_000_000 + i),
			RuleEntryId:     "6f1c1e3a-0000-4000-8000-000000000000",
		}
	}
	return snapshot
}

// With some forty thousand connections the first snapshot alone used to exceed the frame
// limit, so every poll of it failed. A snapshot keeps the oldest connections that fit.
func TestConnectionSnapshotFitsAPoll(t *testing.T) {
	engine := &crowd{count: 40_000}
	if size := proto.Size(engine.Connections()); size <= frame.MaxResponseBytes {
		t.Fatalf("the crowd takes %d bytes, which fits a frame already", size)
	}
	subs := newSubscriptions(engine)
	subs.period = 5 * time.Millisecond
	t.Cleanup(subs.close)
	id, err := subs.subscribe(context.Background(), alice, topicConnections, 0)
	if err != nil {
		t.Fatal(err)
	}
	events := pollUntil(t, subs, id, 1)
	answer := encode(1, &controlpb.Response{Result: &controlpb.Response_PollEvents{
		PollEvents: &controlpb.PollEventsResponse{Events: events},
	}})
	if len(answer) > frame.MaxResponseBytes {
		t.Fatalf("the poll takes %d bytes", len(answer))
	}
	connections := events[0].GetConnections().GetConnections()
	if len(connections) == 0 || len(connections) >= engine.count {
		t.Fatalf("the snapshot carries %d of %d connections", len(connections), engine.count)
	}
	for i, connection := range connections {
		if want := fmt.Sprintf("%08d-0000-4000-8000-000000000000", i); connection.GetId() != want {
			t.Fatalf("connection %d is %s, want the oldest ones in order", i, connection.GetId())
		}
	}
	if size := proto.Size(events[0].GetConnections()); size > maxSnapshotBytes {
		t.Fatalf("the snapshot takes %d bytes, over %d", size, maxSnapshotBytes)
	}
}

// A snapshot within the limit comes through untouched.
func TestSmallConnectionSnapshotIsKept(t *testing.T) {
	snapshot := (&crowd{count: 3}).Connections()
	if got := capSnapshot(snapshot, maxSnapshotBytes); got != snapshot || len(got.GetConnections()) != 3 {
		t.Fatalf("capped to %d connections", len(got.GetConnections()))
	}
	if got := capSnapshot(snapshot, 0); len(got.GetConnections()) != 0 || len(snapshot.GetConnections()) != 3 {
		t.Fatal("capping changed the engine's snapshot")
	}
}
