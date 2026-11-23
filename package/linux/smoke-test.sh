#!/usr/bin/env bash
# Installs, upgrades and removes OpenWeir's Linux packages on a disposable GitHub Actions machine
# (docs/architecture.md "Linux", docs/maintenance.md): CI's linux-install job runs it on the
# packages its linux-package job built. The Windows installer has package/windows/smoke-test.ps1.
#
#     package/linux/smoke-test.sh system OpenWeir-<v>-linux-<arch>.deb OpenWeir-<v+1>-linux-<arch>.deb
#     package/linux/smoke-test.sh deb OpenWeir-<v>-linux-<arch>.deb
#     package/linux/smoke-test.sh rpm OpenWeir-<v>-linux-<arch>.rpm
#
# system: a hosted runner, a virtual machine that systemd runs, as its user with sudo.
#   1. apt-get installs the .deb: both units enabled, the socket listening, the service running
#      and in the journal, /opt/openweir root's and 0755, /var/lib/openweir 0711, the polkit
#      actions, the .desktop file, and every library openweir-ui and Cronet need found.
#   2. The routing a crashed TUN leaves (IPv4 and IPv6 rules in the pinned range, routes in the
#      pinned table, a TUN named openweir0) is made by hand on test networks only, the service
#      is killed with SIGKILL, and systemd's -cleanup must bring the routing back as it was.
#      No real TUN runs: a tunnel needs an owner, and only an active local session (logind)
#      may claim one, which a runner has not. Nor can the system proxy (no GNOME or KDE
#      session), the take-over (no polkit agent) or IPv6 beyond the machine be checked here.
#   3. An offscreen UI as this user quits when --quit asks it to; another, started with
#      --background, quits when the next version's .deb upgrades the package, which restarts
#      the service and keeps the units enabled.
#   4. apt-get remove keeps the service's state in /var/lib/openweir; purge removes it.
# deb: a container of the oldest distributions supported, as root, without systemd: the .deb
#   installs with its dependencies, the units are enabled, the files are root's and unchanged,
#   every library is found; then remove and purge.
# rpm: a Fedora container likewise, with rpm -V and the requirements rpm lists, then dnf remove.
#
# A check that fails prints an ::error:: line that names it, and the test goes on, so that one
# run shows every problem; it fails at the end. What the later checks cannot do without stops it
# at once. The journal, apt's log and the UI's output land in $OPENWEIR_SMOKE_OUTPUT
# (build/linux-smoke), which CI keeps when the test fails.
set -uo pipefail

mode=${1:-}
package=${2:-}
upgrade=${3:-}

# The tests run this against a temporary root with stand-ins for the system's commands.
root=${OPENWEIR_SMOKE_ROOT:-}
output=${OPENWEIR_SMOKE_OUTPUT:-build/linux-smoke}
# product.json's linux section (tests/test_build_linux.py holds them to it).
install_dir=$root/opt/openweir
socket=$root/run/openweir/control.sock
state_dir=$root/var/lib/openweir
cache_dir=$root/var/cache/openweir
unit_dir=$root/usr/lib/systemd/system
desktop_file=$root/usr/share/applications/org.openweir.app.desktop
device=openweir0
table=9347
rule_index=9347
ui=$install_dir/openweir-ui
dpkg_log=$root/var/log/dpkg.log
# The UI's data directory (src/openweir/models/store.py) and its lock (desktop/instance.py).
data_dir=$HOME/.local/share/OpenWeir
# The service's start in the journal (src/core/service/service_linux.go).
started='msg="service started"'
# How long the package's preinst gives running UIs to quit before it sends SIGTERM
# (package/linux/scripts/functions.sh): a UI gone well before then quit when asked.
quit_wait=15

failures=0

# Annotation text: GitHub's escapes for data, and for properties such as the title.
escape_data() {
	local text=$1
	text=${text//'%'/%25}
	text=${text//$'\r'/%0D}
	text=${text//$'\n'/%0A}
	printf '%s' "$text"
}

escape_property() {
	local text
	text=$(escape_data "$1")
	text=${text//:/%3A}
	text=${text//,/%2C}
	printf '%s' "$text"
}

# fail <check> <problem>: the check's error annotation; the test goes on.
fail() {
	echo "::error title=$(escape_property "Linux $mode smoke test: $1")::$(escape_data "${2:0:2000}")"
	failures=$((failures + 1))
}

pass() {
	echo "ok: $1"
}

# abort <check> <problem>: as fail, for what the later checks cannot do without.
abort() {
	fail "$1" "$2"
	finish
}

# expect <check> <actual> <expected>
expect() {
	if [ "$2" = "$3" ]; then
		pass "$1"
	else
		fail "$1" "got '$2', expected '$3'"
	fi
}

# check <check> <command...>: the command succeeds, or the check fails with its output.
check() {
	local name=$1 out
	shift
	if out=$("$@" 2>&1); then
		pass "$name"
	else
		fail "$name" "$* failed: ${out:-no output}"
	fi
}

# refute <check> <command...>: the command fails.
refute() {
	local name=$1 out
	shift
	if out=$("$@" 2>&1); then
		fail "$name" "$* succeeded: ${out:-no output}"
	else
		pass "$name"
	fi
}

section() {
	echo
	echo "== $1"
}

# wait_until <seconds> <command...>: tries the command twice a second until it succeeds.
wait_until() {
	local tries=$(($1 * 2))
	shift
	until "$@" >/dev/null 2>&1; do
		tries=$((tries - 1))
		[ "$tries" -gt 0 ] || return 1
		sleep 0.5
	done
}

as_root() {
	if [ "$(id -u)" -eq 0 ]; then
		"$@"
	else
		sudo "$@"
	fi
}

# What downloads goes through mirrors, which sometimes fail: twice more, and every call with a
# deadline. needrestart, where it is, only lists.
apt_get() {
	local attempt
	for attempt in 1 2 3; do
		if as_root env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l timeout 600 apt-get -q -y \
			-o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 "$@"; then
			return 0
		fi
		if [ "$attempt" -lt 3 ]; then sleep $((attempt * 15)); fi
	done
	return 1
}

dnf_install() {
	local attempt
	for attempt in 1 2 3; do
		if timeout 900 dnf -y --setopt=retries=5 install "$@"; then
			return 0
		fi
		if [ "$attempt" -lt 3 ]; then sleep $((attempt * 15)); fi
	done
	return 1
}

# The journal, apt's or dnf's log and the UI's output, for CI to keep; then the verdict.
finish() {
	if [ "$failures" -gt 0 ]; then
		mkdir -p "$output"
		if [ "$mode" = system ]; then
			as_root journalctl -b --no-pager -u openweir.service -u openweir.socket >"$output/journal.log" 2>&1 || :
		fi
		for log in "$root/var/log/apt/term.log" "$dpkg_log" "$root/var/log/dnf5.log"; do
			if [ -r "$log" ]; then cp "$log" "$output/" || :; fi
		done
		echo
		echo "$failures check(s) failed"
		exit 1
	fi
	echo
	echo "every check passed"
	exit 0
}

alive() {
	local state
	state=$(ps -o stat= -p "$1" 2>/dev/null) && [[ $state != Z* ]]
}

# -- what every mode checks once the package is in --------------------------------------------

installed_checks() {
	local format=$1 missing library
	section "Installed files ($format)"
	expect "package-format" "$(cat "$install_dir/package-format" 2>&1)" "$format"
	expect "/opt/openweir is root's and 0755" "$(stat -c '%U:%G %a' "$install_dir" 2>&1)" "root:root 755"
	expect "openweir.service enabled" "$(systemctl is-enabled openweir.service 2>&1)" enabled
	expect "openweir.socket enabled" "$(systemctl is-enabled openweir.socket 2>&1)" enabled
	check "desktop-file-validate" desktop-file-validate "$desktop_file"
	# The UI, and Cronet, which the service loads by its path; the service itself is static.
	for library in "$ui" "$install_dir/libcronet.so"; do
		if ! missing=$(ldd "$library" 2>&1); then
			fail "ldd ${library##*/}" "$missing"
		elif missing=$(grep 'not found' <<<"$missing"); then
			fail "ldd ${library##*/}" "libraries not found: $missing"
		else
			pass "ldd ${library##*/}"
		fi
	done
}

# deb: remove keeps the service's state, purge removes it.
remove_and_purge() {
	local keeps=$1
	section "Remove and purge"
	check "apt-get remove" as_root env DEBIAN_FRONTEND=noninteractive apt-get -q -y remove openweir
	expect "removed, configuration kept" "$(dpkg-query -W -f='${Status}' openweir 2>&1)" "deinstall ok config-files"
	refute "openweir-ui removed" test -e "$ui"
	refute "openweir.service removed" test -e "$unit_dir/openweir.service"
	if [ "$keeps" = state ]; then
		refute "openweir.socket stopped" systemctl --quiet is-active openweir.socket
		refute "openweir.service stopped" systemctl --quiet is-active openweir.service
		refute "control socket removed" test -S "$socket"
		check "state kept on remove" test -d "$state_dir"
	fi
	check "apt-get purge" as_root env DEBIAN_FRONTEND=noninteractive apt-get -q -y purge openweir
	refute "purged" dpkg-query -W openweir
	refute "state removed on purge" test -e "$state_dir"
	refute "cache removed on purge" test -e "$cache_dir"
}

# -- system: a hosted runner's virtual machine ------------------------------------------------

service_checks() {
	local action out
	section "Service"
	expect "openweir.socket active" "$(systemctl is-active openweir.socket 2>&1)" active
	check "control socket" test -S "$socket"
	if wait_until 30 systemctl --quiet is-active openweir.service; then
		pass "openweir.service active"
	else
		fail "openweir.service active" "$(systemctl status --no-pager openweir.service 2>&1)"
	fi
	expect "/var/lib/openweir is root's and 0711" "$(as_root stat -c '%U:%G %a' "$state_dir" 2>&1)" "root:root 711"
	if wait_until 15 journal_has "$started version=$version "; then
		pass "journal: service started"
	else
		fail "journal: service started" "no '$started version=$version' in: $(as_root journalctl -b --no-pager -o cat -u openweir.service 2>&1 | tail -20)"
	fi
	for action in take-over install-update; do
		if ! out=$(pkaction --action-id "org.openweir.app.$action" --verbose 2>&1); then
			fail "pkaction $action" "$out"
		elif ! grep -Eq 'implicit active: +auth_admin' <<<"$out" ||
			! grep -Fq "org.freedesktop.policykit.exec.path -> $install_dir/openweir-service" <<<"$out" ||
			! grep -Fq "org.freedesktop.policykit.exec.argv1 -> -$action" <<<"$out"; then
			fail "pkaction $action" "not auth_admin for the service's -$action: $out"
		else
			pass "pkaction $action"
		fi
	done
}

journal_has() {
	as_root journalctl -b --no-pager -o cat -u openweir.service | grep -Fq -- "$1"
}

invocation() {
	systemctl show -p InvocationID --value openweir.service
}

# The routing that -cleanup must restore: the rules of both families, the pinned table, the
# device, and where traffic to the test networks below goes.
routing() {
	local family address
	for family in -4 -6; do
		echo "rules $family:"
		ip "$family" rule show
		echo "table $table $family:"
		ip "$family" route show table "$table"
	done
	echo "device:"
	ip link show "$device" 2>&1
	for address in 198.51.100.1 203.0.113.1 2001:db8:1::1 2001:db8:2::1; do
		echo "route to $address:"
		ip route get "$address" 2>&1
	done
}

routing_is() {
	[ "$(routing)" = "$1" ]
}

# What a TUN that crashed under strict-route leaves, on documentation networks only, so that the
# runner's own traffic never goes through it: a rule into the pinned table and an unreachable
# rule at the end of the range, a route in the table, a persistent TUN, and, where
# systemd-resolved runs, a DNS server for it.
crash_leftovers() {
	local last=$((rule_index + 10))
	as_root ip tuntap add dev "$device" mode tun &&
		as_root ip link set "$device" up &&
		as_root ip -4 rule add pref "$rule_index" to 198.51.100.0/24 lookup "$table" &&
		as_root ip -4 rule add pref "$last" to 203.0.113.0/24 unreachable &&
		as_root ip -4 route add blackhole 198.51.100.0/24 table "$table" &&
		as_root ip -6 rule add pref "$rule_index" to 2001:db8:1::/48 lookup "$table" &&
		as_root ip -6 rule add pref "$last" to 2001:db8:2::/48 unreachable &&
		as_root ip -6 route add blackhole 2001:db8:1::/48 table "$table" || return 1
	if command -v resolvectl >/dev/null; then
		as_root resolvectl dns "$device" 192.0.2.53 &&
			as_root resolvectl domain "$device" openweir-smoke.test ||
			echo "note: systemd-resolved took no settings for $device"
	fi
}

crash_cleanup() {
	local before during after pid id
	section "SIGKILL and clean-up"
	before=$(routing)
	if ! crash_leftovers; then
		fail "crash leftovers" "could not make the routing a crashed TUN leaves: $(routing)"
		return
	fi
	during=$(routing)
	if [ "$during" = "$before" ]; then
		fail "crash leftovers" "the routing did not change: $during"
		return
	fi
	pid=$(systemctl show -p MainPID --value openweir.service)
	id=$(invocation)
	if [ -z "$pid" ] || [ "$pid" = 0 ]; then
		fail "kill -9" "openweir.service has no main process"
		return
	fi
	as_root kill -KILL "$pid"
	# ExecStopPost cleans up after the crash, Restart=always starts it again after ExecStartPre.
	if wait_until 60 restarted "$id"; then
		pass "restarted after kill -9"
	else
		fail "restarted after kill -9" "$(systemctl status --no-pager openweir.service 2>&1)"
	fi
	if wait_until 15 routing_is "$before"; then
		pass "kill -9: IPv4 and IPv6 routing restored"
	else
		after=$(routing)
		fail "kill -9: IPv4 and IPv6 routing restored" "$(diff <(echo "$before") <(echo "$after"))"
	fi
}

# restarted <invocation>: the service runs again, as another invocation.
restarted() {
	local now
	now=$(invocation)
	[ -n "$now" ] && [ "$now" != "$1" ] && systemctl --quiet is-active openweir.service
}

# start_ui <name>: an offscreen UI as this user, with the environment a desktop would give it and
# no more, so that the preinst's runuser --quit finds it; ui_pid once it holds its lock.
start_ui() {
	mkdir -p "$output"
	env -i HOME="$HOME" USER="$user" LOGNAME="$user" PATH=/usr/bin:/bin \
		QT_QPA_PLATFORM=offscreen QT_FORCE_STDERR_LOGGING=1 \
		"$ui" --background >"$output/ui-$1.log" 2>&1 &
	ui_pid=$!
	if ! wait_until 120 holds_lock "$ui_pid"; then
		if alive "$ui_pid"; then
			kill "$ui_pid" 2>/dev/null
			fail "UI starts ($1)" "it took no lock in 120 seconds: $(tail -20 "$output/ui-$1.log")"
		else
			local status=0
			wait "$ui_pid" || status=$?
			fail "UI starts ($1)" "it exited with $status while starting: $(tail -20 "$output/ui-$1.log")"
		fi
		return 1
	fi
	pass "UI starts ($1)"
	# The lock comes before the window; give the QML time to load.
	sleep 5
}

# QLockFile's first line is the process id.
holds_lock() {
	alive "$1" && [ "$(head -n 1 "$data_dir/ui.lock" 2>/dev/null)" = "$1" ]
}

# ui_left <name>: the UI is gone, exited 0, and recorded no crash.
ui_left() {
	local status=0
	if ! wait_until 30 gone "$ui_pid"; then
		kill -KILL "$ui_pid" 2>/dev/null
		fail "UI quits ($1)" "it still runs: $(tail -20 "$output/ui-$1.log")"
		return
	fi
	wait "$ui_pid" || status=$?
	expect "UI quits ($1): exit status" "$status" 0
	if compgen -G "$data_dir/crashes/ui-*.log" >/dev/null; then
		fail "UI quits ($1): no crash" "$(cat "$data_dir"/crashes/ui-*.log)"
	else
		pass "UI quits ($1): no crash"
	fi
}

gone() {
	! alive "$1"
}

quit_and_upgrade() {
	local id watcher upgraded gone_at line started_at
	section "UI --quit"
	if start_ui quit; then
		check "openweir-ui --quit" env -i HOME="$HOME" USER="$user" LOGNAME="$user" PATH=/usr/bin:/bin \
			timeout 60 "$ui" --quit
		ui_left quit
	fi

	section "Upgrade over a running UI"
	upgraded=$(version_of "$upgrade")
	start_ui upgrade || ui_pid=
	id=$(invocation)
	# When the UI went, to tell a UI that quit when asked from one the preinst ended later.
	rm -f "$output/ui-gone"
	if [ -n "$ui_pid" ]; then
		(
			while alive "$ui_pid"; do sleep 0.2; done
			date +%s >"$output/ui-gone"
		) &
		watcher=$!
	fi
	if ! apt_get install "$(realpath "$upgrade")"; then
		abort "upgrade" "apt-get install $upgrade failed"
	fi
	expect "upgraded" "$(dpkg-query -W -f='${Version}' openweir 2>&1)" "$upgraded-1"
	if [ -n "$ui_pid" ]; then
		ui_left upgrade
		wait "$watcher" 2>/dev/null
		gone_at=$(cat "$output/ui-gone" 2>/dev/null)
		line=$(grep " upgrade openweir:" "$dpkg_log" 2>/dev/null | tail -n 1)
		started_at=$(date -d "${line:0:19}" +%s 2>/dev/null)
		if [ -z "$gone_at" ] || [ -z "$started_at" ]; then
			fail "UI quit when the upgrade asked" "no times to compare: '$line', '$gone_at'"
		elif [ $((gone_at - started_at)) -ge $((quit_wait - 3)) ]; then
			fail "UI quit when the upgrade asked" "it went $((gone_at - started_at)) s after dpkg began, as the preinst's SIGTERM ends a UI that did not quit"
		else
			pass "UI quit when the upgrade asked"
		fi
	fi
	if wait_until 60 restarted "$id"; then
		pass "upgrade restarted the service"
	else
		fail "upgrade restarted the service" "$(systemctl status --no-pager openweir.service 2>&1)"
	fi
	expect "openweir.service enabled after the upgrade" "$(systemctl is-enabled openweir.service 2>&1)" enabled
	expect "openweir.socket enabled after the upgrade" "$(systemctl is-enabled openweir.socket 2>&1)" enabled
}

# version_of <package>: X.Y.Z of OpenWeir-X.Y.Z-linux-<arch>.<deb|rpm>.
version_of() {
	local name=${1##*/}
	if [[ $name =~ ^OpenWeir-([0-9]+\.[0-9]+\.[0-9]+)-linux-(x64|arm64)\.(deb|rpm)$ ]]; then
		echo "${BASH_REMATCH[1]}"
	fi
}

system_mode() {
	[ -n "$upgrade" ] && [ -f "$upgrade" ] || abort usage "system needs the next version's .deb too"
	[ -n "$(version_of "$upgrade")" ] || abort usage "not a package as docs/release.md names it: $upgrade"
	[ "$(id -u)" -ne 0 ] || abort usage "run it as the runner's user: the service turns a UI of root away"
	user=$(id -un)
	section "Install"
	apt_get update || abort "apt-get update" "the package lists did not update"
	if ! command -v desktop-file-validate >/dev/null; then
		apt_get install --no-install-recommends desktop-file-utils || fail "desktop-file-utils" "apt-get could not install it"
	fi
	apt_get install "$(realpath "$package")" || abort "install" "apt-get install $package failed"
	installed_checks deb
	check "dpkg --verify" dpkg --verify openweir
	service_checks
	crash_cleanup
	quit_and_upgrade
	remove_and_purge state
}

# -- deb, rpm: containers ---------------------------------------------------------------------

deb_mode() {
	section "Install"
	apt_get update || abort "apt-get update" "the package lists did not update"
	apt_get install --no-install-recommends desktop-file-utils || fail "desktop-file-utils" "apt-get could not install it"
	apt_get install "$(realpath "$package")" || abort "install" "apt-get install $package failed"
	installed_checks deb
	check "dpkg --verify" dpkg --verify openweir
	remove_and_purge none
}

rpm_mode() {
	local requires out requirement
	section "Install"
	dnf_install desktop-file-utils || fail "desktop-file-utils" "dnf could not install it"
	dnf_install "$(realpath "$package")" || abort "install" "dnf install $package failed"
	installed_checks rpm
	check "rpm -V" rpm -V openweir
	if ! requires=$(rpm -q --requires openweir 2>&1); then
		fail "rpm -q --requires" "$requires"
	else
		echo "$requires"
		for requirement in systemd /usr/bin/pkexec 'libc.so.6()(64bit)'; do
			if grep -Fqx "$requirement" <<<"$requires"; then
				pass "requires $requirement"
			else
				fail "requires $requirement" "not among: $requires"
			fi
		done
	fi
	section "Remove"
	if out=$(timeout 600 dnf -y remove openweir 2>&1); then
		pass "dnf remove"
	else
		fail "dnf remove" "$out"
	fi
	refute "removed" rpm -q openweir
	refute "openweir-ui removed" test -e "$ui"
	refute "openweir.service removed" test -e "$unit_dir/openweir.service"
}

if [ "${GITHUB_ACTIONS:-}" != true ]; then
	echo "This installs a root service: run it only on a disposable GitHub Actions machine." >&2
	exit 2
fi
if [ -z "$package" ] || [ ! -f "$package" ]; then
	abort usage "no package: '$package'"
fi
version=$(version_of "$package")
[ -n "$version" ] || abort usage "not a package as docs/release.md names it: $package"
case "$mode" in
system) system_mode ;;
deb) deb_mode ;;
rpm) rpm_mode ;;
*) abort usage "unknown mode '$mode': system, deb or rpm" ;;
esac
finish
