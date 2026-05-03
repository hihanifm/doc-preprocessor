# Default: AWS Public ECR mirror of Docker Official Images (many labs block Docker Hub).
# Override: PYTHON_IMAGE=docker.io/library/python:3.12-slim
ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

WORKDIR /app

# Install lab CA cert if present (corporate TLS interception; safe no-op when certs/ is empty).
COPY certs/ /tmp/certs/
RUN if ls /tmp/certs/*.crt 2>/dev/null 1>&2; then \
      cp /tmp/certs/*.crt /usr/local/share/ca-certificates/ && \
      update-ca-certificates; \
    fi && rm -rf /tmp/certs

ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

COPY requirements.txt .
COPY pip-cache/ /pip-cache/

# Install from local pip-cache first (offline/proxy-safe); falls back to PyPI for missing deps.
# --trusted-host list is defensive against corporate TLS interception (harmless otherwise).
# BuildKit cache mount: pip reuses wheels between builds without baking them into image layers.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
        --trusted-host pypi.org \
        --trusted-host www.pypi.org \
        --trusted-host pypi.python.org \
        --trusted-host pypi.io \
        --trusted-host files.pythonhosted.org \
        --find-links /pip-cache/ -r requirements.txt \
    || pip install \
        --trusted-host pypi.org \
        --trusted-host www.pypi.org \
        --trusted-host pypi.python.org \
        --trusted-host pypi.io \
        --trusted-host files.pythonhosted.org \
        -r requirements.txt

COPY . .

RUN mkdir -p /data

ENV CONFIG_PATH=/data/config.json

EXPOSE 5000
