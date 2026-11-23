# What the maintainer scripts share. scripts/build_linux.py copies this file into each of them
# where they say so, since nothing of the package is on disk when preinst runs.
# The tests replace the settings below to run the scripts in a temporary root.
ui=/opt/openweir/openweir-ui
proc=/proc
systemd_dir=/run/systemd/system
state_dir=/var/lib/openweir
cache_dir=/var/cache/openweir
quit_wait=15

# systemd runs the machine: not a container or a chroot being set up, where systemctl can only
# enable units.
systemd_running() {
	[ -d "$systemd_dir" ]
}

# The process ids of the running UIs, as root sees them.
ui_pids() {
	for entry in "$proc"/[0-9]*; do
		if [ "$(readlink "$entry/exe" 2>/dev/null)" = "$ui" ]; then
			echo "${entry##*/}"
		fi
	done
}

# Asks every running UI to quit, as its user, the way the Windows installer does, and gives them
# $quit_wait seconds before it sends SIGTERM to those left. Either way a UI leaves without
# disconnecting: the service keeps the tunnel through an upgrade.
quit_uis() {
	pids=$(ui_pids)
	if [ -z "$pids" ]; then
		return 0
	fi
	if command -v runuser >/dev/null 2>&1; then
		for uid in $(for pid in $pids; do stat -c %u "$proc/$pid" 2>/dev/null || :; done | sort -u); do
			user=$(getent passwd "$uid" | cut -d: -f1) || :
			if [ -n "$user" ]; then
				timeout "$quit_wait" runuser -u "$user" -- "$ui" --quit >/dev/null 2>&1 &
			fi
		done
	fi
	waited=0
	while [ "$waited" -lt "$quit_wait" ] && [ -n "$(ui_pids)" ]; do
		sleep 1
		waited=$((waited + 1))
	done
	for pid in $(ui_pids); do
		kill -TERM "$pid" 2>/dev/null || :
	done
	wait
}

# Either unit is enabled.
units_enabled() {
	systemctl --quiet is-enabled openweir.socket 2>/dev/null ||
		systemctl --quiet is-enabled openweir.service 2>/dev/null
}

# Enables the units, unless an administrator masked them: then it says so and fails.
# openweir.service enables the socket too (Also=). systemctl does this without a running systemd.
enable_units() {
	for unit in openweir.socket openweir.service; do
		case "$(systemctl is-enabled "$unit" 2>/dev/null || :)" in
		masked | masked-runtime)
			echo "openweir: $unit is masked; leaving the units as they are" >&2
			return 1
			;;
		esac
	done
	systemctl --no-reload enable openweir.service
}
