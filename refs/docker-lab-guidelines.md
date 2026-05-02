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

## Build-time proxies

Lab networks sometimes require an HTTP proxy for `docker build` / `pip install`. The Makefile clears proxy env for compose by default so pulls/builds are less likely to break; see `Makefile` and use `make dev USE_SYSTEM_PROXY=1` when you intend to pass the host proxy into Compose.

`NO_PROXY` in compose includes `localhost` and related ranges so local/registry traffic is less often forced through a bad proxy.
