#!/bin/sh
# Before the package's files go. deb: prerm remove|upgrade|deconfigure|failed-upgrade; rpm:
# %preun 0 on a removal, 1 or more on an upgrade, which needs nothing here.
set -e
# @functions@

# A removal stops the service, which ends the tunnel, and disables the units while their files
# are still there. Failures do not hold the removal up; systemctl says what they were.
case "$1" in
remove | 0)
	quit_uis
	if systemd_running; then
		systemctl stop openweir.socket openweir.service || :
	fi
	systemctl --no-reload disable openweir.service openweir.socket || :
	;;
esac
exit 0
