# Slim baker — python + pyarrow + pillow + requests + uv.
# Used for: devcontainer, lint, test, ambientcg/polyhaven/physicallybased baking.
# No C++ compilation, builds in seconds.
#
# Build:  podman build -t ghcr.io/morepet/mat-vis-baker:latest .
# Push:   podman push ghcr.io/morepet/mat-vis-baker:latest

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

COPY pyproject.toml README.md ./
COPY src/ src/
RUN uv pip install --system --no-cache .[baker]

WORKDIR /workspace
