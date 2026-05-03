.DEFAULT_GOAL := help

# Proxy: auto-detect. If the shell has any HTTP(S)_PROXY set, pass it through to compose
# (build args + runtime env). Otherwise leave compose env untouched. Set NO_DOCKER_PROXY=1
# to force-clear (rare: only useful when daemon proxy is mis-auth'd AND the registry is
# reachable directly). Full story: refs/docker-lab-guidelines.md
ifeq ($(NO_DOCKER_PROXY),1)
DOCKER_COMPOSE_ENV := HTTP_PROXY= HTTPS_PROXY= http_proxy= https_proxy= ALL_PROXY= FTP_PROXY=
else
DOCKER_COMPOSE_ENV :=
endif

COMPOSE := $(DOCKER_COMPOSE_ENV) docker compose

# Single Flask service; ports from docker-compose.yml (prod :5000, dev host :35050).
.PHONY: help up down down-all build rebuild restart logs logs-dev ps dev e2e-up \
	prod prod-down down-prod build-prod logs-prod \
	run run-lan pip-cache build-dev doctor

help:
	@echo ""
	@echo "Docker — refs/docker-lab-guidelines.md"
	@echo ""
	@echo "  Daily dev"
	@echo "    make build && make up   Build image, start dev → http://<host>:35050"
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
	@echo "  Diagnostics"
	@echo "    make doctor             Show daemon proxy + smoke-test base image pull"
	@echo ""
	@echo "  Proxy (Linux)"
	@echo "    Default: shell HTTP(S)_PROXY is passed through to compose. Set NO_DOCKER_PROXY=1"
	@echo "    to force-clear. Daemon-level proxy (systemd) governs 'docker pull'; see"
	@echo "    refs/docker-lab-guidelines.md for the three-layer setup."
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

# ── Diagnostics: confirm daemon proxy + smoke-test image pulls ──────────────
# Run before `make dev` when builds fail with proxy/registry errors. Walks the
# three proxy layers (shell / build args / daemon — see refs/docker-lab-guidelines.md)
# and runs an isolated curl + docker pull so you know which layer is broken.
# Passwords are redacted in printed URLs (USER:PASS@ → USER:<redacted>@).
doctor:
	@echo "── 1. Daemon runtime proxy (from 'docker info') ──"
	@out=$$(docker info 2>/dev/null | grep -iE 'proxy|registry' || true); \
	 if [ -z "$$out" ]; then echo "  (no proxy fields in 'docker info' — daemon is not configured for proxy)"; \
	 else echo "$$out" | sed -E 's#(://[^:[:space:]]+:)[^@[:space:]]+(@)#\1<redacted>\2#g'; fi
	@echo ""
	@echo "── 2. systemd drop-in: /etc/systemd/system/docker.service.d/http-proxy.conf ──"
	@if [ -r /etc/systemd/system/docker.service.d/http-proxy.conf ]; then \
	  sed -E 's#(://[^:]+:)[^@]+(@)#\1<redacted>\2#g' /etc/systemd/system/docker.service.d/http-proxy.conf; \
	else \
	  echo "  (file not found or unreadable — daemon proxy not set this way, or run with sudo to read)"; \
	fi
	@echo ""
	@echo "── 3. systemctl Environment for docker.service (what systemd actually loaded) ──"
	@if command -v systemctl >/dev/null 2>&1; then \
	  systemctl show --property=Environment docker 2>/dev/null | tr ' ' '\n' \
	    | sed -E 's#(://[^:]+:)[^@]+(@)#\1<redacted>\2#g' \
	    | grep -iE '^(Environment|HTTP|HTTPS|NO|ALL|FTP)' || echo "  (no proxy in systemd Environment — daemon-reload + restart needed after editing the drop-in?)"; \
	else \
	  echo "  (systemctl not present — non-systemd host)"; \
	fi
	@echo ""
	@echo "── 4. Shell proxy (used by compose substitution into build args + runtime) ──"
	@out=$$(env | grep -iE '^(http|https|no|all|ftp)_proxy=' || true); \
	 if [ -z "$$out" ]; then echo "  (no *_PROXY set in shell)"; \
	 else echo "$$out" | sed -E 's#(://[^:]+:)[^@]+(@)#\1<redacted>\2#g'; fi
	@echo ""
	@echo "── 5. Curl proxy auth test (host → proxy → public.ecr.aws) ──"
	@if [ -n "$${HTTPS_PROXY}$${https_proxy}$${HTTP_PROXY}$${http_proxy}" ] && command -v curl >/dev/null 2>&1; then \
	  proxy="$${HTTPS_PROXY:-$${https_proxy:-$${HTTP_PROXY:-$$http_proxy}}}"; \
	  echo "  using proxy: $$(echo $$proxy | sed -E 's#(://[^:]+:)[^@]+(@)#\1<redacted>\2#g')"; \
	  curl --max-time 10 -sS -o /dev/null -w "  HTTP %{http_code}  total=%{time_total}s\n" \
	    -x "$$proxy" https://public.ecr.aws/v2/ \
	    || echo "  (curl through proxy failed — bad creds, wrong host:port, or proxy unreachable)"; \
	else \
	  echo "  (skipped: no shell proxy set, or curl not installed)"; \
	fi
	@echo ""
	@echo "── 6. Pull test (base image — what 'make dev' actually needs) ──"
	@docker pull $${PYTHON_IMAGE:-public.ecr.aws/docker/library/python:3.12-slim}
	@echo ""
	@echo "── 7. Pull test (DH BuildKit frontend; only relevant if a Dockerfile re-adds '# syntax=...') ──"
	@docker pull docker.io/docker/dockerfile:1 || echo "  (failure here is harmless — this repo's Dockerfile no longer needs it)"
	@echo ""
	@echo "── URL-encode special chars in proxy passwords ──"
	@echo "  ! = %21   @ = %40   : = %3A   # = %23   / = %2F   ? = %3F   space = %20"
	@echo ""
	@echo "If layer 6 fails with 401: fix /etc/systemd/system/docker.service.d/http-proxy.conf,"
	@echo "then 'sudo systemctl daemon-reload && sudo systemctl restart docker'."
	@echo "If layer 5 fails but layer 6 works: shell creds are wrong (encoding?). Compose builds"
	@echo "will still pull but 'pip install' inside the build may fail."

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
