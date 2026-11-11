#!/bin/sh
# Fail closed if the pinned base changes its APT inputs.
set -eu
grep -Fqx '# http://snapshot.debian.org/archive/debian/20261104T212339Z' /etc/apt/sources.list.d/debian.sources
grep -Fqx '# http://snapshot.debian.org/archive/debian-security/20261104T212339Z' /etc/apt/sources.list.d/debian.sources
sed -i \
  -e 's|http://deb.debian.org/debian-security|https://snapshot.debian.org/archive/debian-security/20261108T120000Z|' \
  -e 's|http://deb.debian.org/debian|https://snapshot.debian.org/archive/debian/20261108T120000Z|' \
  /etc/apt/sources.list.d/debian.sources
printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99snapshot
if [ "${1-}" = source ]; then
  sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources
fi
# Authentication remains enabled. Only snapshot expiry is disabled.
apt-get update
