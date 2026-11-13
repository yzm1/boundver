# Container Git source and rebuild recipe

This image contains a custom, reduced-feature build of upstream Git 2.50.2,
not Debian's stock Git binary. This upstream security release contains fixes
for the November Git advisories; the build does not backport or invent patches.
The package records its actual upstream source version, with `+boundver1`
identifying Boundver's packaging rather than an additional security fix.

The kernel.org archive and detached signature have pinned sizes and SHA-256
digests. The vendored public key is pinned too. Before extraction, `gpgv`
verifies the uncompressed tar against primary fingerprint
`96E07AF25771955980DAD10020D04E5A713660A7` and its pinned signing subkey.
No personal keyring, secret key, agent or keyserver is used. Source URL,
redirect, size, deadline, version, digest or signature mismatches fail the build;
there is no fallback to the older source.

`/usr/share/licenses/git/source` accompanies each image with:

- the upstream archive, detached signature, public verification key and
  canonical `upstream.json` authentication record;
- authenticated APT package indexes, InRelease signatures, archive keyring and
  repository configuration for the compiler and runtime dependencies;
- the complete Dockerfile and custom build, snapshot and validation scripts;
- Boundver's MIT licence for those custom recipes, separate from Git's licence;
- source/recipe hashes, effective compiler flags, builder package versions,
  architecture and the installed executable hashes in `build.json`;
- `SHA256SUMS`, covering every regular file in this directory except itself.

Full Git COPYING is in `/usr/share/licenses/git/COPYING` and
`/usr/share/doc/git/copyright`. The original Git source includes
its bundled components and their notices. This bundle is not a licensing
certification for the entire Python/Debian image.

To reproduce the Git package, restore the contents of `recipe/` into a clean
build context: `Dockerfile` and `LICENSE` at its root, with the scripts under
`scripts/minimal_git/`. The Git stage requires exactly those inputs:

```sh
docker build --target git-builder -t boundver-git-source .
docker create --name boundver-git-source-export boundver-git-source
docker cp boundver-git-source-export:/out/git-minimal.deb .
docker rm boundver-git-source-export
```

Network access is needed to retrieve the pinned base, signed snapshot and
hash-pinned upstream archive/signature. The public verification key is vendored.
`build.sh` extracts the same verified source twice into different directories,
uses Debian hardening flags with explicit PIE/RELRO/NOW, tests SHA1 collision
detection, audits every installed ELF and script interpreter, and requires
byte-identical `.deb` outputs. `SOURCE_DATE_EPOCH` and the Git source version
are fixed in the Dockerfile. Build concurrency is bounded at two jobs.

The source bundle and package are constructed inside the build, without hidden
repository inputs or additional patches. The `.deb` is an intermediate, not a
separately published product. Distributing it separately requires accompanying
source too. The manifest records integrity, not an independent signature of
Boundver's custom build. Upstream signs Git; APT authenticates Debian build and
runtime packages. Neither substitutes for final image scans or code review.

HTTP(S) client helpers, Perl, Tcl/Tk, Python helpers, gettext, Expat and OpenSSL
integration are disabled. Local Git inspection and SHA-1/SHA-256 repositories
are supported for Boundver; optional executable sample hooks are not installed.
This is not a general-purpose Git image. Other Git
transports are not a security sandbox: run the built-in CLI non-root, with no
network, capabilities or writable verification mounts as documented.
