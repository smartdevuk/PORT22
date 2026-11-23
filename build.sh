#!/bin/sh
set -eu
cd "$(dirname "$0")"
output_dir=${1:-dist}
mkdir -p "$output_dir"
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
install -d "$stage/DEBIAN" "$stage/usr/share/port22" "$stage/usr/bin" "$stage/usr/share/applications" "$stage/usr/share/icons/hicolor/256x256/apps" "$stage/usr/share/doc/port22"
install -m 644 port22/*.py "$stage/usr/share/port22/"
install -m 755 launcher "$stage/usr/bin/port22"
install -m 644 assets/port22.desktop "$stage/usr/share/applications/"
install -m 644 assets/port22.png "$stage/usr/share/icons/hicolor/256x256/apps/"
install -m 644 README.md "$stage/usr/share/doc/port22/"
install -m 644 LICENSE "$stage/usr/share/doc/port22/copyright"
cat > "$stage/DEBIAN/control" <<'META'
Package: port22
Version: 1.2.2
Architecture: all
Section: net
Priority: optional
Replaces: violet-relay (<= 1.0.0)
Conflicts: violet-relay (<= 1.0.0)
Maintainer: PORT22 project
Depends: python3 (>= 3.9), python3-pyqt5, python3-paramiko (>= 2.12), python3-cryptography, putty-tools
Description: A native, dark violet SFTP client for ChromeOS Linux
 Browse saved SSH-key connections, upload and download directory trees in
 parallel, and recover queued transfers after interruptions.
META
chmod 755 "$stage" "$stage/DEBIAN"
dpkg-deb --root-owner-group -Zxz --build "$stage" "$output_dir/port22_1.2.2_all.deb"
