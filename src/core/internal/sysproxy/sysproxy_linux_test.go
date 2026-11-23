package sysproxy

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// fakeDesktop stands in for a user's desktop behind systemd-run: GSettings' proxy keys, by
// name relative to gnomeSchema (nil without the schema), and kioslaverc's [Proxy Settings]. It
// runs the service's scripts as gsettings, kreadconfig and kwriteconfig would.
type fakeDesktop struct {
	gnome, kde map[string]string
	tools      map[string]bool
	users      []string
	notified   int
	fail       error
}

func newDesktop() *fakeDesktop {
	return &fakeDesktop{tools: map[string]bool{installed.gsettings: true, installed.dbusSend: true,
		installed.kde[1][0]: true, installed.kde[1][1]: true}}
}

func (f *fakeDesktop) desktops() desktops {
	return desktops{run: f.run, tools: installed, exists: func(path string) bool { return f.tools[path] }}
}

// freshGNOME is what GSettings holds for a user who never changed their proxy, with keys the
// service leaves alone.
func freshGNOME() map[string]string {
	return map[string]string{
		"mode": "'none'", "autoconfig-url": "''", "ignore-hosts": "['localhost', '127.0.0.0/8', '::1']",
		"use-same-proxy": "true", "http.host": "''", "http.port": "8080", "http.authentication-password": "'secret'",
		"https.host": "''", "https.port": "0", "ftp.host": "''", "ftp.port": "0", "socks.host": "''", "socks.port": "0",
	}
}

func (f *fakeDesktop) run(_ context.Context, user string, argv []string) ([]byte, error) {
	f.users = append(f.users, user)
	if f.fail != nil {
		return nil, f.fail
	}
	if len(argv) < 5 || argv[0] != installed.shell || argv[1] != "-c" || argv[3] != "sh" {
		return nil, fmt.Errorf("unexpected command %q", argv)
	}
	var out strings.Builder
	args := argv[5:]
	switch argv[2] {
	case gnomeRead:
		if f.gnome == nil {
			break
		}
		for _, name := range slices.Sorted(maps.Keys(f.gnome)) {
			schema, key := gnomeSchema, name
			if child, rest, ok := strings.Cut(name, "."); ok {
				schema, key = gnomeSchema+"."+child, rest
			}
			fmt.Fprintf(&out, "%s %s %s\n", schema, key, f.gnome[name])
		}
	case gnomeWrite:
		if f.gnome == nil {
			break
		}
		for ; len(args) >= 3; args = args[3:] {
			name := args[1]
			if child, ok := strings.CutPrefix(args[0], gnomeSchema+"."); ok {
				name = child + "." + args[1]
			}
			f.gnome[name] = args[2]
		}
		out.WriteString("written\n")
	case kdeRead:
		for _, key := range args[1:] {
			value, ok := f.kde[key]
			if !ok {
				value = args[0]
			}
			fmt.Fprintf(&out, "%s\n", value)
		}
	case kdeWrite:
		if f.kde == nil {
			f.kde = map[string]string{}
		}
		if args[0] != "" {
			f.notified++
		}
		for args = args[1:]; len(args) >= 3; args = args[3:] {
			if args[0] == "delete" {
				delete(f.kde, args[1])
			} else {
				f.kde[args[1]] = args[2]
			}
		}
	default:
		return nil, errors.New("unknown script")
	}
	return []byte(out.String()), nil
}

// managed is the part of a GNOME store the service reads and writes.
func managed(gnome map[string]string) map[string]string {
	kept := map[string]string{}
	for _, name := range gnomeKeys {
		kept[name] = gnome[name]
	}
	return kept
}

// The service keeps exactly what each store held, puts its own settings in every store, and
// gives the user exactly theirs back, through proxy.json; keys KDE did not have go again.
func TestReadSetAndGiveBack(t *testing.T) {
	desktop := newDesktop()
	desktop.gnome = freshGNOME()
	desktop.gnome["mode"], desktop.gnome["autoconfig-url"] = "'auto'", "'http://corp.example/proxy.pac'"
	desktop.kde = map[string]string{"ProxyType": "1", "httpProxy": "http://proxy.corp.example 3128", "NoProxyFor": "corp.example"}
	gnome, kde := maps.Clone(desktop.gnome), maps.Clone(desktop.kde)
	d := desktop.desktops()

	theirs, err := d.read("alice")
	if err != nil {
		t.Fatal(err)
	}
	if theirs.Flags != Direct|AutoProxyURL || theirs.AutoConfigURL != "http://corp.example/proxy.pac" || Ours(theirs) {
		t.Fatalf("read %+v", theirs)
	}
	if !maps.Equal(theirs.Desktop.GNOME, managed(gnome)) || !maps.Equal(theirs.Desktop.KDE, kde) {
		t.Fatalf("snapshot %+v", theirs.Desktop)
	}
	if err := d.write("alice", Core(9347)); err != nil {
		t.Fatal(err)
	}
	for name, value := range gnomeOurs(9347) {
		if desktop.gnome[name] != value {
			t.Errorf("GNOME's %s = %s", name, desktop.gnome[name])
		}
	}
	if desktop.gnome["http.authentication-password"] != "'secret'" || desktop.gnome["use-same-proxy"] != "true" {
		t.Error("changed a key the service leaves alone")
	}
	if !maps.Equal(desktop.kde, map[string]string{
		"ProxyType": "1", "httpProxy": "http://127.0.0.1 9347", "httpsProxy": "http://127.0.0.1 9347",
		"socksProxy": "socks://127.0.0.1 9347", "ReversedException": "false",
		"NoProxyFor": "localhost,127.0.0.0/8,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
	}) {
		t.Fatalf("KDE's settings %v", desktop.kde)
	}
	if desktop.notified != 1 {
		t.Fatalf("told KDE %d times", desktop.notified)
	}
	if ours, err := d.read("alice"); err != nil || !Ours(ours) || Owned(ours) != (Settings{Flags: Direct}) {
		t.Fatalf("read the service's own as %+v, %v", ours, err)
	}

	// As proxy.json keeps them.
	raw, err := json.Marshal(Owned(theirs))
	if err != nil {
		t.Fatal(err)
	}
	var kept Settings
	if err := json.Unmarshal(raw, &kept); err != nil {
		t.Fatal(err)
	}
	if err := d.write("alice", kept); err != nil {
		t.Fatal(err)
	}
	if !maps.Equal(desktop.gnome, gnome) || !maps.Equal(desktop.kde, kde) {
		t.Fatalf("gave back GNOME's %v and KDE's %v", desktop.gnome, desktop.kde)
	}
	if !slices.Equal(slices.Compact(desktop.users), []string{"alice"}) {
		t.Fatalf("ran as %v", desktop.users)
	}
}

// A KDE store with none of the keys set is kept as empty, and giving it back deletes what the
// service set.
func TestAnEmptyStoreIsGivenBackEmpty(t *testing.T) {
	desktop := newDesktop()
	delete(desktop.tools, installed.gsettings)
	desktop.kde = map[string]string{"Proxy Settings Other": "kept"}
	d := desktop.desktops()
	theirs, err := d.read("1000")
	if err != nil || theirs.Flags != Direct || theirs.Desktop.GNOME != nil || theirs.Desktop.KDE == nil || len(theirs.Desktop.KDE) != 0 {
		t.Fatalf("read %+v (%+v), %v", theirs, theirs.Desktop, err)
	}
	raw, _ := json.Marshal(theirs)
	var kept Settings
	if err := json.Unmarshal(raw, &kept); err != nil || kept.Desktop.KDE == nil || kept.Desktop.GNOME != nil {
		t.Fatalf("kept %s as %+v, %v", raw, kept.Desktop, err)
	}
	if err := d.write("1000", Core(9347)); err != nil {
		t.Fatal(err)
	}
	if err := d.write("1000", kept); err != nil {
		t.Fatal(err)
	}
	if !maps.Equal(desktop.kde, map[string]string{"Proxy Settings Other": "kept"}) {
		t.Fatalf("KDE's settings %v", desktop.kde)
	}
}

// Settings the service left behind, by a crash, are never given back as the user's: a store
// that holds them gets no proxy instead.
func TestOursAreNeverGivenBack(t *testing.T) {
	desktop := newDesktop()
	desktop.gnome = freshGNOME()
	d := desktop.desktops()
	if err := d.write("alice", Core(7890)); err != nil {
		t.Fatal(err)
	}
	left, err := d.read("alice")
	if err != nil || !Ours(left) {
		t.Fatalf("read %+v, %v", left, err)
	}
	if err := d.write("alice", Owned(left)); err != nil {
		t.Fatal(err)
	}
	if desktop.gnome["mode"] != "'none'" || desktop.kde["ProxyType"] != "0" {
		t.Fatalf("gave back GNOME's mode %s, KDE's type %s", desktop.gnome["mode"], desktop.kde["ProxyType"])
	}

	// The user changed KDE's settings while GNOME's were still the service's: KDE's are theirs.
	if err := d.write("alice", Core(7890)); err != nil {
		t.Fatal(err)
	}
	desktop.kde = map[string]string{"ProxyType": "3"}
	left, err = d.read("alice")
	if err != nil || Ours(left) || left.Flags != Direct|AutoDetect {
		t.Fatalf("read %+v, %v", left, err)
	}
	if err := d.write("alice", Owned(left)); err != nil {
		t.Fatal(err)
	}
	if desktop.gnome["mode"] != "'none'" || !maps.Equal(desktop.kde, map[string]string{"ProxyType": "3"}) {
		t.Fatalf("gave back GNOME's mode %s, KDE's %v", desktop.gnome["mode"], desktop.kde)
	}
}

// Without GNOME's schema or KDE's tools the desktop has nowhere for the settings.
func TestUnsupportedDesktop(t *testing.T) {
	desktop := newDesktop()
	desktop.tools = map[string]bool{installed.gsettings: true} // the schema is not installed
	d := desktop.desktops()
	if _, err := d.read("alice"); !errors.Is(err, ErrUnsupportedDesktop) {
		t.Fatalf("read = %v", err)
	}
	if err := d.write("alice", Core(9347)); !errors.Is(err, ErrUnsupportedDesktop) {
		t.Fatalf("write = %v", err)
	}
	desktop.tools = map[string]bool{installed.kde[0][0]: true} // a reader without a writer
	if _, err := d.read("alice"); !errors.Is(err, ErrUnsupportedDesktop) {
		t.Fatalf("read = %v", err)
	}
	desktop.tools = map[string]bool{installed.gsettings: true}
	desktop.fail = errors.New("no user manager")
	if _, err := d.read("alice"); err == nil || errors.Is(err, ErrUnsupportedDesktop) {
		t.Fatalf("read without a user manager = %v", err)
	}
	if err := d.write("alice", Core(9347)); err == nil || errors.Is(err, ErrUnsupportedDesktop) {
		t.Fatalf("write without a user manager = %v", err)
	}
}

// What is not the service's own reads as WinINet would hold it, and a store without a
// snapshot gets its form of that.
func TestDeriveAndEncode(t *testing.T) {
	g, k := gnome{}, kde{}
	for _, settings := range []Settings{
		{Flags: Direct},
		{Flags: Direct | AutoDetect},
		{Flags: Direct | AutoProxyURL, AutoConfigURL: "http://corp.example/it's.pac"},
		{Flags: Direct | Proxy, Server: "proxy.corp.example:3128", Bypass: "corp.example;10.0.0.0/8"},
	} {
		if got := g.derive(g.encode(settings)); got != settings {
			t.Errorf("GNOME's %+v reads as %+v", settings, got)
		}
		if got := k.derive(k.encode(settings)); got != settings {
			t.Errorf("KDE's %+v reads as %+v", settings, got)
		}
	}
	if got := g.encode(Settings{Flags: Direct | Proxy, Server: "http=[::1]:8080;https=other:1"}); got["http.host"] != "'::1'" || got["http.port"] != "8080" {
		t.Errorf("encoded %v", got)
	}
	if got := g.derive(map[string]string{"mode": "'manual'", "socks.host": "'127.0.0.1'", "socks.port": "1080", "ignore-hosts": "@as []"}); got != (Settings{Flags: Direct | Proxy, Server: "socks=127.0.0.1:1080"}) {
		t.Errorf("derived %+v", got)
	}
	if got := k.derive(map[string]string{"ProxyType": "1", "httpsProxy": "http://[::1]:8080"}); got != (Settings{Flags: Direct | Proxy, Server: "[::1]:8080"}) {
		t.Errorf("derived %+v", got)
	}
	if list := gvariantList(`['a', "it's", 'back\\slash', 'q\'uote']`); !slices.Equal(list, []string{"a", "it's", `back\slash`, "q'uote"}) {
		t.Errorf("list %q", list)
	}
	if gvariantStrings(nil) != "@as []" || gvariantString(`it's \`) != `'it\'s \\'` {
		t.Error("quoting")
	}
	if _, ok := (gnome{}).ours(gnomeOurs(0)); ok {
		t.Error("port 0 is ours")
	}
}

// systemd's manager expands $NAME in a command's arguments, so every $ goes as $$.
func TestSystemdRunArgs(t *testing.T) {
	got := systemdRunArgs("alice", []string{"/bin/sh", "-c", `echo "$1" ${HOME} $$`, "sh", "a$b"})
	want := []string{"--user", "--machine=alice@.host", "--wait", "--pipe", "--quiet", "--collect", "--",
		"/bin/sh", "-c", `echo "$$1" $${HOME} $$$$`, "sh", "a$$b"}
	if !slices.Equal(got, want) {
		t.Fatalf("args %q", got)
	}
}

// The scripts themselves, run by /bin/sh against stand-ins for gsettings, kreadconfig5,
// kwriteconfig5 and dbus-send that keep the settings in files.
func TestScripts(t *testing.T) {
	if _, err := os.Stat(installed.shell); err != nil {
		t.Skip("no /bin/sh")
	}
	dir := t.TempDir()
	store := filepath.Join(dir, "store")
	for _, sub := range []string{"gnome", "kde"} {
		if err := os.MkdirAll(filepath.Join(store, sub), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	programs := map[string]string{
		"gsettings": `case $1 in
list-schemas) echo org.gnome.desktop.interface; [ -d "$STORE/gnome" ] && echo org.gnome.system.proxy ;;
list-recursively)
	for file in "$STORE"/gnome/*; do
		name=${file##*/}
		printf '%s %s %s\n' "${name% *}" "${name#* }" "$(cat "$file")"
	done ;;
set) printf '%s' "$4" > "$STORE/gnome/$2 $3" ;;
*) exit 1 ;;
esac`,
		"kreadconfig5": `while [ $# -gt 0 ]; do
	case $1 in
	--key) key=$2; shift 2 ;;
	--default) default=$2; shift 2 ;;
	--file|--group) shift 2 ;;
	*) exit 1 ;;
	esac
done
if [ -f "$STORE/kde/$key" ]; then cat "$STORE/kde/$key"; echo; else printf '%s\n' "$default"; fi`,
		"kwriteconfig5": `delete=
while [ $# -gt 0 ]; do
	case $1 in
	--key) key=$2; shift 2 ;;
	--delete) delete=1; shift ;;
	--file|--group) shift 2 ;;
	--) value=$2; shift 2 ;;
	*) exit 1 ;;
	esac
done
if [ -n "$delete" ]; then rm -f "$STORE/kde/$key"; else printf '%s' "$value" > "$STORE/kde/$key"; fi`,
		"dbus-send": `printf '%s\n' "$*" >> "$STORE/notified"`,
	}
	for name, body := range programs {
		script := "#!/bin/sh\nSTORE='" + store + "'\n" + body + "\n"
		if err := os.WriteFile(filepath.Join(dir, name), []byte(script), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	gnomeStore := map[string]string{
		"org.gnome.system.proxy mode": "'manual'", "org.gnome.system.proxy autoconfig-url": "''",
		"org.gnome.system.proxy ignore-hosts": "['localhost', '$HOME']", "org.gnome.system.proxy.http host": "'proxy'",
		"org.gnome.system.proxy.http port": "3128", "org.gnome.system.proxy.https host": "''",
		"org.gnome.system.proxy.https port": "0", "org.gnome.system.proxy.socks host": "''",
		"org.gnome.system.proxy.socks port": "0", "org.gnome.system.proxy.http authentication-user": "'me'",
	}
	for name, value := range gnomeStore {
		if err := os.WriteFile(filepath.Join(store, "gnome", name), []byte(value), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(store, "kde", "Proxy Config Script"), []byte("http://corp/a b.pac"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(store, "kde", "ProxyType"), []byte("2"), 0o600); err != nil {
		t.Fatal(err)
	}
	files := func(sub string) map[string]string {
		found := map[string]string{}
		entries, err := os.ReadDir(filepath.Join(store, sub))
		if err != nil {
			t.Fatal(err)
		}
		for _, entry := range entries {
			content, err := os.ReadFile(filepath.Join(store, sub, entry.Name()))
			if err != nil {
				t.Fatal(err)
			}
			found[entry.Name()] = string(content)
		}
		return found
	}
	gnomeBefore, kdeBefore := files("gnome"), files("kde")
	d := desktops{
		run: func(ctx context.Context, _ string, argv []string) ([]byte, error) {
			command := exec.CommandContext(ctx, argv[0], argv[1:]...)
			var stderr bytes.Buffer
			command.Stderr = &stderr
			out, err := command.Output()
			if err != nil {
				return nil, fmt.Errorf("%w: %s", err, stderr.String())
			}
			return out, nil
		},
		tools: tools{
			shell: installed.shell, gsettings: filepath.Join(dir, "gsettings"), dbusSend: filepath.Join(dir, "dbus-send"),
			kde: [][2]string{{filepath.Join(dir, "kreadconfig6"), filepath.Join(dir, "kwriteconfig6")},
				{filepath.Join(dir, "kreadconfig5"), filepath.Join(dir, "kwriteconfig5")}},
		},
		exists: executable,
	}
	theirs, err := d.read("alice")
	if err != nil {
		t.Fatal(err)
	}
	if theirs.Server != "proxy:3128" || theirs.Bypass != "localhost;$HOME" || theirs.Desktop.GNOME["ignore-hosts"] != "['localhost', '$HOME']" ||
		!maps.Equal(theirs.Desktop.KDE, map[string]string{"ProxyType": "2", "Proxy Config Script": "http://corp/a b.pac"}) {
		t.Fatalf("read %+v (%+v)", theirs, theirs.Desktop)
	}
	if err := d.write("alice", Core(9347)); err != nil {
		t.Fatal(err)
	}
	if ours, err := d.read("alice"); err != nil || !Ours(ours) {
		t.Fatalf("read the service's own as %+v, %v", ours, err)
	}
	if got := files("kde"); got["httpProxy"] != "http://127.0.0.1 9347" || got["ProxyType"] != "1" {
		t.Fatalf("KDE's settings %v", got)
	}
	if err := d.write("alice", theirs); err != nil {
		t.Fatal(err)
	}
	if got := files("gnome"); !maps.Equal(got, gnomeBefore) {
		t.Fatalf("GNOME's settings %v, not %v", got, gnomeBefore)
	}
	if got := files("kde"); !maps.Equal(got, kdeBefore) {
		t.Fatalf("KDE's settings %v, not %v", got, kdeBefore)
	}
	notified, err := os.ReadFile(filepath.Join(store, "notified"))
	if err != nil || strings.Count(string(notified), "org.kde.KIO.Scheduler.reparseSlaveConfiguration") != 2 {
		t.Fatalf("notified %q, %v", notified, err)
	}
}
