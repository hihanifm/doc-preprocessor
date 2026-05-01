.DEFAULT_GOAL := help

help:
	@echo ""
	@echo "  Docker (Linux server)"
	@echo "    make prod        Build + start production container  → http://<server-ip>:5000"
	@echo "    make dev         Build + start dev container         → http://<server-ip>:5001"
	@echo "    make down-prod   Stop production"
	@echo "    make down-dev    Stop dev"
	@echo "    make down        Stop everything"
	@echo "    make logs-prod   Tail production logs"
	@echo "    make logs-dev    Tail dev logs"
	@echo ""
	@echo "  Mac (no Docker)"
	@echo "    make run         Start Flask locally                 → http://localhost:5000"
	@echo "    make run-lan     Start Flask on LAN                  → http://<mac-ip>:5000"
	@echo ""
	@echo "  Lab / offline builds"
	@echo "    make pip-cache   Pre-download Linux wheels into pip-cache/ (run before taking to lab)"
	@echo ""

# ── Production ────────────────────────────────────────────────────────────────
prod:
	docker compose --profile prod build
	docker compose --profile prod up -d

build-prod: prod

down-prod:
	docker compose --profile prod down --remove-orphans

logs-prod:
	docker compose --profile prod logs -f app-prod

# ── Development ───────────────────────────────────────────────────────────────
dev:
	docker compose --profile dev build
	docker compose --profile dev up -d

build-dev: dev

down-dev:
	docker compose --profile dev down --remove-orphans

logs-dev:
	docker compose --profile dev logs -f app-dev

# ── Both ──────────────────────────────────────────────────────────────────────
down:
	docker compose --profile dev --profile prod down --remove-orphans

# ── Mac local dev (no Docker) ─────────────────────────────────────────────────
run:
	@test -f .venv/bin/activate || python3 -m venv .venv
	@. .venv/bin/activate && pip install -q -r requirements.txt
	@. .venv/bin/activate && python app.py --host 127.0.0.1

run-lan:
	@test -f .venv/bin/activate || python3 -m venv .venv
	@. .venv/bin/activate && pip install -q -r requirements.txt
	@. .venv/bin/activate && python app.py --host 0.0.0.0

# ── pip-cache (pre-download Linux wheels for Docker builds) ───────────────────
# Run this on any machine with internet access before building in a restricted lab.
# Downloads manylinux wheels (Linux/Docker compatible) regardless of host OS.
pip-cache:
	pip download \
	  --platform manylinux2014_x86_64 \
	  --python-version 3.12 \
	  --implementation cp \
	  --abi cp312 \
	  --only-binary=:all: \
	  -r requirements.txt \
	  -d pip-cache/
