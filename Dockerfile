# boundver — CI container image
#
# Build:
#   docker build -t boundver .
#
# Run a read-only verification (mount your repo):
#   docker run --rm --network none --read-only --cap-drop ALL \
#     --security-opt no-new-privileges \
#     -v "$(pwd):/repo:ro" -w /repo boundver verify --source head
# Generation deliberately needs a writable repository mount and should remain
# an explicit, trusted operation.
#
# Release images are published at ghcr.io/yzm1/boundver. Prefer an immutable
# version tag or digest; the Dockerfile is also exercised from exact source in CI.

# Keep the multi-architecture base digest and Debian package snapshot explicit.
# The base records the older snapshot it was built from; the runtime refreshes
# inherited packages from the newer immutable security-review snapshot below.
FROM python:3.12.16-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS git-builder

ENV SOURCE_DATE_EPOCH=1794139200
ARG GIT_SOURCE_VERSION=1:2.47.3-0+deb13u1
COPY scripts/minimal_git/snapshot.sh /build-inputs/snapshot.sh
RUN sh /build-inputs/snapshot.sh source \
    && apt-get install -y --no-install-recommends \
      build-essential dpkg-dev zlib1g-dev libpcre2-dev
COPY Dockerfile LICENSE /recipe/
COPY scripts/minimal_git /recipe/scripts/minimal_git
RUN sh /recipe/scripts/minimal_git/build.sh


FROM python:3.12.16-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS builder

# Clamp archive timestamps to the pinned snapshot's UTC timestamp so identical
# source inputs produce the same local wheel bytes.
ENV SOURCE_DATE_EPOCH=1794139200

WORKDIR /build
COPY scripts/requirements/action.lock /locks/action.lock
RUN python -I -m pip download \
      --isolated \
      --disable-pip-version-check \
      --no-cache-dir \
      --index-url https://pypi.org/simple \
      --require-hashes \
      --only-binary=:all: \
      --dest /wheelhouse \
      --requirement /locks/action.lock \
    && python -I -m pip install \
      --isolated \
      --disable-pip-version-check \
      --no-cache-dir \
      --no-index \
      --require-hashes \
      --only-binary=:all: \
      --find-links=/wheelhouse \
      --requirement /locks/action.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -I -m pip wheel \
      --isolated \
      --disable-pip-version-check \
      --no-cache-dir \
      --no-deps \
      --no-build-isolation \
      --wheel-dir /wheelhouse \
      .


FROM python:3.12.16-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# pip compiles installed modules in this stage. Force hash-based bytecode so
# retries do not embed the wall clock in hundreds of .pyc headers.
ENV SOURCE_DATE_EPOCH=1794139200

ARG BOUNDVER_VERSION=development
ARG BOUNDVER_REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/yzm1/boundver" \
      org.opencontainers.image.url="https://yzm1.github.io/boundver/" \
      org.opencontainers.image.documentation="https://yzm1.github.io/boundver/" \
      org.opencontainers.image.title="boundver" \
      org.opencontainers.image.description="A Git-aware lockfile and CI check for contracts shared across components" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="$BOUNDVER_VERSION" \
      org.opencontainers.image.revision="$BOUNDVER_REVISION"

# Git is built from authenticated Debian source without HTTP client libraries.
# Its complete source/recipe bundle is package-owned outside dpkg's doc filter.
COPY scripts/minimal_git/snapshot.sh /build-inputs/snapshot.sh
COPY --from=git-builder /out/git-minimal.deb /packages/git-minimal.deb
RUN export DEBIAN_FRONTEND=noninteractive \
    && sh /build-inputs/snapshot.sh \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends /packages/git-minimal.deb \
    && test -s /usr/share/licenses/git/COPYING \
    && cd /usr/share/licenses/git/source && sha256sum -c SHA256SUMS \
    && apt-get clean \
    && rm -rf /build-inputs /packages \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f \
      /var/cache/ldconfig/aux-cache \
      /var/log/apt/history.log \
      /var/log/apt/term.log \
      /var/log/dpkg.log

ARG BOUNDVER_UID=1000
ARG BOUNDVER_GID=1000
COPY --from=builder /wheelhouse /wheelhouse
COPY --from=builder /locks/action.lock /locks/action.lock
# Build/package managers are not part of the boundver runtime. Removing them
# also removes vendored dependency copies from the attack surface. The image
# always runs as an unprivileged user and needs no set-ID helper.
RUN python -I -m pip install \
      --isolated \
      --disable-pip-version-check \
      --no-cache-dir \
      --no-index \
      --require-hashes \
      --only-binary=:all: \
      --find-links=/wheelhouse \
      --requirement /locks/action.lock \
    && python -I -m pip install \
      --isolated \
      --disable-pip-version-check \
      --no-cache-dir \
      --no-index \
      --no-deps \
      /wheelhouse/boundver-*.whl \
    && python -I -m pip uninstall --yes pip setuptools wheel \
    && rm -rf /wheelhouse /locks \
    && groupadd --gid "$BOUNDVER_GID" boundver \
    && useradd --uid "$BOUNDVER_UID" --gid boundver --create-home boundver \
    && mkdir -p /repo \
    && chown boundver:boundver /repo \
    && git config --system --add safe.directory /repo \
    && find / -xdev -type f -perm /6000 -exec chmod a-s {} +

WORKDIR /repo
USER boundver
ENTRYPOINT ["boundver"]
CMD ["--help"]
