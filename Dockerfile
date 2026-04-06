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

ARG RUN_IMAGE=ubuntu:22.04
FROM ${RUN_IMAGE}
# FROM python:3.12-slim-bookworm

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates python3 pip python3-dev libpq5 && \
    rm -rf /var/cache/apt/archives /var/lib/apt/lists/* && \
    groupadd --gid 1000 app && \
    useradd --uid 1000 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --chmod=644 certs/* /usr/local/share/ca-certificates/
RUN update-ca-certificates

ARG PIP_INDEX_URL=''
ENV PIP_INDEX_URL=$PIP_INDEX_URL
ARG PIP_TRUSTED_HOST=''
ENV PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

LABEL org.opencontainers.image.title="FF Manager" \
      org.opencontainers.image.description="Менеджер фитнес-проверок (FastAPI)"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && rm -rf /root/.cache/pip

COPY --chown=1000:1000 . .

USER 1000:1000

EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port \"${PORT}\""]
