# FF Manager — образ для Kubernetes
# - процесс не от root (uid/gid 1000)
# - порт через переменную PORT (по умолчанию 8000); в probes используйте GET /health
# - корректное завершение: uvicorn как PID 1 получает SIGTERM от kubelet
#
# Пример probes:
#   livenessProbe:  httpGet: path: /health  port: http
#   readinessProbe: httpGet: path: /health  port: http
#
# БД и секреты — через env (FF_DB_*, FF_API_BASE_URL и т.д.) из Secret/ConfigMap.

FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

LABEL org.opencontainers.image.title="FF Manager" \
      org.opencontainers.image.description="Менеджер фитнес-проверок (FastAPI)"

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY --chown=1000:1000 . .

USER 1000:1000

EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port \"${PORT}\""]
