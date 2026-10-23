FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
# Reuse the existing pinned, hashed dependency layer, including FastAPI/httpx/uvicorn.
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock
ENV PYTHONPATH=/app/src
COPY src/agent_backend/sandbox ./src/agent_backend/sandbox
EXPOSE 8090
# This trusted control-plane service owns Docker access. Task containers never do.
CMD ["uvicorn", "agent_backend.sandbox.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
