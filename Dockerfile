FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps . && useradd --uid 10001 --create-home appuser
COPY aegra.json ./
USER appuser
EXPOSE 2026
CMD ["uvicorn", "agent_backend.server:app", "--host", "0.0.0.0", "--port", "2026", "--workers", "1", "--timeout-graceful-shutdown", "45"]
