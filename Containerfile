# Slim baker — python + pyarrow + pillow + requests + uv.
# Used for: devcontainer, lint, test, ambientcg/polyhaven/physicallybased baking.
# No C++ compilation, builds in seconds.
#
# Build:  podman build -t ghcr.io/morepet/mat-vis-baker:latest .
# Push:   podman push ghcr.io/morepet/mat-vis-baker:latest

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends git curl ca-certificates \
 && curl -fsSL https://github.com/cli/cli/releases/download/v2.74.1/gh_2.74.1_linux_amd64.tar.gz \
    | tar xz --strip-components=2 -C /usr/local/bin gh_2.74.1_linux_amd64/bin/gh \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ src/
RUN uv pip install --system --no-cache .[baker]

WORKDIR /workspace
