# boundver — CI container image
#
# Build:
#   docker build -t boundver .
#
# Run (mount your repo):
#   docker run --rm -v "$(pwd):/repo" -w /repo boundver verify
#   docker run --rm -v "$(pwd):/repo" -w /repo boundver generate --source head
#
# No public container image is currently published or supported. The
# Dockerfile is exercised from the exact source commit in CI.

FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels ".[schema,yaml]"


FROM python:3.12-slim

# git is required — boundver reads HEAD/index via git subprocess calls.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ARG BOUNDVER_UID=1000
ARG BOUNDVER_GID=1000
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index \
      --find-links=/wheels /wheels/boundver-*.whl \
    && rm -rf /wheels \
    && groupadd --gid "$BOUNDVER_GID" boundver \
    && useradd --uid "$BOUNDVER_UID" --gid boundver --create-home boundver \
    && mkdir -p /repo \
    && chown boundver:boundver /repo \
    && git config --system --add safe.directory /repo

WORKDIR /repo
USER boundver
ENTRYPOINT ["boundver"]
CMD ["--help"]
