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
COPY pip-cache/ /tmp/pip-cache/

# If wheels were pre-downloaded (make pip-cache), install fully offline — no network needed.
# Otherwise fall back to PyPI with trusted-host flags for lab proxy environments.
# BuildKit cache mount: pip reuses wheels between builds without baking them into image layers.
RUN --mount=type=cache,target=/root/.cache/pip \
    if ls /tmp/pip-cache/*.whl /tmp/pip-cache/*.tar.gz 2>/dev/null | grep -q .; then \
      pip install --no-index --find-links /tmp/pip-cache/ -r requirements.txt; \
    else \
      pip install \
          --trusted-host pypi.org \
          --trusted-host www.pypi.org \
          --trusted-host pypi.python.org \
          --trusted-host pypi.io \
          --trusted-host files.pythonhosted.org \
          -r requirements.txt; \
    fi

COPY . .

RUN mkdir -p /data

EXPOSE 5000
