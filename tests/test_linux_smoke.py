"""package/linux/smoke-test.sh, which CI's linux-install job runs as root's packages go in and
out of a disposable machine, run here against a temporary root with stand-ins for apt-get,
dpkg, dnf, rpm, systemctl, journalctl, ip, pkaction and the UI: every check passes when the
machine behaves, and one that does not names itself in an error annotation. What the stand-ins
cannot show (a real systemd, polkit, routing and Qt) only CI's run shows."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from openweir import product

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "package" / "linux" / "smoke-test.sh"

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bash") is None or shutil.which("ps") is None,
    reason="GNU date and stat, ps and /bin/bash, as on the Linux runners",
)

# The stand-ins keep what they did in $FAKE_STATE: one file per fact.
COMMON = """\
#!/bin/bash
state=$FAKE_STATE
root=$OPENWEIR_SMOKE_ROOT
get() { cat "$state/$1" 2>/dev/null; }
put() { printf '%s' "$2" > "$state/$1"; }
"""

FAKES = {
    "sudo": '#!/bin/bash\nexec "$@"\n',
    "sleep": "#!/bin/bash\nexit 0\n",
    "desktop-file-validate": '#!/bin/bash\n[ -f "$1" ]\n',
    "resolvectl": "#!/bin/bash\nexit 0\n",
    "ldd": COMMON
    + """
echo "	linux-vdso.so.1 (0x0000)"
echo "	libc.so.6 => /lib/libc.so.6 (0x0000)"
if [ -n "$FAKE_MISSING_LIBRARY" ] && [ "${1##*/}" = openweir-ui ]; then
	echo "	$FAKE_MISSING_LIBRARY => not found"
fi
""",
    "stat": COMMON
    + """
path=${!#}
case "$path" in
"$root/opt/openweir") echo "root:root 755" ;;
"$root/var/lib/openweir") echo "root:root 711" ;;
*) exec /usr/bin/stat "$@" ;;
esac
""",
    "systemctl": COMMON
    + """
quiet=
if [ "$1" = --quiet ]; then quiet=1; shift; fi
case "$1" in
is-enabled)
	value=$(get "enabled-$2"); value=${value:-disabled}
	[ -n "$quiet" ] || echo "$value"
	[ "$value" = enabled ] ;;
is-active)
	value=$(get "active-$2"); value=${value:-inactive}
	[ -n "$quiet" ] || echo "$value"
	[ "$value" = active ] ;;
show)
	case "$3" in
	MainPID) get mainpid ;;
	InvocationID) get invocation ;;
	esac ;;
status) echo "openweir.service - OpenWeir service" ;;
*) echo "unexpected systemctl $*" >&2; exit 2 ;;
esac
""",
    "journalctl": COMMON
    + """
if [ -n "$(get invocation)" ]; then
	echo "time=2026-11-23T10:00:00Z level=INFO msg=\\"service started\\"" \\
		"version=$FAKE_VERSION core=v1 owned=false"
fi
""",
    "pkaction": COMMON
    + """
action=${2#org.openweir.app.}
cat <<EOF
org.openweir.app.$action:
  implicit any:      no
  implicit inactive: no
  implicit active:   auth_admin
  annotation:        org.freedesktop.policykit.exec.path -> $root/opt/openweir/openweir-service
  annotation:        org.freedesktop.policykit.exec.argv1 -> -$action
EOF
""",
    # A crash's leftovers are lines in $state/leftovers, which the service's -cleanup clears.
    "ip": COMMON
    + """
args="$*"
leftovers() { grep "^$1 " "$state/leftovers" 2>/dev/null; }
case "$args" in
"tuntap add dev openweir0 mode tun") echo "device openweir0" >> "$state/leftovers" ;;
"link set openweir0 up") ;;
"-4 rule add "* | "-6 rule add "* | "-4 route add "* | "-6 route add "*)
	echo "${1#-}${2} ${*:4}" >> "$state/leftovers" ;;
"-4 rule show" | "-6 rule show")
	echo "0:	from all lookup local"
	leftovers "${1#-}rule"
	echo "32766:	from all lookup main" ;;
"-4 route show table 9347" | "-6 route show table 9347") leftovers "${1#-}route" ;;
"link show openweir0")
	if leftovers device > /dev/null; then echo "9: openweir0: <POINTOPOINT,UP>"; else
		echo 'Device "openweir0" does not exist.'; exit 1; fi ;;
"route get "*)
	if [ -s "$state/leftovers" ]; then echo "RTNETLINK answers: Network is unreachable"; exit 2; fi
	echo "$3 via 10.1.0.1 dev eth0 src 10.1.0.4 uid 1001" ;;
*) echo "unexpected ip $args" >&2; exit 2 ;;
esac
""",
    # SIGKILL to the service: systemd's ExecStopPost and ExecStartPre clean up, then it restarts.
    "kill": COMMON
    + """
if [ "$1" = -KILL ] && [ "$2" = "$(get mainpid)" ]; then
	[ -n "$FAKE_NO_CLEANUP" ] || rm -f "$state/leftovers"
	put invocation "$(( $(get invocation) + 1 ))"
	exit 0
fi
exec /bin/kill "$@"
""",
    "dpkg-query": COMMON
    + """
status=$(get status)
case "$*" in
"-W openweir") [ -n "$status" ] ;;
*'${Status}'*) [ -n "$status" ] && echo -n "$status" ;;
*'${Version}'*) [ -n "$status" ] && echo -n "$(get version)-1" ;;
*) echo "unexpected dpkg-query $*" >&2; exit 2 ;;
esac
""",
    "dpkg": COMMON + '[ "$*" = "--verify openweir" ] && [ -n "$(get status)" ]\n',
    "apt-get": COMMON
    + """
while [ $# -gt 0 ]; do
	case $1 in -o) shift 2 ;; -*) shift ;; *) break ;; esac
done
command=$1
shift
ui=$root/opt/openweir/openweir-ui
case $command in
update) ;;
install)
	for target in "$@"; do
		case $target in
		*.deb) ;;
		*) continue ;;
		esac
		[[ ${target##*/} =~ ^OpenWeir-([0-9.]+)-linux- ]]
		version=${BASH_REMATCH[1]}
		if [ "$(get status)" = "install ok installed" ]; then
			# preinst upgrade: the running UIs quit, or SIGTERM ends them 15 seconds later.
			line="upgrade openweir:arm64 $(get version)-1 $version-1"
			if [ -n "$FAKE_UI_IGNORES_QUIT" ]; then
				echo "$(date -d '-20 seconds' '+%F %T') $line" >> "$root/var/log/dpkg.log"
				pkill -TERM -f "$ui --background"
			else
				echo "$(date '+%F %T') $line" >> "$root/var/log/dpkg.log"
				"$ui" --quit
			fi
			put invocation "$(( $(get invocation) + 1 ))"
		else
			mkdir -p "$root/opt/openweir" "$root/usr/lib/systemd/system" \\
				"$root/usr/share/applications" "$root/run/openweir"
			cp "$FAKE_UI" "$ui"
			chmod 755 "$ui"
			echo deb > "$root/opt/openweir/package-format"
			touch "$root/opt/openweir/libcronet.so" "$root/opt/openweir/openweir-service"
			touch "$root/usr/lib/systemd/system/openweir."{service,socket}
			touch "$root/usr/share/applications/org.openweir.app.desktop"
			put enabled-openweir.service enabled
			put enabled-openweir.socket enabled
			if [ -n "$FAKE_SYSTEMD" ]; then
				# Relative: a temporary root's path may be too long for a socket's.
				(cd "$root/run/openweir" &&
					python3 -c 'import socket; socket.socket(socket.AF_UNIX).bind("control.sock")')
				mkdir -p "$root/var/lib/openweir/users" "$root/var/cache/openweir"
				put active-openweir.service active
				put active-openweir.socket active
				put mainpid 4242
				put invocation 1
			fi
		fi
		put status "install ok installed"
		put version "$version"
	done ;;
remove)
	rm -rf "$root/opt/openweir" "$root/usr/lib/systemd/system/openweir."* "$root/run/openweir"
	rm -f "$root/usr/share/applications/org.openweir.app.desktop"
	rm -f "$state"/enabled-* "$state"/active-*
	put status "deinstall ok config-files" ;;
purge)
	rm -rf "$root/var/lib/openweir" "$root/var/cache/openweir"
	rm -f "$state/status" ;;
*) echo "unexpected apt-get $command" >&2; exit 2 ;;
esac
""",
    "dnf": COMMON
    + """
args=()
for arg in "$@"; do case $arg in -y | --setopt=*) ;; *) args+=("$arg") ;; esac; done
case "${args[0]} ${args[1]}" in
"install desktop-file-utils") ;;
"install "*.rpm)
	mkdir -p "$root/opt/openweir" "$root/usr/lib/systemd/system" "$root/usr/share/applications"
	cp "$FAKE_UI" "$root/opt/openweir/openweir-ui"
	echo rpm > "$root/opt/openweir/package-format"
	touch "$root/opt/openweir/libcronet.so" "$root/usr/lib/systemd/system/openweir.service"
	touch "$root/usr/share/applications/org.openweir.app.desktop"
	put enabled-openweir.service enabled
	put enabled-openweir.socket enabled
	put status installed ;;
"remove openweir")
	rm -rf "$root/opt/openweir" "$root/usr/lib/systemd/system/openweir."*
	rm -f "$state/status" ;;
*) echo "unexpected dnf $*" >&2; exit 2 ;;
esac
""",
    "rpm": COMMON
    + """
case "$*" in
"-V openweir" | "-q openweir") [ -n "$(get status)" ] ;;
"-q --requires openweir")
	printf '%s\\n' /bin/sh 'libc.so.6()(64bit)' 'libc.so.6(GLIBC_2.34)(64bit)' \\
		systemd /usr/bin/pkexec ;;
*) echo "unexpected rpm $*" >&2; exit 2 ;;
esac
""",
}

# The UI: --background holds the lock until --quit or SIGTERM, as desktop/instance.py's does.
UI = """\
#!/bin/bash
data=$HOME/.local/share/OpenWeir
case "$1" in
--background)
	mkdir -p "$data"
	trap 'rm -f "$data/ui.lock"; exit 0' TERM
	echo $$ > "$data/ui.lock"
	echo OpenWeir >> "$data/ui.lock"
	while [ ! -e "$data/quit" ]; do sleep 0.05; done
	rm -f "$data/quit" "$data/ui.lock"
	exit 0 ;;
--quit)
	[ -e "$data/ui.lock" ] || exit 0
	touch "$data/quit"
	while [ -e "$data/ui.lock" ]; do sleep 0.05; done ;;
esac
"""


def run(tmp_path: Path, mode: str, *names: str, **env: str) -> subprocess.CompletedProcess[str]:
    root, state, bin_dir, home = (tmp_path / n for n in ("root", "state", "bin", "home"))
    for directory in (root / "var" / "log", state, bin_dir, home):
        directory.mkdir(parents=True)
    for name, text in FAKES.items():
        fake = bin_dir / name
        fake.write_text(text, encoding="utf-8")
        fake.chmod(0o755)
    ui = tmp_path / "ui"
    ui.write_text(UI, encoding="utf-8")
    ui.chmod(0o755)
    packages = []
    for name in names:
        (tmp_path / name).write_bytes(b"package")
        packages.append(str(tmp_path / name))
    return subprocess.run(
        ["bash", str(SCRIPT), mode, *packages],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(home),
            "GITHUB_ACTIONS": "true",
            "OPENWEIR_SMOKE_ROOT": str(root),
            "OPENWEIR_SMOKE_OUTPUT": str(tmp_path / "output"),
            "FAKE_STATE": str(state),
            "FAKE_UI": str(ui),
            "FAKE_VERSION": "1.2.3",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


DEB = "OpenWeir-1.2.3-linux-arm64.deb"
NEXT = "OpenWeir-1.2.4-linux-arm64.deb"


def errors(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in result.stdout.splitlines() if line.startswith("::error")]


def test_a_machine_that_behaves_passes_every_check(tmp_path: Path) -> None:
    result = run(tmp_path, "system", DEB, NEXT, FAKE_SYSTEMD="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not errors(result)
    passed = re.findall(r"^ok: (.+)$", result.stdout, re.M)
    for name in (
        "package-format",
        "openweir.service enabled",
        "ldd openweir-ui",
        "ldd libcronet.so",
        "control socket",
        "journal: service started",
        "pkaction take-over",
        "pkaction install-update",
        "kill -9: IPv4 and IPv6 routing restored",
        "openweir-ui --quit",
        "UI quit when the upgrade asked",
        "upgrade restarted the service",
        "state kept on remove",
        "state removed on purge",
    ):
        assert name in passed, name
    assert "every check passed" in result.stdout


@pytest.mark.parametrize("mode", ["deb", "rpm"])
def test_a_container_passes_every_check(tmp_path: Path, mode: str) -> None:
    result = run(tmp_path, mode, DEB if mode == "deb" else DEB.replace(".deb", ".rpm"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert not errors(result)
    passed = re.findall(r"^ok: (.+)$", result.stdout, re.M)
    assert "ldd openweir-ui" in passed
    assert ("purged" if mode == "deb" else "requires /usr/bin/pkexec") in passed


@pytest.mark.parametrize(
    ("env", "check", "problem"),
    [
        ({"FAKE_NO_CLEANUP": "1"}, "kill -9%3A IPv4 and IPv6 routing restored", "rule"),
        (
            {"FAKE_UI_IGNORES_QUIT": "1"},
            "UI quit when the upgrade asked",
            "as the preinst's SIGTERM ends a UI",
        ),
        ({"FAKE_MISSING_LIBRARY": "libxcb-cursor.so.0"}, "ldd openweir-ui", "libxcb-cursor.so.0"),
        ({"FAKE_VERSION": "9.9.9"}, "journal%3A service started", "no 'msg="),
    ],
)
def test_each_failed_check_names_itself(
    tmp_path: Path, env: dict[str, str], check: str, problem: str
) -> None:
    result = run(tmp_path, "system", DEB, NEXT, FAKE_SYSTEMD="1", **env)
    assert result.returncode == 1
    (line,) = errors(result)
    assert line.startswith(f"::error title=Linux system smoke test%3A {check}::")
    assert problem in line
    # The rest still ran.
    assert "ok: state removed on purge" in result.stdout
    assert "1 check(s) failed" in result.stdout


def test_it_runs_only_on_github_actions_and_wants_its_packages(tmp_path: Path) -> None:
    result = run(tmp_path / "local", "deb", DEB, GITHUB_ACTIONS="")
    assert result.returncode == 2
    assert "disposable GitHub Actions machine" in result.stderr
    result = run(tmp_path / "upgrade", "system", DEB, FAKE_SYSTEMD="1")
    assert result.returncode == 1
    assert errors(result) == [
        "::error title=Linux system smoke test%3A usage::system needs the next version's .deb too"
    ]
    result = run(tmp_path / "name", "deb", "openweir.deb")
    assert errors(result) == [
        "::error title=Linux deb smoke test%3A usage::not a package as docs/release.md names it: "
        f"{tmp_path / 'name' / 'openweir.deb'}"
    ]


def test_annotations_escape_what_github_reads_as_syntax(tmp_path: Path) -> None:
    functions = "; ".join(
        f"/^{name}()/,/^}}/p" for name in ("escape_data", "escape_property", "fail")
    )
    script = textwrap.dedent(
        f"""\
        source <(sed -n '{functions}' {SCRIPT})
        mode=deb
        failures=0
        fail 'a: b, c' $'50% done\\nnext line'
        """
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True, env=os.environ
    )
    assert result.stdout == (
        "::error title=Linux deb smoke test%3A a%3A b%2C c::50%25 done%0Anext line\n"
    )


def test_the_settings_are_the_products() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    settings = dict(re.findall(r"^(\w+)=(\S+)$", text, re.M))
    assert settings["install_dir"] == f"$root{product.LINUX_INSTALL_DIR}"
    assert settings["socket"] == f"$root{product.LINUX_SOCKET}"
    assert settings["state_dir"] == f"$root{product.LINUX_STATE_DIR}"
    assert settings["desktop_file"] == (
        f"$root/usr/share/applications/{product.LINUX_DESKTOP_ID}.desktop"
    )
    assert settings["device"] == product.LINUX_TUN_DEVICE
    assert int(settings["table"]) == product.LINUX_ROUTE_TABLE
    assert int(settings["rule_index"]) == product.LINUX_RULE_INDEX
    assert settings["ui"] == f"$install_dir/{product.LINUX_UI_EXECUTABLE}"
    # The preinst's wait for the UIs (package/linux/scripts/functions.sh).
    functions = (ROOT / "package" / "linux" / "scripts" / "functions.sh").read_text("utf-8")
    assert f"quit_wait={settings['quit_wait']}\n" in functions
