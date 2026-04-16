# Baker image — pre-installs materialx + pyarrow so CI doesn't rebuild every run.
# Build:  podman build -t ghcr.io/morepet/mat-vis-baker:latest .
# Push:   podman push ghcr.io/morepet/mat-vis-baker:latest
# Use in CI: container: ghcr.io/morepet/mat-vis-baker:latest

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

COPY pyproject.toml .
COPY src/ src/
RUN uv pip install --system --no-cache .[baker,materialx]

WORKDIR /workspace
