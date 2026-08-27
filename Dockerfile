# boundver — CI container image
#
# Build:
#   docker build -t boundver .
#
# Run (mount your repo):
#   docker run --rm -v "$(pwd):/repo" -w /repo boundver verify
#   docker run --rm -v "$(pwd):/repo" -w /repo boundver generate --source head
#
# Release images are published at ghcr.io/yzm1/boundver. Prefer an immutable
# version tag or digest; the Dockerfile is also exercised from exact source in CI.

# Keep the multi-architecture base digest and Debian snapshot together. The
# snapshot timestamp is the one recorded by this exact official Python image.
FROM python:3.12.14-slim-trixie@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS builder

# Clamp archive timestamps to the pinned snapshot's UTC timestamp so identical
# source inputs produce the same local wheel bytes.
ENV SOURCE_DATE_EPOCH=1785715200

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
      --no-deps \
      --find-links=/wheelhouse \
      setuptools==80.9.0 wheel==0.48.0
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


FROM python:3.12.14-slim-trixie@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

# pip compiles installed modules in this stage. Force hash-based bytecode so
# retries do not embed the wall clock in hundreds of .pyc headers.
ENV SOURCE_DATE_EPOCH=1785715200

ARG BOUNDVER_VERSION=development
ARG BOUNDVER_REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/yzm1/boundver" \
      org.opencontainers.image.url="https://yzm1.github.io/boundver/" \
      org.opencontainers.image.documentation="https://yzm1.github.io/boundver/" \
      org.opencontainers.image.title="boundver" \
      org.opencontainers.image.description="Contract-drift classification and downstream impact for polyglot repositories" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="$BOUNDVER_VERSION" \
      org.opencontainers.image.revision="$BOUNDVER_REVISION"

# git is required — boundver reads HEAD/index via git subprocess calls.
# Resolve it only from the immutable Debian archive snapshot recorded by the
# pinned base image, rather than today's mutable package index.
RUN export DEBIAN_FRONTEND=noninteractive \
    && grep -Fqx \
      '# http://snapshot.debian.org/archive/debian/20260803T000000Z' \
      /etc/apt/sources.list.d/debian.sources \
    && grep -Fqx \
      '# http://snapshot.debian.org/archive/debian-security/20260803T000000Z' \
      /etc/apt/sources.list.d/debian.sources \
    && sed -i \
      -e 's|http://deb.debian.org/debian-security|https://snapshot.debian.org/archive/debian-security/20260803T000000Z|' \
      -e 's|http://deb.debian.org/debian|https://snapshot.debian.org/archive/debian/20260803T000000Z|' \
      /etc/apt/sources.list.d/debian.sources \
    && printf 'Acquire::Check-Valid-Until "false";\n' \
      > /etc/apt/apt.conf.d/99snapshot \
    && apt-get update \
    && apt-get install -y --no-install-recommends git=1:2.47.3-0+deb13u1 \
    && apt-get clean \
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
    && rm -rf /wheelhouse /locks \
    && groupadd --gid "$BOUNDVER_GID" boundver \
    && useradd --uid "$BOUNDVER_UID" --gid boundver --create-home boundver \
    && mkdir -p /repo \
    && chown boundver:boundver /repo \
    && git config --system --add safe.directory /repo

WORKDIR /repo
USER boundver
ENTRYPOINT ["boundver"]
CMD ["--help"]
