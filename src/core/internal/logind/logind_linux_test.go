package logind

import (
	"slices"
	"testing"

	"github.com/godbus/dbus/v5"
)

// logind's properties as the bus delivers them (busctl introspect org.freedesktop.login1).
func TestDecode(t *testing.T) {
	user, sessions, err := decodeUser(map[string]dbus.Variant{
		"UID":       dbus.MakeVariant(uint32(1000)),
		"GID":       dbus.MakeVariant(uint32(1000)),
		"Name":      dbus.MakeVariant("alice"),
		"Timestamp": dbus.MakeVariant(uint64(1795340267506871)),
		"Display":   dbus.MakeVariant([]any{"4", dbus.ObjectPath("/org/freedesktop/login1/session/_34")}),
		"Sessions": dbus.MakeVariant([][]any{
			{"4", dbus.ObjectPath("/org/freedesktop/login1/session/_34")},
			{"1", dbus.ObjectPath("/org/freedesktop/login1/session/_31")},
		}),
		"State": dbus.MakeVariant("active"),
	})
	if err != nil {
		t.Fatal(err)
	}
	if user.UID != 1000 || user.Name != "alice" || user.Timestamp != 1795340267506871 || user.Display != "4" ||
		!slices.Equal(sessions, []dbus.ObjectPath{"/org/freedesktop/login1/session/_34", "/org/freedesktop/login1/session/_31"}) {
		t.Fatalf("user %+v, sessions %v", user, sessions)
	}
	// A user without a display session has ("", "/").
	if user, _, err := decodeUser(map[string]dbus.Variant{
		"UID": dbus.MakeVariant(uint32(1001)), "Name": dbus.MakeVariant("bob"), "Timestamp": dbus.MakeVariant(uint64(1)),
		"Display": dbus.MakeVariant([]any{"", dbus.ObjectPath("/")}), "Sessions": dbus.MakeVariant([][]any{}),
	}); err != nil || user.Display != "" {
		t.Fatalf("user %+v, %v", user, err)
	}
	session, err := decodeSession(map[string]dbus.Variant{
		"Id":     dbus.MakeVariant("4"),
		"User":   dbus.MakeVariant([]any{uint32(1000), dbus.ObjectPath("/org/freedesktop/login1/user/_1000")}),
		"Class":  dbus.MakeVariant("user"),
		"State":  dbus.MakeVariant("active"),
		"Remote": dbus.MakeVariant(false),
		"Active": dbus.MakeVariant(true),
	})
	if err != nil || session != (Session{ID: "4", UID: 1000, Class: "user", State: "active"}) {
		t.Fatalf("session %+v, %v", session, err)
	}
	if _, err := decodeSession(map[string]dbus.Variant{"Id": dbus.MakeVariant("4")}); err == nil {
		t.Fatal("decoded a session without its user")
	}
	if _, _, err := decodeUser(map[string]dbus.Variant{"UID": dbus.MakeVariant("1000")}); err == nil {
		t.Fatal("decoded a uid of the wrong type")
	}
}

func TestEventOf(t *testing.T) {
	for _, test := range []struct {
		signal *dbus.Signal
		want   event
		ok     bool
	}{
		{&dbus.Signal{Name: manager + ".PrepareForSleep", Body: []any{true}}, event{sleep: true, sleeping: true}, true},
		{&dbus.Signal{Name: manager + ".PrepareForSleep", Body: []any{false}}, event{sleep: true}, true},
		{&dbus.Signal{Name: manager + ".PrepareForSleep", Body: []any{"true"}}, event{}, false},
		{&dbus.Signal{Name: manager + ".UserNew", Body: []any{uint32(1000), dbus.ObjectPath("/")}}, event{}, true},
		{&dbus.Signal{Name: manager + ".SessionRemoved", Body: []any{"4", dbus.ObjectPath("/")}}, event{}, true},
		{&dbus.Signal{Name: manager + ".SeatNew"}, event{}, false},
		{&dbus.Signal{Name: properties + ".PropertiesChanged", Body: []any{sessionInterface,
			map[string]dbus.Variant{"State": dbus.MakeVariant("closing")}, []string{}}}, event{}, true},
		{&dbus.Signal{Name: properties + ".PropertiesChanged", Body: []any{userInterface,
			map[string]dbus.Variant{}, []string{"Display"}}}, event{}, true},
		{&dbus.Signal{Name: properties + ".PropertiesChanged", Body: []any{sessionInterface,
			map[string]dbus.Variant{"IdleHint": dbus.MakeVariant(true)}, []string{}}}, event{}, false},
	} {
		if got, ok := eventOf(test.signal); got != test.want || ok != test.ok {
			t.Errorf("%s %v = %+v, %v", test.signal.Name, test.signal.Body, got, ok)
		}
	}
}
