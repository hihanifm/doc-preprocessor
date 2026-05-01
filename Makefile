.DEFAULT_GOAL := help

# Proxy: host HTTP(S)_PROXY often breaks Docker pulls/builds on Linux; clear for compose by default.
# Full story: refs/docker-lab-guidelines.md
#   make dev USE_SYSTEM_PROXY=1   — pass host proxy into compose build/up (restricted egress labs)
ifeq ($(USE_SYSTEM_PROXY),1)
DOCKER_COMPOSE_ENV :=
else
DOCKER_COMPOSE_ENV := HTTP_PROXY= HTTPS_PROXY= http_proxy= https_proxy= ALL_PROXY= FTP_PROXY=
endif

COMPOSE := $(DOCKER_COMPOSE_ENV) docker compose

# Single Flask service; ports from docker-compose.yml (BASE_PORT=5000 → prod :5000, dev :5001).
.PHONY: help up down down-all build rebuild restart logs logs-dev ps dev e2e-up \
	prod prod-down down-prod build-prod logs-prod \
	run run-lan pip-cache build-dev

help:
	@echo ""
	@echo "Docker — refs/docker-lab-guidelines.md"
	@echo ""
	@echo "  Daily dev"
	@echo "    make build && make up   Build image, start dev → http://<host>:5001"
	@echo "    make dev                Same intent as CI: up -d --build"
	@echo ""
	@echo "  After Dockerfile / requirements.txt changes"
	@echo "    make rebuild            dev: build --no-cache, then up"
	@echo ""
	@echo "  Logs / status"
	@echo "    make logs               Follow app-dev logs"
	@echo "    make logs-prod          Follow app-prod logs"
	@echo "    make ps                 docker compose ps"
	@echo ""
	@echo "  Stop"
	@echo "    make down               Dev stack only"
	@echo "    make prod-down          Prod only   (alias: down-prod)"
	@echo "    make down-all           Dev + prod"
	@echo ""
	@echo "  Production"
	@echo "    make prod               Build + start prod → http://<host>:5000"
	@echo ""
	@echo "  Mac (no Docker)"
	@echo "    make run / make run-lan"
	@echo ""
	@echo "  Lab / offline builds"
	@echo "    make pip-cache          Linux wheels → pip-cache/"
	@echo ""
	@echo "  Proxy (Linux)"
	@echo "    Default: proxy env cleared for compose. USE_SYSTEM_PROXY=1 keeps host proxy."
	@echo ""

# ── Dev (profile dev, service app-dev) ─────────────────────────────────────────
up:
	$(COMPOSE) --profile dev up -d

down:
	$(COMPOSE) --profile dev down --remove-orphans

build:
	$(COMPOSE) --profile dev build

# Does not pick up source edits unless image layers changed — dev bind-mounts .:/app for Python reload.
rebuild:
	$(COMPOSE) --profile dev build --no-cache
	$(COMPOSE) --profile dev up -d

# No rebuild — won’t fix stale images; use rebuild after Dockerfile/deps edits.
restart:
	$(COMPOSE) --profile dev down --remove-orphans
	$(COMPOSE) --profile dev up -d

logs:
	$(COMPOSE) --profile dev logs -f app-dev

logs-dev: logs

ps:
	$(COMPOSE) ps

e2e-up:
	$(COMPOSE) --profile dev up -d --build

dev: e2e-up

build-dev: build

# ── Production (profile prod, service app-prod) ────────────────────────────────
prod:
	$(COMPOSE) --profile prod build
	$(COMPOSE) --profile prod up -d

build-prod: prod

prod-down:
	$(COMPOSE) --profile prod down --remove-orphans

down-prod: prod-down

logs-prod:
	$(COMPOSE) --profile prod logs -f app-prod

# ── Stop all profiles ──────────────────────────────────────────────────────────
down-all:
	$(COMPOSE) --profile dev --profile prod down --remove-orphans

# ── Mac local (no Docker) ────────────────────────────────────────────────────
run:
	@test -f .venv/bin/activate || python3 -m venv .venv
	@. .venv/bin/activate && pip install -q -r requirements.txt
	@. .venv/bin/activate && python app.py --host 127.0.0.1

run-lan:
	@test -f .venv/bin/activate || python3 -m venv .venv
	@. .venv/bin/activate && pip install -q -r requirements.txt
	@. .venv/bin/activate && python app.py --host 0.0.0.0

# ── pip-cache (offline-friendly Docker builds) ───────────────────────────────
pip-cache:
	pip download \
	  --platform manylinux2014_x86_64 \
	  --python-version 3.12 \
	  --implementation cp \
	  --abi cp312 \
	  --only-binary=:all: \
	  -r requirements.txt \
	  -d pip-cache/
