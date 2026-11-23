#!/bin/sh
# Before the package's files go in. deb: preinst install|upgrade <old version>; rpm: %pre 1 on
# an installation, 2 or more on an upgrade.
set -e
# @functions@

# The running UIs quit before their files are replaced; the old UI asks them.
case "$1" in
upgrade)
	quit_uis
	;;
[0-9]*)
	if [ "$1" -ge 2 ]; then
		quit_uis
	fi
	;;
esac
exit 0
