FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build && npm run check

FROM python:3.13.2-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN groupadd --system --gid 10001 waygent && useradd --system --uid 10001 --gid waygent --create-home --home-dir /app waygent && mkdir -p /app/data && chown -R waygent:waygent /app
COPY requirements.runtime.lock.txt requirements.checkpoint-gate.constraints.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip==25.3 && python -m pip install --no-cache-dir -r requirements.runtime.lock.txt
COPY --chown=waygent:waygent app ./app
COPY --chown=waygent:waygent migrations_mysql ./migrations_mysql
COPY --chown=waygent:waygent frontend ./frontend
COPY --chown=waygent:waygent --from=frontend /build/generated ./frontend/generated
COPY --chown=waygent:waygent --from=frontend /build/index.html ./frontend/index.html
COPY --chown=waygent:waygent scripts ./scripts
USER 10001:10001
EXPOSE 8765
CMD ["python", "scripts/start.py"]
