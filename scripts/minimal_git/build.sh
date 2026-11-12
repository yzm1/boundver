#!/bin/sh
# Build the same source twice, checking hardening, dependencies and byte identity.
set -eu
: "${GIT_SOURCE_VERSION:?Dockerfile must supply the exact upstream source version}"
: "${SOURCE_DATE_EPOCH:?Dockerfile must supply the snapshot timestamp}"
export LC_ALL=C TZ=UTC DEBIAN_FRONTEND=noninteractive
export DEB_BUILD_MAINT_OPTIONS='hardening=+all reproducible=+all'
mkdir /source /out /build
python -I /recipe/scripts/minimal_git/audit.py fetch-source /source "$GIT_SOURCE_VERSION"
arch=$(dpkg --print-architecture)
for variant in a b; do
  source_dir=/build/$variant
  stage=/build/stage-$variant
  mkdir "$source_dir"
  tar -xJf /source/git-2.50.2.tar.xz --strip-components=1 -C "$source_dir"
  cd "$source_dir"
  # dpkg-shlibdeps needs a source control file, not Debian source patches.
  mkdir debian
  printf 'Source: git\n\nPackage: git\nArchitecture: any\nDescription: Upstream Git for Boundver\n' >debian/control
  export CFLAGS="$(dpkg-buildflags --get CFLAGS) -fstack-protector-strong -fPIE -ffile-prefix-map=$source_dir=. -fdebug-prefix-map=$source_dir=."
  export CPPFLAGS="$(dpkg-buildflags --get CPPFLAGS) -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=3"
  export LDFLAGS="$(dpkg-buildflags --get LDFLAGS) -pie -Wl,-z,relro,-z,now"
  python -I /recipe/scripts/minimal_git/audit.py flags
  # No eval: flags are passed as single make assignments. Configuration and
  # hardening are identical for all/test-tool/install invocations.
  build_git() {
    make -j2 prefix=/usr \
      NO_CURL=YesPlease NO_EXPAT=YesPlease NO_GETTEXT=YesPlease \
      NO_PERL=YesPlease NO_TCLTK=YesPlease NO_PYTHON=YesPlease \
      NO_OPENSSL=YesPlease USE_LIBPCRE2=YesPlease \
      "CFLAGS=$CFLAGS" "CPPFLAGS=$CPPFLAGS" "LDFLAGS=$LDFLAGS" "$@"
  }
  build_git all t/helper/test-tool
  test "$(./git version)" = 'git version 2.50.2'
  ./t/helper/test-tool sha1-is-sha1dc
  if ./t/helper/test-tool sha1 <t/t0013/shattered-1.pdf 2>collision.err; then
    printf 'SHA1 collision was accepted\n' >&2; exit 1
  fi
  grep -F collision collision.err
  grep -F 38762cf7f55934b34d179ae6a4c80cadccbb7f0a collision.err
  # Strip before install so hardlinked aliases remain hardlinks.
  strip --strip-unneeded git git-shell scalar git-daemon git-http-backend \
    git-imap-send git-sh-i18n--envsubst
  build_git "DESTDIR=$stage" install
  # These are optional hook examples, including a Perl fsmonitor example.
  # Boundver suppresses repository hooks; don't ship dormant executable samples.
  find "$stage/usr/share/git-core/templates/hooks" -maxdepth 1 \
    -type f -name '*.sample' -delete
  python -I /recipe/scripts/minimal_git/audit.py audit "$stage" "$arch" /build/audit-$variant.json
  mkdir -p "$stage/usr/share/licenses/git" "$stage/usr/share/doc/git"
  cp COPYING "$stage/usr/share/licenses/git/COPYING"
  cp COPYING "$stage/usr/share/doc/git/copyright"
  python -I /recipe/scripts/minimal_git/audit.py bundle \
    "$stage" /source /build/audit-$variant.json "$GIT_SOURCE_VERSION"
  find "$stage" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
  DPKG_DEB_THREADS_MAX=2 dpkg-deb --build --root-owner-group \
    --uniform-compression -Zxz -z6 "$stage" /out/git-$variant.deb
done
cmp /out/git-a.deb /out/git-b.deb
sha256sum /out/git-a.deb /out/git-b.deb
mv /out/git-a.deb /out/git-minimal.deb
rm /out/git-b.deb
