FROM node:22-bookworm-slim AS node_runtime
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bash git ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir 'pytest>=9,<10'
COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node_runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && useradd --uid 10001 --create-home sandbox \
    && mkdir -p /workspace /input /opt/sandbox \
    && chown 10001:10001 /workspace /input
COPY src/agent_backend/sandbox/helper.py src/agent_backend/sandbox/runner.py src/agent_backend/sandbox/entrypoint.py /opt/sandbox/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LANG=C.UTF-8
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["python", "/opt/sandbox/entrypoint.py"]
