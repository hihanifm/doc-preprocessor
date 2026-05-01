FROM python:3.12-slim

# Proxy build args — pass from host if lab egress is restricted
ARG HTTP_PROXY=
ARG HTTPS_PROXY=
ARG NO_PROXY=
ENV HTTP_PROXY=${HTTP_PROXY} HTTPS_PROXY=${HTTPS_PROXY} NO_PROXY=${NO_PROXY}

WORKDIR /app

COPY requirements.txt .
COPY pip-cache/ /pip-cache/

# Install from local pip-cache first (offline/proxy-safe); fall back to PyPI if empty
RUN pip install --no-cache-dir --find-links /pip-cache/ -r requirements.txt \
    || pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

ENV CONFIG_PATH=/data/config.json \
    HTTP_PROXY= HTTPS_PROXY= NO_PROXY=

EXPOSE 5000
