package sysproxy

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"slices"
	"strconv"
	"strings"
	"time"
)

// On Linux the service, root, sets its owner's system proxy in their desktop's own settings,
// as them (D63): systemd-run --user --machine=<user>@.host runs the desktop's tool in their
// user manager, where their session bus and settings daemon are, and the service writes every
// store that is there. GNOME keeps the settings in GSettings' org.gnome.system.proxy, which
// GTK, Firefox and Chromium read too; KDE in kioslaverc's [Proxy Settings]. Read keeps what
// was there in Settings.Desktop, and Write puts exactly that back.

// tools are the programs the service runs as the user, by absolute path: they run in the
// user's manager, whose PATH is theirs.
type tools struct {
	shell, gsettings, dbusSend string
	// kde are kreadconfig and kwriteconfig, Plasma 6's before Plasma 5's.
	kde [][2]string
}

var installed = tools{
	shell:     "/bin/sh",
	gsettings: "/usr/bin/gsettings",
	dbusSend:  "/usr/bin/dbus-send",
	kde: [][2]string{
		{"/usr/bin/kreadconfig6", "/usr/bin/kwriteconfig6"},
		{"/usr/bin/kreadconfig5", "/usr/bin/kwriteconfig5"},
	},
}

// runner runs argv as user in their user manager and returns what it printed.
type runner func(ctx context.Context, user string, argv []string) ([]byte, error)

// desktops reaches users' desktop settings; tests stand in for the runner and the tools.
type desktops struct {
	run    runner
	tools  tools
	exists func(path string) bool
}

var system = desktops{run: systemdRun, tools: installed, exists: executable}

// timeout bounds a Read or a Write: each store's tool runs once, but systemd-run may have to
// start the user's manager first.
const timeout = 30 * time.Second

// Read returns the proxy settings of user (a user name, or a uid, as systemd-run's --machine
// takes it) with a snapshot of every desktop store that is there; ErrUnsupportedDesktop when
// none is.
func Read(user string) (Settings, error) { return system.read(user) }

// Write sets user's proxy settings in every desktop store that is there: settings' snapshot
// where it has one of the store, else the store's form of the WinINet fields.
func Write(user string, settings Settings) error { return system.write(user, settings) }

// A store is one place a desktop keeps a user's proxy settings, as key-value pairs in the
// form of its own tool.
type store interface {
	name() string
	// read returns the store's values, false when the user's desktop does not have it.
	read(ctx context.Context, d desktops, user string) (map[string]string, bool, error)
	// apply sets values; with restore it also deletes the store's keys that values lack,
	// which a snapshot leaves out because they were not set. false when the store is not
	// there.
	apply(ctx context.Context, d desktops, user string, values map[string]string, restore bool) (bool, error)
	// ours is the port of the service's own settings when values hold them.
	ours(values map[string]string) (uint16, bool)
	// encode is the store's form of settings' WinINet fields.
	encode(settings Settings) map[string]string
	// derive is the WinINet form of values, as near as it goes.
	derive(values map[string]string) Settings
	// snapshot is the store's part of desktop, nil when it has none; keep sets it.
	snapshot(desktop *Desktop) map[string]string
	keep(desktop *Desktop, values map[string]string)
}

var stores = []store{gnome{}, kde{}}

func (d desktops) read(user string) (Settings, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	snapshot := &Desktop{}
	var theirs, ours *Settings
	for _, store := range stores {
		values, present, err := store.read(ctx, d, user)
		if err != nil {
			return Settings{}, fmt.Errorf("read %s's proxy settings: %w", store.name(), err)
		}
		if !present {
			continue
		}
		store.keep(snapshot, values)
		if port, ok := store.ours(values); ok {
			if ours == nil {
				settings := Core(port)
				ours = &settings
			}
		} else if theirs == nil {
			settings := store.derive(values)
			theirs = &settings
		}
	}
	// The fields say what the user has: their own settings where a store holds them, the
	// service's where every store does, so that Owned gives them no proxy back.
	var settings Settings
	switch {
	case theirs != nil:
		settings = *theirs
	case ours != nil:
		settings = *ours
	default:
		return Settings{}, ErrUnsupportedDesktop
	}
	settings.Desktop = snapshot
	return settings, nil
}

func (d desktops) write(user string, settings Settings) error {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	written := false
	var failed []error
	for _, store := range stores {
		var present bool
		var err error
		values := store.snapshot(settings.Desktop)
		switch {
		case values == nil:
			present, err = store.apply(ctx, d, user, store.encode(settings), false)
		case isOurs(store, values):
			// The service's own settings, left by a crash, are never given back: they
			// point at a port that nothing may listen on.
			present, err = store.apply(ctx, d, user, store.encode(Settings{Flags: Direct}), false)
		default:
			present, err = store.apply(ctx, d, user, values, true)
		}
		if err != nil {
			failed = append(failed, fmt.Errorf("write %s's proxy settings: %w", store.name(), err))
			continue
		}
		written = written || present
	}
	if len(failed) > 0 {
		return errors.Join(failed...)
	}
	if !written {
		return ErrUnsupportedDesktop
	}
	return nil
}

func isOurs(store store, values map[string]string) bool {
	_, ok := store.ours(values)
	return ok
}

// holds reports whether values have every pair of want.
func holds(values, want map[string]string) bool {
	for key, value := range want {
		if current, ok := values[key]; !ok || current != value {
			return false
		}
	}
	return true
}

// linuxBypass is Bypass for the desktops, which take networks rather than WinINet's wildcards.
var linuxBypass = []string{"localhost", "127.0.0.0/8", "::1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"}

// ourPort is the port of the service's own settings.
func ourPort(settings Settings) (uint16, bool) {
	if !Ours(settings) {
		return 0, false
	}
	_, port, _ := net.SplitHostPort(settings.Server)
	number, err := strconv.ParseUint(port, 10, 16)
	return uint16(number), err == nil
}

// server splits a WinINet proxy server, "host:port" or "http=host:port;https=…", into the
// HTTP proxy's host and port.
func server(value string) (string, string, bool) {
	for entry := range strings.SplitSeq(value, ";") {
		if protocol, address, ok := strings.Cut(entry, "="); ok {
			if protocol != "http" {
				continue
			}
			entry = address
		}
		host, port, err := net.SplitHostPort(strings.TrimSpace(entry))
		if err == nil && host != "" && port != "" {
			return host, port, true
		}
	}
	return "", "", false
}

// bypassList splits WinINet's bypass list; "<local>" has no equivalent on the desktops.
func bypassList(bypass string) []string {
	var hosts []string
	for host := range strings.SplitSeq(bypass, ";") {
		if host = strings.TrimSpace(host); host != "" && host != "<local>" {
			hosts = append(hosts, host)
		}
	}
	return hosts
}

// GNOME's org.gnome.system.proxy, through gsettings. Values are GVariant text, as gsettings
// prints and takes them: 'manual', 9347, ['localhost', '::1'].
type gnome struct{}

const gnomeSchema = "org.gnome.system.proxy"

// gnomeKeys are those the service writes and gives back, relative to gnomeSchema; mode comes
// last, so that programs following it see the rest in place. The proxies' credentials and the
// FTP proxy stay as the user set them.
var gnomeKeys = []string{
	"autoconfig-url", "ignore-hosts", "http.host", "http.port", "https.host", "https.port",
	"socks.host", "socks.port", "mode",
}

// gnomeRead prints the schema's keys, or nothing where it is not installed: then the user has
// no GNOME store. $1 is gsettings.
const gnomeRead = `"$1" list-schemas | grep -qx org.gnome.system.proxy || exit 0
exec "$1" list-recursively org.gnome.system.proxy
`

// gnomeWrite sets each schema, key and value after gsettings ($1), and prints "written"; or
// nothing where the schema is not installed.
const gnomeWrite = `tool=$1
shift
"$tool" list-schemas | grep -qx org.gnome.system.proxy || exit 0
while [ $# -ge 3 ]; do
	"$tool" set "$1" "$2" "$3" || exit
	shift 3
done
echo written
`

func (gnome) name() string { return "GNOME" }

func (gnome) snapshot(desktop *Desktop) map[string]string {
	if desktop == nil {
		return nil
	}
	return desktop.GNOME
}

func (gnome) keep(desktop *Desktop, values map[string]string) { desktop.GNOME = values }

func (gnome) read(ctx context.Context, d desktops, user string) (map[string]string, bool, error) {
	if !d.exists(d.tools.gsettings) {
		return nil, false, nil
	}
	out, err := d.run(ctx, user, []string{d.tools.shell, "-c", gnomeRead, "sh", d.tools.gsettings})
	if err != nil {
		return nil, false, err
	}
	if len(bytes.TrimSpace(out)) == 0 {
		return nil, false, nil
	}
	values := map[string]string{}
	for line := range strings.Lines(string(out)) {
		schema, rest, _ := strings.Cut(strings.TrimSuffix(line, "\n"), " ")
		key, value, ok := strings.Cut(rest, " ")
		if !ok {
			continue
		}
		name := key
		if child, ok := strings.CutPrefix(schema, gnomeSchema+"."); ok {
			name = child + "." + key
		} else if schema != gnomeSchema {
			continue
		}
		if slices.Contains(gnomeKeys, name) {
			values[name] = value
		}
	}
	return values, true, nil
}

func (gnome) apply(ctx context.Context, d desktops, user string, values map[string]string, _ bool) (bool, error) {
	if !d.exists(d.tools.gsettings) {
		return false, nil
	}
	argv := []string{d.tools.shell, "-c", gnomeWrite, "sh", d.tools.gsettings}
	// Every key has a value in GSettings, so a snapshot holds them all and restoring sets
	// them all: nothing to delete.
	for _, name := range gnomeKeys {
		value, ok := values[name]
		if !ok {
			continue
		}
		schema, key := gnomeSchema, name
		if child, rest, ok := strings.Cut(name, "."); ok {
			schema, key = gnomeSchema+"."+child, rest
		}
		argv = append(argv, schema, key, value)
	}
	out, err := d.run(ctx, user, argv)
	if err != nil {
		return false, err
	}
	return strings.TrimSpace(string(out)) == "written", nil
}

func gnomeOurs(port uint16) map[string]string {
	host, number := gvariantString("127.0.0.1"), strconv.Itoa(int(port))
	return map[string]string{
		"mode": "'manual'", "ignore-hosts": gvariantStrings(linuxBypass),
		"http.host": host, "http.port": number, "https.host": host, "https.port": number,
		"socks.host": host, "socks.port": number,
	}
}

func (gnome) ours(values map[string]string) (uint16, bool) {
	port, err := strconv.ParseUint(values["http.port"], 10, 16)
	if err != nil || port == 0 {
		return 0, false
	}
	return uint16(port), holds(values, gnomeOurs(uint16(port)))
}

func (gnome) encode(settings Settings) map[string]string {
	if port, ok := ourPort(settings); ok {
		return gnomeOurs(port)
	}
	switch {
	case settings.Flags&AutoProxyURL != 0 && settings.AutoConfigURL != "":
		return map[string]string{"mode": "'auto'", "autoconfig-url": gvariantString(settings.AutoConfigURL)}
	case settings.Flags&AutoDetect != 0:
		return map[string]string{"mode": "'auto'", "autoconfig-url": "''"}
	case settings.Flags&Proxy != 0:
		if host, port, ok := server(settings.Server); ok {
			return map[string]string{
				"mode": "'manual'", "ignore-hosts": gvariantStrings(bypassList(settings.Bypass)),
				"http.host": gvariantString(host), "http.port": port,
				"https.host": gvariantString(host), "https.port": port,
			}
		}
	}
	return map[string]string{"mode": "'none'"}
}

func (gnome) derive(values map[string]string) Settings {
	switch gvariantUnquote(values["mode"]) {
	case "manual":
		settings := Settings{Flags: Direct | Proxy, Bypass: strings.Join(gvariantList(values["ignore-hosts"]), ";")}
		for _, protocol := range []string{"http", "https", "socks"} {
			if host := gvariantUnquote(values[protocol+".host"]); host != "" {
				settings.Server = net.JoinHostPort(host, values[protocol+".port"])
				if protocol == "socks" {
					settings.Server = "socks=" + settings.Server
				}
				break
			}
		}
		return settings
	case "auto":
		if url := gvariantUnquote(values["autoconfig-url"]); url != "" {
			return Settings{Flags: Direct | AutoProxyURL, AutoConfigURL: url}
		}
		return Settings{Flags: Direct | AutoDetect}
	}
	return Settings{Flags: Direct}
}

// gvariantString is s as GVariant text.
func gvariantString(s string) string {
	return "'" + strings.NewReplacer(`\`, `\\`, `'`, `\'`).Replace(s) + "'"
}

// gvariantStrings is a list of strings as GVariant text, as gsettings prints it.
func gvariantStrings(list []string) string {
	if len(list) == 0 {
		return "@as []"
	}
	quoted := make([]string, len(list))
	for i, s := range list {
		quoted[i] = gvariantString(s)
	}
	return "[" + strings.Join(quoted, ", ") + "]"
}

// gvariantList reads the strings of GVariant text, such as a list of them.
func gvariantList(text string) []string {
	var list []string
	for i := 0; i < len(text); i++ {
		quote := text[i]
		if quote != '\'' && quote != '"' {
			continue
		}
		var value strings.Builder
		for i++; i < len(text) && text[i] != quote; i++ {
			if text[i] == '\\' && i+1 < len(text) {
				i++
			}
			value.WriteByte(text[i])
		}
		list = append(list, value.String())
	}
	return list
}

// gvariantUnquote reads a GVariant string; "" for anything else.
func gvariantUnquote(text string) string {
	if list := gvariantList(text); len(list) == 1 {
		return list[0]
	}
	return ""
}

// KDE's kioslaverc [Proxy Settings], through kreadconfig and kwriteconfig. Values are as
// kwriteconfig takes them; a key that is not set is not in a snapshot.
type kde struct{}

// kdeKeys are those the service writes and gives back; ProxyType comes last, as mode does
// for GNOME.
var kdeKeys = []string{"httpProxy", "httpsProxy", "socksProxy", "NoProxyFor", "ReversedException", "Proxy Config Script", "ProxyType"}

// kdeAbsent is what kreadconfig prints for a key that is not set.
const kdeAbsent = "<openweir: not set>"

// kdeRead prints the value of each key after kreadconfig ($1) and kdeAbsent ($2), one a line.
const kdeRead = `tool=$1 absent=$2
shift 2
for key do
	value=$("$tool" --file kioslaverc --group 'Proxy Settings' --key "$key" --default "$absent") || exit
	printf '%s\n' "$value"
done
`

// kdeWrite applies each "set" or "delete", key and value after kwriteconfig ($1), then has
// running KDE programs read the settings again through dbus-send ($2), if there is one.
const kdeWrite = `tool=$1 notify=$2
shift 2
while [ $# -ge 3 ]; do
	if [ "$1" = delete ]; then
		"$tool" --file kioslaverc --group 'Proxy Settings' --key "$2" --delete || exit
	else
		"$tool" --file kioslaverc --group 'Proxy Settings' --key "$2" -- "$3" || exit
	fi
	shift 3
done
if [ -n "$notify" ]; then
	"$notify" --session --type=signal /KIO/Scheduler org.kde.KIO.Scheduler.reparseSlaveConfiguration string: >/dev/null 2>&1 || :
fi
`

func (kde) name() string { return "KDE" }

func (kde) snapshot(desktop *Desktop) map[string]string {
	if desktop == nil {
		return nil
	}
	return desktop.KDE
}

func (kde) keep(desktop *Desktop, values map[string]string) { desktop.KDE = values }

// tools are the kreadconfig and kwriteconfig that are installed.
func (kde) tools(d desktops) ([2]string, bool) {
	for _, pair := range d.tools.kde {
		if d.exists(pair[0]) && d.exists(pair[1]) {
			return pair, true
		}
	}
	return [2]string{}, false
}

func (k kde) read(ctx context.Context, d desktops, user string) (map[string]string, bool, error) {
	tools, ok := k.tools(d)
	if !ok {
		return nil, false, nil
	}
	argv := append([]string{d.tools.shell, "-c", kdeRead, "sh", tools[0], kdeAbsent}, kdeKeys...)
	out, err := d.run(ctx, user, argv)
	if err != nil {
		return nil, false, err
	}
	lines := strings.Split(strings.TrimSuffix(string(out), "\n"), "\n")
	if len(lines) != len(kdeKeys) {
		return nil, false, fmt.Errorf("kreadconfig printed %d lines for %d keys", len(lines), len(kdeKeys))
	}
	values := map[string]string{}
	for i, key := range kdeKeys {
		if lines[i] != kdeAbsent {
			values[key] = lines[i]
		}
	}
	return values, true, nil
}

func (k kde) apply(ctx context.Context, d desktops, user string, values map[string]string, restore bool) (bool, error) {
	tools, ok := k.tools(d)
	if !ok {
		return false, nil
	}
	notify := ""
	if d.exists(d.tools.dbusSend) {
		notify = d.tools.dbusSend
	}
	argv := []string{d.tools.shell, "-c", kdeWrite, "sh", tools[1], notify}
	for _, key := range kdeKeys {
		if value, ok := values[key]; ok {
			argv = append(argv, "set", key, value)
		} else if restore {
			argv = append(argv, "delete", key, "")
		}
	}
	if _, err := d.run(ctx, user, argv); err != nil {
		return false, err
	}
	return true, nil
}

// kdeProxy is a proxy as KDE's settings write it: "http://127.0.0.1 9347".
func kdeProxy(scheme, host, port string) string {
	return scheme + "://" + host + " " + port
}

func kdeOurs(port uint16) map[string]string {
	number := strconv.Itoa(int(port))
	return map[string]string{
		"ProxyType": "1", "ReversedException": "false", "NoProxyFor": strings.Join(linuxBypass, ","),
		"httpProxy": kdeProxy("http", "127.0.0.1", number), "httpsProxy": kdeProxy("http", "127.0.0.1", number),
		"socksProxy": kdeProxy("socks", "127.0.0.1", number),
	}
}

// kdeAddress reads a proxy KDE keeps, "http://host port" or "http://host:port", as host and
// port.
func kdeAddress(value string) (string, string, bool) {
	if _, rest, ok := strings.Cut(value, "://"); ok {
		value = rest
	}
	if host, port, ok := strings.Cut(value, " "); ok {
		return host, port, host != "" && port != ""
	}
	host, port, err := net.SplitHostPort(value)
	return host, port, err == nil && host != "" && port != ""
}

func (kde) ours(values map[string]string) (uint16, bool) {
	_, port, ok := kdeAddress(values["httpProxy"])
	if !ok {
		return 0, false
	}
	number, err := strconv.ParseUint(port, 10, 16)
	if err != nil || number == 0 {
		return 0, false
	}
	return uint16(number), holds(values, kdeOurs(uint16(number)))
}

func (kde) encode(settings Settings) map[string]string {
	if port, ok := ourPort(settings); ok {
		return kdeOurs(port)
	}
	switch {
	case settings.Flags&AutoProxyURL != 0 && settings.AutoConfigURL != "":
		return map[string]string{"ProxyType": "2", "Proxy Config Script": settings.AutoConfigURL}
	case settings.Flags&AutoDetect != 0:
		return map[string]string{"ProxyType": "3"}
	case settings.Flags&Proxy != 0:
		if host, port, ok := server(settings.Server); ok {
			return map[string]string{
				"ProxyType": "1", "ReversedException": "false", "NoProxyFor": strings.Join(bypassList(settings.Bypass), ","),
				"httpProxy": kdeProxy("http", host, port), "httpsProxy": kdeProxy("http", host, port),
			}
		}
	}
	return map[string]string{"ProxyType": "0"}
}

func (kde) derive(values map[string]string) Settings {
	switch values["ProxyType"] {
	case "1":
		settings := Settings{Flags: Direct | Proxy, Bypass: strings.ReplaceAll(values["NoProxyFor"], ",", ";")}
		for _, key := range []string{"httpProxy", "httpsProxy"} {
			if host, port, ok := kdeAddress(values[key]); ok {
				settings.Server = net.JoinHostPort(host, port)
				break
			}
		}
		return settings
	case "2":
		return Settings{Flags: Direct | AutoProxyURL, AutoConfigURL: values["Proxy Config Script"]}
	case "3":
		return Settings{Flags: Direct | AutoDetect}
	}
	return Settings{Flags: Direct}
}

// systemdRun runs argv as user through their user manager: a transient unit that systemd-run
// waits for, with its output piped back (systemd 248 or later).
func systemdRun(ctx context.Context, user string, argv []string) ([]byte, error) {
	command := exec.CommandContext(ctx, "systemd-run", systemdRunArgs(user, argv)...)
	var stderr bytes.Buffer
	command.Stderr = &stderr
	out, err := command.Output()
	if err != nil {
		message := strings.TrimSpace(stderr.String())
		if len(message) > 512 {
			message = message[:512]
		}
		return nil, fmt.Errorf("run as %s through systemd-run: %w: %s", user, err, message)
	}
	return out, nil
}

func systemdRunArgs(user string, argv []string) []string {
	args := []string{"--user", "--machine=" + user + "@.host", "--wait", "--pipe", "--quiet", "--collect", "--"}
	for _, arg := range argv {
		// The manager expands $NAME and ${NAME} in a command's arguments; $$ stays a $.
		args = append(args, strings.ReplaceAll(arg, "$", "$$"))
	}
	return args
}

// executable reports whether path is a program the service may run.
func executable(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular() && info.Mode()&0o111 != 0
}
