#!/bin/sh
# After the package's files are in. deb: postinst configure <old version, empty on an
# installation>; rpm: %post 1 on an installation, 2 or more on an upgrade.
set -e
# @functions@

# An installation enables and starts the socket and the service. dpkg configures a package that
# was removed but not purged with its old version too, so whenever neither unit is enabled the
# scripts do the same; to keep OpenWeir's service off, mask it. An upgrade restarts the service
# if it runs, and the tunnel's owner gets it back (the UIs have quit without disconnecting).
# As with debhelper, a unit that fails to start does not fail the installation; systemctl says
# why, and the journal has the rest.
configure() {
	if systemd_running; then
		systemctl daemon-reload || :
	fi
	if [ "$1" = installed ] || ! units_enabled; then
		if enable_units && systemd_running; then
			systemctl start openweir.socket openweir.service || :
		fi
	elif systemd_running; then
		systemctl try-restart openweir.service || :
	fi
}

case "$1" in
configure)
	if [ -z "$2" ]; then
		configure installed
	else
		configure upgraded
	fi
	;;
1)
	configure installed
	;;
[0-9]*)
	configure upgraded
	;;
esac
exit 0
