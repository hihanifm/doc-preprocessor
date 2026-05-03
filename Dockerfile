# Default: AWS Public ECR mirror of Docker Official Images (many labs block Docker Hub).
# Override: PYTHON_IMAGE=docker.io/library/python:3.12-slim
ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

# Proxy build args — pass from docker-compose / host if lab egress is restricted
ARG HTTP_PROXY=
ARG HTTPS_PROXY=
ARG NO_PROXY=
ENV HTTP_PROXY=${HTTP_PROXY} HTTPS_PROXY=${HTTPS_PROXY} NO_PROXY=${NO_PROXY}

WORKDIR /app

# Install lab CA cert if present (MITM / corporate proxy roots; safe no-op when certs/ is empty).
COPY certs/ /tmp/certs/
RUN if ls /tmp/certs/*.crt 2>/dev/null 1>&2; then \
      cp /tmp/certs/*.crt /usr/local/share/ca-certificates/ && \
      update-ca-certificates; \
    fi && rm -rf /tmp/certs

ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

COPY requirements.txt .
COPY pip-cache/ /pip-cache/

# Install from local pip-cache first (offline/proxy-safe); both paths may still hit PyPI for deps.
# Broad --trusted-host list for lab MITM / proxy SSL issues (same index + redirects + file CDN).
RUN pip install --no-cache-dir \
        --trusted-host pypi.org \
        --trusted-host www.pypi.org \
        --trusted-host pypi.python.org \
        --trusted-host pypi.io \
        --trusted-host files.pythonhosted.org \
        --find-links /pip-cache/ -r requirements.txt \
    || pip install --no-cache-dir \
        --trusted-host pypi.org \
        --trusted-host www.pypi.org \
        --trusted-host pypi.python.org \
        --trusted-host pypi.io \
        --trusted-host files.pythonhosted.org \
        -r requirements.txt

COPY . .

RUN mkdir -p /data

# Drop build-time proxy from image layers; runtime uses compose `environment` (x-proxy-env) when set.
ENV CONFIG_PATH=/data/config.json \
    HTTP_PROXY= HTTPS_PROXY= NO_PROXY=

EXPOSE 5000
