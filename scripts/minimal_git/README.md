# Container Git source and rebuild recipe

This image contains a custom, reduced-feature build of Debian's Git source,
not Debian's stock Git binary. The original source identity is retained in the
installed package's `Source` field; the `+boundver1` binary suffix is not a claim
that any Git vulnerability has been patched.

`/usr/share/licenses/git/source` accompanies each image with:

- the `.dsc` and every archive listed in its SHA256 section, including Debian's
  patches (applied by `dpkg-source -x`);
- authenticated APT source indexes, InRelease signatures, archive keyring and
  repository configuration used when obtaining those inputs;
- the complete Dockerfile and custom build, snapshot and validation scripts;
- Boundver's MIT licence for those custom recipes, separate from Git's licence;
- source/recipe hashes, effective compiler flags, builder package versions,
  architecture and the installed executable hashes in `build.json`;
- `SHA256SUMS`, covering every regular file in this directory except itself.

Full Git COPYING is in `/usr/share/licenses/git/COPYING`; Debian's copyright
record is also kept at `/usr/share/doc/git/copyright`. The Git source includes
its bundled components and their notices. This bundle is not a licensing
certification for the entire Python/Debian image.

To reproduce the Git package, use the bundled Dockerfile as the base/snapshot
record and restore `recipe/scripts/minimal_git` into a clean build context.
The Git stage requires only that directory and `recipe/Dockerfile`:

```sh
docker build --target git-builder -t boundver-git-source .
docker create --name boundver-git-source-export boundver-git-source
docker cp boundver-git-source-export:/out/git-minimal.deb .
docker rm boundver-git-source-export
```

Network access is needed to retrieve the pinned base and signed snapshot.
`build.sh` extracts the same verified source twice into different directories,
uses Debian hardening flags with explicit PIE/RELRO/NOW, tests SHA1 collision
detection, audits every installed ELF and script interpreter, and requires
byte-identical `.deb` outputs. `SOURCE_DATE_EPOCH` and the Git source version
are fixed in the Dockerfile. Build concurrency is bounded at two jobs.

The source bundle and package are constructed inside the build, without hidden
repository inputs or additional patches. The `.deb` is an intermediate, not a
separately published product. Distributing it separately requires accompanying
source too. The manifest records integrity, not an independent signature of
Boundver's custom build; APT authentication applies to the Debian inputs.

HTTP(S) client helpers, Perl, Tcl/Tk, Python helpers, gettext, Expat and OpenSSL
integration are disabled. Local Git inspection and SHA-1/SHA-256 repositories
are supported for Boundver; optional executable sample hooks are not installed.
This is not a general-purpose Git image. Other Git
transports are not a security sandbox: run the built-in CLI non-root, with no
network, capabilities or writable verification mounts as documented.
