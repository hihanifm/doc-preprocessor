.DEFAULT_GOAL := help

COMPOSE := docker compose

# Single Flask service; ports from docker-compose.yml (prod :5000, dev host :35050).
.PHONY: help up down down-all build rebuild restart logs logs-dev ps dev e2e-up \
	prod prod-down down-prod build-prod logs-prod \
	run run-lan pip-cache build-dev doctor proxy-keepalive

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
	@echo "    make doctor             Smoke-test docker pull + (if shell proxy set) curl probe"
	@echo ""
	@echo "  Corporate proxy (optional)"
	@echo "    Project assumes transparent proxy + IP-cache auth (e.g. Firefox login dance)."
	@echo "    No project-side proxy config is needed in that case. See refs/docker-lab-guidelines.md."
	@echo "    make proxy-keepalive    Periodic curl through \$$HTTP_PROXY to keep IP cache warm"
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

# ── Diagnostics: is the network ready for 'make dev'? ────────────────────────
# Smoke-tests the actual workload: a 'docker pull' of the base image. If your
# corporate proxy uses the IP-cache pattern (Firefox login → IP allowlisted),
# this is the fastest way to confirm whether your auth is currently fresh.
# When shell HTTP_PROXY is set, also runs a curl probe so you can distinguish
# proxy-auth issues from registry/DNS issues. Passwords in any printed proxy
# URLs are redacted.
doctor:
	@echo "── Docker version / context ──"
	@docker version --format '  Client: {{.Client.Version}}  Server: {{.Server.Version}} ({{.Server.Os}}/{{.Server.Arch}})' 2>/dev/null || echo "  (docker daemon not reachable)"
	@docker context ls 2>/dev/null | sed 's/^/  /' || true
	@echo ""
	@echo "── Shell proxy (loaded by compose if .env sets it) ──"
	@out=$$(env | grep -iE '^(http|https|no|all|ftp)_proxy=' || true); \
	 if [ -z "$$out" ]; then echo "  (no *_PROXY set in shell — assumes transparent proxy or no proxy at all)"; \
	 else echo "$$out" | sed -E 's#(://[^:]+:)[^@]+(@)#\1<redacted>\2#g' | sed 's/^/  /'; fi
	@echo ""
	@echo "── Curl probe (only runs if shell proxy is set) ──"
	@if [ -n "$${HTTPS_PROXY}$${https_proxy}$${HTTP_PROXY}$${http_proxy}" ] && command -v curl >/dev/null 2>&1; then \
	  proxy="$${HTTPS_PROXY:-$${https_proxy:-$${HTTP_PROXY:-$$http_proxy}}}"; \
	  echo "  using proxy: $$(echo "$$proxy" | sed -E 's#(://[^:]+:)[^@]+(@)#\1<redacted>\2#g')"; \
	  curl --max-time 10 -sS -o /dev/null -w "  HTTP %{http_code}  total=%{time_total}s\n" \
	    -x "$$proxy" https://public.ecr.aws/v2/ \
	    || echo "  (curl through proxy failed — bad creds, wrong host:port, or proxy unreachable)"; \
	else \
	  echo "  (skipped: no shell proxy set, or curl not installed)"; \
	fi
	@echo ""
	@echo "── Pull test (base image — what 'make dev' actually needs) ──"
	@docker pull $${PYTHON_IMAGE:-public.ecr.aws/docker/library/python:3.12-slim}
	@echo ""
	@echo "If the pull failed with 'unauthorized' / 'authentication_failed' / HTML auth page:"
	@echo "  → your corporate proxy IP cache has expired. Open Firefox, do the proxy login"
	@echo "    dance, then retry. Or run 'make proxy-keepalive' in another terminal to keep"
	@echo "    the cache warm. See refs/docker-lab-guidelines.md for sysadmin alternatives."

# ── Proxy keepalive: refresh corporate-proxy IP-cache auth ──────────────────
# Some corporate proxies (Bluecoat / Squid with `auth_param ... session`)
# authenticate by source IP after the first successful login (typically via a
# Firefox auth dialog) and expire the cache after a TTL. This target replays
# the auth via curl on a timer so the cache never expires while you work.
#
# Run in a SEPARATE terminal and leave it running:
#   export HTTP_PROXY="http://USER:PASSWORD@proxy:port"   # URL-encode special chars
#   make proxy-keepalive
#
# Tunables:
#   PROXY_REFRESH_SEC=300   default 5 min — reduce if your cache TTL is shorter
#   PROXY_PROBE_URL=...     default https://www.google.com/generate_204 (small, fast)
proxy-keepalive:
	@if [ -z "$$HTTP_PROXY$$http_proxy$$HTTPS_PROXY$$https_proxy" ]; then \
	  echo "Error: HTTP_PROXY (or http_proxy / HTTPS_PROXY / https_proxy) is not set."; \
	  echo "Export it in this shell first, then re-run."; \
	  exit 1; \
	fi
	@if ! command -v curl >/dev/null 2>&1; then \
	  echo "Error: curl is not installed (sudo apt install curl)."; exit 1; \
	fi
	@proxy="$${HTTPS_PROXY:-$${https_proxy:-$${HTTP_PROXY:-$$http_proxy}}}"; \
	 interval=$${PROXY_REFRESH_SEC:-300}; \
	 probe=$${PROXY_PROBE_URL:-https://www.google.com/generate_204}; \
	 redacted=$$(echo "$$proxy" | sed -E 's#(://[^:]+:)[^@]+(@)#\1<redacted>\2#g'); \
	 echo "Proxy keepalive starting."; \
	 echo "  proxy:    $$redacted"; \
	 echo "  probe:    $$probe"; \
	 echo "  interval: $$interval s   (Ctrl-C to stop)"; \
	 echo ""; \
	 while true; do \
	   code=$$(curl --max-time 10 -sS -o /dev/null -w "%{http_code}" -x "$$proxy" "$$probe" 2>/dev/null || echo "000"); \
	   ts=$$(date +%H:%M:%S); \
	   case "$$code" in \
	     2*|301|302|404) echo "[$$ts] OK ($$code) — proxy auth alive";; \
	     407|401)        echo "[$$ts] AUTH FAILED ($$code) — Basic creds rejected; proxy may need NTLM/Negotiate";; \
	     000)            echo "[$$ts] NETWORK FAIL — proxy unreachable, VPN dropped?";; \
	     *)              echo "[$$ts] UNEXPECTED ($$code) — continuing";; \
	   esac; \
	   sleep $$interval; \
	 done

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
