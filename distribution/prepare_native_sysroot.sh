#!/usr/bin/env bash
# Optional Debian-only, workspace-local native headers; no root/system mutation.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CACHE=${MODELBUILDER_BUILD_CACHE:-"$ROOT/.cache"}
mkdir -p "$CACHE/apt/lists/partial" "$CACHE/apt/archives/partial" "$CACHE/apt/debs" "$CACHE/sysroot"
apt-get -o Dir::State::lists="$CACHE/apt/lists" -o Dir::Cache="$CACHE/apt" -o Debug::NoLocking=1 update
cd "$CACHE/apt/debs"
apt-get -o Dir::State::lists="$CACHE/apt/lists" download libx11-dev libxkbfile-dev libsecret-1-dev libglib2.0-dev libgio-2.0-dev libxcb1-dev libxau-dev libxdmcp-dev x11proto-dev xtrans-dev libx11-6 libxkbfile1 libsecret-1-0 libglib2.0-0t64 libffi-dev libpcre2-dev libblkid-dev libmount-dev libxcb1 libxau6 libxdmcp6 libgcrypt20-dev libgpg-error-dev libsysprof-capture-4-dev
for package in *.deb; do dpkg-deb -x "$package" "$CACHE/sysroot"; done
sha256sum ./*.deb > "$ROOT/evidence/gui/distribution/native-packages.sha256"
printf 'Use PKG_CONFIG_SYSROOT_DIR=%q and PKG_CONFIG_PATH=%q\n' "$CACHE/sysroot" "$CACHE/sysroot/usr/lib/x86_64-linux-gnu/pkgconfig:$CACHE/sysroot/usr/share/pkgconfig"
