# Baker image — pre-installs materialx + pyarrow so CI doesn't rebuild every run.
# Build:  podman build -t ghcr.io/morepet/mat-vis-baker:latest .
# Push:   podman push ghcr.io/morepet/mat-vis-baker:latest
# Use in CI: container: ghcr.io/morepet/mat-vis-baker:latest

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir .[baker,materialx]

WORKDIR /workspace
