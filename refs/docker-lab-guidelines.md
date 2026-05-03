# Docker lab guidelines (Docs Garage)

Conventions for `docker-compose.yml`, `Makefile`, and running the app in containers.

## Ports

| Profile | Host port | Container port | Notes |
|--------|-----------|----------------|--------|
| **dev** (`app-dev`) | **35050** | 5000 | Flask dev server, bind-mount for live reload |
| **prod** (`app-prod`) | **5000** | 5000 | Gunicorn |

Dev and prod can run side by side if you use profiles (`make dev` vs prod targets). On shared machines, set `COMPOSE_PROJECT_NAME=doc-preprocessor` to isolate networks and volumes.

## Host services from inside the container (`host.docker.internal`)

On **Docker Desktop** (macOS / Windows), `host.docker.internal` resolves to the host automatically.

On **Linux**, Engine often does **not** define that name unless you add it. This compose file sets:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

So the app can reach **LLM proxies**, **Ollama**, or other APIs bound on the host using base URLs such as `http://host.docker.internal:11434/v1` or `http://host.docker.internal:<port>/v1`.

Without this mapping, you may see **`[Errno -2] Name or service not known`** for `host.docker.internal` even though the same URL works from other contexts.

If `host-gateway` is unavailable on an older Compose/Engine, replace with the Docker bridge gateway IP (often `172.17.0.1`) or add an explicit `--add-host` equivalent.

## Proxies — the three layers

Corporate-proxy debugging fails most often because people fix one layer and assume the others are wired the same way. They are not. Here's the complete picture; configure each layer independently.

| Layer | Configured at | What it affects | Symptom when wrong |
|---|---|---|---|
| **1. Shell** | `export HTTP_PROXY=…` in your `~/.bashrc` etc. | `curl`, `pip`, `git`, **and** Compose's `${HTTP_PROXY:-}` substitution into build args / runtime env | `pip install` outside Docker fails; `make dev` runs but build args end up empty |
| **2. Build args / runtime env** | `docker-compose.yml` `x-proxy-args` and `x-proxy-env` (already wired) | `pip install` *inside* `docker build`; `requests` / `httpx` calls from the running Flask container | Build pulls work, but `pip install` inside build TLS/DNS-fails; or runtime LLM calls bypass proxy |
| **3. Docker daemon** | `/etc/systemd/system/docker.service.d/http-proxy.conf` | `docker pull` itself (base image, BuildKit frontend, anything fetched by the daemon) | `failed to resolve source metadata for …: 401 Unauthorized` or DNS errors *before* the Dockerfile runs |

Compose substitution accepts both casings — set whichever you prefer. URL-encode special chars in passwords (`!` → `%21`, `@` → `%40`, `:` → `%3A`).

### Layer 1 — shell

Set both casings; some libcurl-based tools only read the lowercase form.

```bash
export HTTP_PROXY="http://USER:PASSWORD@proxy-host:4433"
export HTTPS_PROXY="$HTTP_PROXY"
export http_proxy="$HTTP_PROXY"
export https_proxy="$HTTP_PROXY"
export NO_PROXY="localhost,127.0.0.1,::1,host.docker.internal,.corp.samsungelectronics.net"
export no_proxy="$NO_PROXY"
```

Or — equivalently for Compose only — copy [.env.example](../.env.example) to `.env` and uncomment the proxy block. Compose v2 auto-loads `.env` for `${VAR:-}` substitution, so this feeds Layer 2 without polluting your interactive shell.

### Layer 2 — build args + runtime env (already wired)

[docker-compose.yml](../docker-compose.yml) propagates both casings via `x-proxy-args` (build) and `x-proxy-env` (runtime), with cross-fallback so setting only `HTTP_PROXY` also fills `http_proxy` and vice versa. [Dockerfile](../Dockerfile) declares matching `ARG`s and persists them into `ENV` for the build stage, then resets them at the end so they don't leak into the final image runtime (which gets fresh values from `x-proxy-env`).

`NO_PROXY` defaults broaden to `localhost,127.0.0.1,::1,127.0.0.0/8,host.docker.internal` so local and host-bound LLM/Ollama URLs aren't forced through a bad proxy.

### Layer 3 — Docker daemon (Linux/systemd)

This is the only layer that affects `docker pull`. Required to fetch the base image, the BuildKit frontend, or any image the daemon itself loads. `make dev` clearing shell proxy will *not* help here — the daemon has its own config.

```bash
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/http-proxy.conf >/dev/null <<'EOF'
[Service]
Environment="HTTP_PROXY=http://USER:PASSWORD@proxy-host:4433"
Environment="HTTPS_PROXY=http://USER:PASSWORD@proxy-host:4433"
Environment="NO_PROXY=localhost,127.0.0.1,host.docker.internal,.corp.samsungelectronics.net"
EOF
sudo systemctl daemon-reload && sudo systemctl restart docker
docker info | grep -iE 'proxy'                                  # verify
docker pull public.ecr.aws/docker/library/python:3.12-slim       # smoke test
```

> **Docker Desktop (macOS / Windows):** instead of systemd, use *Settings → Resources → Proxies*. The same three layers still apply; only the Layer 3 surface differs.

### Make targets

- `make doctor` — prints daemon proxy config (Layer 3), shell proxy (Layer 1), then runs `docker pull` smoke tests for the base image and the legacy DH BuildKit frontend. Run this first when builds fail with proxy/registry errors.
- `make dev` — by default passes shell proxy through to compose (Layer 1 → Layer 2). Set `NO_DOCKER_PROXY=1` to force-clear (rare; only useful when daemon proxy is mis-auth'd *and* the registry is reachable directly).

### Symptoms → fix cheatsheet

| You see… | Layer | Fix |
|---|---|---|
| `failed to resolve source metadata for docker.io/...: 401 Unauthorized via …proxy…` on `docker compose build` | 3 | Daemon proxy creds wrong/expired; rewrite `http-proxy.conf` and `systemctl restart docker`. URL-encode the password. |
| `failed to resolve source metadata for docker.io/docker/dockerfile:1` | 3 | Caused by a `# syntax=docker/dockerfile:1` directive at the top of the Dockerfile. This repo no longer uses one — if you re-add it, you take on the dependency on Docker Hub being reachable from the daemon. |
| `pip install` inside build fails with TLS / DNS errors but `docker pull` works | 2 | Build args missing. Ensure shell `HTTP_PROXY` is set or `.env` proxy block is uncommented before `make dev`. Run `docker compose --profile dev exec app-dev env \| grep -i proxy` to verify the running container too. |
| `host.docker.internal` resolves to nothing or hits the proxy | 1/2 | Confirm `extra_hosts: host.docker.internal:host-gateway` (already set) and that `host.docker.internal` is in `NO_PROXY` for shell/daemon/runtime. |
| Cert errors (`SSL: CERTIFICATE_VERIFY_FAILED`) on `pip install` | 2 | Drop the corporate root `.crt` under `certs/`; the Dockerfile installs it into the image's CA store and points `REQUESTS_CA_BUNDLE` at it. |

### Base image and BuildKit notes

- **Base image:** build arg `PYTHON_IMAGE` defaults to AWS Public ECR's `python:3.12-slim` mirror (many labs block Docker Hub for image pulls). Override with `PYTHON_IMAGE=docker.io/library/python:3.12-slim` if your registry policy requires it.
- **No `# syntax=…` directive:** intentionally omitted from [Dockerfile](../Dockerfile) so the daemon never has to fetch `docker.io/docker/dockerfile:1` before the build starts. Modern BuildKit's built-in frontend (Docker Desktop / Compose v2) supports `RUN --mount=type=cache` without it.
- **Pip cache:** `RUN --mount=type=cache,target=/root/.cache/pip` means rebuilds reuse downloaded wheels on the Docker host without baking the cache into image layers. Use **BuildKit** (on by default in Docker Desktop / Compose v2; set `DOCKER_BUILDKIT=1` on older Linux). For fully offline builds, run `make pip-cache` to populate `pip-cache/` so the first `pip install` line can install with little or no PyPI traffic.
