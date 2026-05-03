# boundver — CI container image
#
# Build:
#   docker build -t boundver .
#
# Run (mount your repo):
#   docker run --rm -v "$(pwd):/repo" -w /repo boundver verify
#   docker run --rm -v "$(pwd):/repo" -w /repo boundver generate --source head
#
# Multi-platform build + push to GHCR (from CI):
#   docker buildx build \
#     --platform linux/amd64,linux/arm64 \
#     -t ghcr.io/yzm1/boundver:latest \
#     --push .

FROM python:3.12-slim

# git is required — boundver reads HEAD/index via git subprocess calls.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy source and install (no external deps).
WORKDIR /build
COPY . .
RUN pip install --no-cache-dir --no-deps . \
    && rm -rf /build

# Mark /repo as safe for git even when mounted with a different UID.
RUN git config --global --add safe.directory /repo

WORKDIR /repo
ENTRYPOINT ["boundver"]
CMD ["--help"]
