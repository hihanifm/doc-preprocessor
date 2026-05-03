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
	run run-lan pip-cache build-dev doctor daemon-proxy daemon-proxy-clear

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
	@echo "    make daemon-proxy        Write /etc/systemd/.../http-proxy.conf from \$$HTTP_PROXY (sudo)"
	@echo "    make daemon-proxy-clear  Remove daemon proxy drop-in and restart Docker (sudo)"
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
	@echo "── 2. Docker install type + config source ──"
	@if command -v snap >/dev/null 2>&1 && snap list docker >/dev/null 2>&1; then \
	  echo "  Install: snap (config via 'snap set docker http-proxy=...')"; \
	  for k in http-proxy https-proxy no-proxy; do \
	    v=$$(sudo snap get docker $$k 2>/dev/null || echo ""); \
	    if [ -n "$$v" ]; then \
	      echo "    $$k = $$(echo "$$v" | sed -E 's#(://[^:]+:)[^@]+(@)#\1<redacted>\2#g')"; \
	    else \
	      echo "    $$k = (unset)"; \
	    fi; \
	  done; \
	elif command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files docker.service >/dev/null 2>&1; then \
	  echo "  Install: native systemd ('docker.service')"; \
	  conf=/etc/systemd/system/docker.service.d/http-proxy.conf; \
	  if [ -r $$conf ]; then \
	    echo "  Drop-in $$conf:"; \
	    sed -E 's#(://[^:]+:)[^@]+(@)#\1<redacted>\2#g' $$conf | sed 's/^/    /'; \
	  else \
	    echo "  Drop-in $$conf: (not present — run 'make daemon-proxy' to create)"; \
	  fi; \
	  echo "  systemctl Environment:"; \
	  systemctl show --property=Environment docker 2>/dev/null | tr ' ' '\n' \
	    | sed -E 's#(://[^:]+:)[^@]+(@)#\1<redacted>\2#g' \
	    | grep -iE '^(HTTP|HTTPS|NO|ALL|FTP)' | sed 's/^/    /' \
	    || echo "    (no proxy in Environment — did you 'systemctl daemon-reload' + restart?)"; \
	else \
	  echo "  Install: unknown (not snap, no docker.service unit). Could be Docker Desktop,"; \
	  echo "    rootless, or a remote context. Try: docker context ls"; \
	fi
	@echo ""
	@echo "── 3. Shell proxy (used by compose substitution into build args + runtime) ──"
	@out=$$(env | grep -iE '^(http|https|no|all|ftp)_proxy=' || true); \
	 if [ -z "$$out" ]; then echo "  (no *_PROXY set in shell)"; \
	 else echo "$$out" | sed -E 's#(://[^:]+:)[^@]+(@)#\1<redacted>\2#g'; fi
	@echo ""
	@echo "── 4. Curl proxy auth test (host → proxy → public.ecr.aws) ──"
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
	@echo "── 5. Pull test (base image — what 'make dev' actually needs) ──"
	@docker pull $${PYTHON_IMAGE:-public.ecr.aws/docker/library/python:3.12-slim}
	@echo ""
	@echo "── 6. Pull test (DH BuildKit frontend; only relevant if a Dockerfile re-adds '# syntax=...') ──"
	@docker pull docker.io/docker/dockerfile:1 || echo "  (failure here is harmless — this repo's Dockerfile no longer needs it)"
	@echo ""
	@echo "── URL-encode special chars in proxy passwords ──"
	@echo "  ! = %21   @ = %40   : = %3A   # = %23   / = %2F   ? = %3F   space = %20"
	@echo ""
	@echo "If layer 5 (or 1) shows no proxy + pull 401s: run 'make daemon-proxy' to set the"
	@echo "daemon proxy from your shell HTTP_PROXY ('make daemon-proxy-clear' to undo). The"
	@echo "make target auto-detects snap vs systemd Docker."
	@echo "If layer 4 fails but layer 5 works: shell creds are wrong (encoding?). Compose builds"
	@echo "will still pull but 'pip install' inside the build may fail."

# ── Daemon proxy: configure dockerd to use shell HTTP_PROXY ─────────────────
# Required for Linux corporate-proxy users — the Docker daemon does NOT inherit
# proxy from the user shell. Without this, 'docker pull' goes through any
# transparent intercepting proxy unauthenticated and 401s.
#
# Auto-detects install type and dispatches:
#   - snap docker  -> 'sudo snap set docker http-proxy=...' (snap auto-restarts dockerd)
#   - native systemd -> writes /etc/systemd/system/docker.service.d/http-proxy.conf
#                       (mode 0600 because it contains the cleartext password,
#                       URL-encode special chars: ! -> %21, @ -> %40, etc.)
# Idempotent: skips write/restart if the existing config already matches.
daemon-proxy:
	@if [ -z "$$HTTP_PROXY$$http_proxy$$HTTPS_PROXY$$https_proxy" ]; then \
	  echo "Error: HTTP_PROXY (or http_proxy / HTTPS_PROXY / https_proxy) is not set."; \
	  echo "Export it in this shell, then re-run 'make daemon-proxy'. Example:"; \
	  echo "  export HTTP_PROXY=\"http://USER:PASSWORD@proxy-host:4433\""; \
	  echo "  export HTTPS_PROXY=\"\$$HTTP_PROXY\""; \
	  exit 1; \
	fi
	@H="$${HTTP_PROXY:-$$http_proxy}"; \
	 S="$${HTTPS_PROXY:-$${https_proxy:-$$H}}"; \
	 N="$${NO_PROXY:-$${no_proxy:-localhost,127.0.0.1,::1,host.docker.internal}}"; \
	 redact() { echo "$$1" | sed -E 's#(://[^:]+:)[^@]+(@)#\1<redacted>\2#g'; }; \
	 echo "Proposed daemon proxy:"; \
	 echo "  HTTP_PROXY=$$(redact "$$H")"; \
	 echo "  HTTPS_PROXY=$$(redact "$$S")"; \
	 echo "  NO_PROXY=$$N"; \
	 echo ""; \
	 if command -v snap >/dev/null 2>&1 && snap list docker >/dev/null 2>&1; then \
	   echo "Detected: snap-installed Docker. Using 'sudo snap set docker ...' (snap auto-restarts)."; \
	   curr_h=$$(sudo snap get docker http-proxy 2>/dev/null || true); \
	   curr_s=$$(sudo snap get docker https-proxy 2>/dev/null || true); \
	   curr_n=$$(sudo snap get docker no-proxy 2>/dev/null || true); \
	   if [ "$$curr_h" = "$$H" ] && [ "$$curr_s" = "$$S" ] && [ "$$curr_n" = "$$N" ]; then \
	     echo "Snap docker proxy already up to date — no change."; \
	     echo "Run 'make doctor' to verify."; \
	     exit 0; \
	   fi; \
	   echo "Setting snap docker proxy (sudo will prompt; snap will auto-restart dockerd)..."; \
	   sudo snap set docker http-proxy="$$H" https-proxy="$$S" no-proxy="$$N"; \
	   sleep 2; \
	   echo ""; \
	   echo "Loaded by daemon:"; \
	   info=$$(docker info 2>/dev/null | grep -iE 'proxy' || true); \
	   if [ -z "$$info" ]; then \
	     echo "  (no proxy in 'docker info' yet — snap restart may still be in progress; rerun 'make doctor' in a few seconds)"; \
	   else \
	     echo "$$info" | sed -E 's#(://[^:[:space:]]+:)[^@[:space:]]+(@)#\1<redacted>\2#g'; \
	   fi; \
	   echo ""; \
	   echo "Next: 'make doctor' to confirm a base-image pull works."; \
	   exit 0; \
	 fi; \
	 if ! command -v systemctl >/dev/null 2>&1; then \
	   echo "Error: not snap-installed and no systemctl available."; \
	   echo "Manual setup depends on your install: Docker Desktop -> Settings -> Resources -> Proxies."; \
	   exit 1; \
	 fi; \
	 if ! systemctl list-unit-files docker.service >/dev/null 2>&1; then \
	   echo "Error: 'docker.service' systemd unit not found and Docker is not snap-installed."; \
	   echo "Your Docker may be rootless, Docker Desktop, or pointing at a remote context. Run:"; \
	   echo "  systemctl list-unit-files | grep -iE 'docker|moby|containerd'"; \
	   echo "  systemctl --user list-unit-files | grep -iE 'docker'"; \
	   echo "  docker context ls"; \
	   exit 1; \
	 fi; \
	 echo "Detected: native systemd Docker. Using /etc/systemd/system/docker.service.d/http-proxy.conf."; \
	 tmp=$$(mktemp); trap 'rm -f $$tmp' EXIT; \
	 printf '[Service]\nEnvironment="HTTP_PROXY=%s"\nEnvironment="HTTPS_PROXY=%s"\nEnvironment="NO_PROXY=%s"\n' "$$H" "$$S" "$$N" > $$tmp; \
	 conf=/etc/systemd/system/docker.service.d/http-proxy.conf; \
	 if [ -f $$conf ] && sudo cmp -s $$tmp $$conf 2>/dev/null; then \
	   echo "Daemon proxy already up to date — no restart needed."; \
	   echo "Run 'make doctor' to verify."; \
	   exit 0; \
	 fi; \
	 echo "Writing $$conf (sudo will prompt)..."; \
	 sudo mkdir -p /etc/systemd/system/docker.service.d; \
	 sudo install -m 0600 -o root -g root $$tmp $$conf; \
	 echo "Reloading systemd + restarting Docker daemon (running containers will be stopped)..."; \
	 sudo systemctl daemon-reload; \
	 sudo systemctl restart docker; \
	 echo ""; \
	 echo "Loaded by daemon:"; \
	 info=$$(docker info 2>/dev/null | grep -iE 'proxy' || true); \
	 if [ -z "$$info" ]; then \
	   echo "  (no proxy in 'docker info' — check 'sudo systemctl status docker')"; \
	 else \
	   echo "$$info" | sed -E 's#(://[^:[:space:]]+:)[^@[:space:]]+(@)#\1<redacted>\2#g'; \
	 fi; \
	 echo ""; \
	 echo "Next: 'make doctor' to confirm a base-image pull works."

# Reverse of daemon-proxy. Cleans both snap config and any orphan systemd
# drop-in (in case daemon-proxy was run earlier on a host that turned out to
# be snap-installed, leaving a harmless but tidier-removed file behind).
daemon-proxy-clear:
	@cleared=0; \
	 if command -v snap >/dev/null 2>&1 && snap list docker >/dev/null 2>&1; then \
	   echo "Unsetting snap docker proxy keys (sudo will prompt)..."; \
	   sudo snap unset docker http-proxy https-proxy no-proxy 2>/dev/null || true; \
	   cleared=1; \
	 fi; \
	 conf=/etc/systemd/system/docker.service.d/http-proxy.conf; \
	 if [ -f $$conf ]; then \
	   echo "Removing $$conf and restarting Docker..."; \
	   sudo rm -f $$conf; \
	   sudo rmdir /etc/systemd/system/docker.service.d 2>/dev/null || true; \
	   if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files docker.service >/dev/null 2>&1; then \
	     sudo systemctl daemon-reload; \
	     sudo systemctl restart docker; \
	   fi; \
	   cleared=1; \
	 fi; \
	 if [ "$$cleared" = "0" ]; then \
	   echo "Nothing to clear (no snap docker config and no systemd drop-in)."; \
	   exit 0; \
	 fi; \
	 sleep 2; \
	 echo ""; \
	 info=$$(docker info 2>/dev/null | grep -iE 'proxy' || true); \
	 if [ -z "$$info" ]; then \
	   echo "  (no proxy in 'docker info' — daemon is back to direct egress)"; \
	 else \
	   echo "Still loaded:"; echo "$$info" | sed -E 's#(://[^:[:space:]]+:)[^@[:space:]]+(@)#\1<redacted>\2#g'; \
	 fi

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
