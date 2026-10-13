FROM python:3.12-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.12.17
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev && mkdir /workspace && chown 65532:65532 /workspace

USER 65532:65532
CMD [".venv/bin/python", "-m", "caf_orchestration_worker", "queue-synthetic-once", "--workspace", "/workspace"]
