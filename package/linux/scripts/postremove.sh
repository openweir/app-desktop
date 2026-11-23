#!/bin/sh
# After the package's files went. deb: postrm remove|purge|upgrade|...; rpm: %postun 0 on a
# removal, 1 or more on an upgrade.
set -e
# @functions@

case "$1" in
remove | 0)
	if systemd_running; then
		systemctl daemon-reload || :
	fi
	;;
purge)
	# What the service kept: the owner, each user's mihomo home and crash logs, and the update
	# helper's copies. The users' own data directories are theirs.
	rm -rf -- "$state_dir" "$cache_dir"
	;;
esac
exit 0
