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

## Corporate proxies

This codebase carries **no project-side proxy configuration** by default. It assumes the common corporate-network pattern: a *transparent intercepting proxy* that authenticates by source IP. You log in once via your browser's auth dialog ("the Firefox dance"), the proxy allowlists your IP for some TTL, and from then on all egress (docker pulls, pip installs, browser requests, anything) just works without any client-side proxy config.

### Daily flow

1. Open Firefox (or Chrome / Edge), navigate to anything outside the corporate network — the proxy auth dialog appears.
2. Enter your corporate credentials. Your IP is now allowlisted.
3. `make dev` (or `make doctor` to smoke-test pulls before building).

When the IP cache expires (Firefox dialog reappears, or `make doctor` starts failing with `unauthorized` / `authentication_failed`), repeat step 1.

### Optional: avoid the dance with `make proxy-keepalive`

Run in a separate terminal so the IP cache never expires:

```bash
export HTTP_PROXY="http://USER:PASSWORD@proxy-host:port"   # URL-encode: ! → %21, @ → %40
make proxy-keepalive
```

It curls a small URL (`google.com/generate_204` by default) through the proxy every 5 min. Each successful authenticated request refreshes the same IP cache that Firefox would. Tunable via `PROXY_REFRESH_SEC` and `PROXY_PROBE_URL`.

If keepalive logs `AUTH FAILED (407)`, the proxy probably doesn't accept Basic auth via `user:pass@host` — it requires NTLM/Kerberos browser auth. See [px-proxy fallback](#fallback-px-proxy-for-ntlm--negotiate-only-proxies) below.

### When `make doctor` fails

`make doctor` smoke-tests `docker pull` of the base image. If it fails with HTML body `Access Denied (authentication_failed)` or `unauthorized: <HTML>`, your IP cache has expired — do step 1 above. If it fails with DNS or "connection refused", you're off the corporate network entirely (VPN dropped?).

### Fallback: explicit proxy at runtime (only if needed)

Some networks don't use the IP-cache pattern and require explicit proxy env in every client. If that's you:

```bash
# Either in your shell:
export HTTP_PROXY="http://USER:PASSWORD@proxy:port"
export HTTPS_PROXY="$HTTP_PROXY"

# Or in this repo's .env (Compose v2 auto-loads it for ${VAR} substitution):
# HTTP_PROXY=http://USER:PASSWORD@proxy:port
# HTTPS_PROXY=http://USER:PASSWORD@proxy:port
```

The Flask container will pick these up via `env_file`. The Docker daemon itself (which performs `docker pull`) is configured separately — see "Configuring the daemon proxy" below.

#### Configuring the daemon proxy (rarely needed here)

Only relevant if your proxy does NOT do IP-cache auth and your daemon needs explicit credentials. The path depends on how Docker is installed:

**Snap-installed** (`which docker` returns `/snap/bin/docker`):

```bash
sudo snap set docker http-proxy="http://USER:PASSWORD@proxy:port"
sudo snap set docker https-proxy="http://USER:PASSWORD@proxy:port"
sudo snap set docker no-proxy="localhost,127.0.0.1,::1,host.docker.internal"
# Snap auto-restarts dockerd. To clear:
# sudo snap unset docker http-proxy https-proxy no-proxy
```

**Native systemd** (`docker.service` exists):

```bash
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/http-proxy.conf >/dev/null <<'EOF'
[Service]
Environment="HTTP_PROXY=http://USER:PASSWORD@proxy:port"
Environment="HTTPS_PROXY=http://USER:PASSWORD@proxy:port"
Environment="NO_PROXY=localhost,127.0.0.1,host.docker.internal"
EOF
sudo systemctl daemon-reload && sudo systemctl restart docker
```

**Verify either:** `docker info | grep -i proxy && docker pull public.ecr.aws/docker/library/python:3.12-slim`.

### Fallback: `px-proxy` for NTLM- / Negotiate-only proxies

If the corporate proxy refuses Basic auth (only Firefox can authenticate because it does NTLM/Kerberos via OS integration), run a local NTLM-aware proxy that handles upstream auth and exposes a plain endpoint:

```bash
pip3 install --user px-proxy
px --username "DOMAIN\\$USER" \
   --proxy "proxy-host:port" \
   --listen 127.0.0.1 --port 3128 &

export HTTP_PROXY="http://127.0.0.1:3128"
export HTTPS_PROXY="$HTTP_PROXY"
```

For snap docker: snap confinement may block `127.0.0.1` access from inside the snap. Run px on the host's LAN IP (`px --listen 0.0.0.0`) and point snap docker at that address.

## Asks for your sysadmin (cleaner setups)

The Firefox-dance pattern is fragile and undocumented at most companies. If you can shape policy with your IT/security team, ask for one of these — ordered by ease of approval:

1. **IP allowlist for dev workstations.** Adds your machine's IP (or a dev VLAN's CIDR) to the proxy's "no-auth" group. Zero client-side config, zero re-auth, no Firefox dance. Easiest ask, almost universally available in commercial proxy products. Cost: one ACL entry per dev.

2. **Internal package mirror (Nexus / Artifactory / Verdaccio).** Runs inside the network, mirrors PyPI / Docker Hub / npm / etc. Devs point pip and Docker at the internal mirror, which fetches and caches upstream content centrally. Dev tools never traverse the corporate proxy at all. **This is the gold standard.** Bonus: dramatically faster builds and survives upstream outages. Cost: one shared service, big infrastructure win for the whole team.

3. **Per-domain direct-egress allowlist.** If full IP allowlist is too broad, ask for direct (no-proxy) egress to known dev hosts: `*.docker.io`, `*.public.ecr.aws`, `*.pypi.org`, `*.pythonhosted.org`, `github.com`, `*.githubusercontent.com`, `*.huggingface.co`. These are well-known, reputable, and most security teams will approve them. The proxy still inspects browser traffic; dev tools bypass.

4. **Kerberos / SSO machine identity.** If the proxy uses Negotiate auth, configure dev workstations to authenticate via the machine's domain join (no user interaction). Eliminates the Firefox dance for everyone. Requires Active Directory integration on the proxy side; viable in most enterprise environments but more setup.

5. **TLS-passthrough for dev domains.** Even if your proxy must be in the path, request that it not MITM-intercept TLS for the package/registry domains in (3). Eliminates the `--trusted-host` workaround in [Dockerfile](../Dockerfile) and stops cert-validation failures across the board. Easier ask than dropping the proxy entirely.

6. **Service account / longer TTL.** Request a per-developer service account or just a longer TTL on the IP-cache (e.g. 8h instead of 30min) for dev workstations. Reduces — doesn't eliminate — the dance.

7. **PAC / WPAD with auto-config.** If the proxy must stay, at least have it auto-configure via PAC so all tools (browsers, system, Docker, etc.) share one source of truth. Reduces drift between what your shell, daemon, and browser think the proxy is.

The best ask is **(1) for individuals** and **(2) for the team**. Either makes this whole document obsolete.

## Base image and BuildKit

- **Base image:** `PYTHON_IMAGE` build arg defaults to `public.ecr.aws/docker/library/python:3.12-slim` (many corporate networks block Docker Hub for image pulls). Override with `PYTHON_IMAGE=docker.io/library/python:3.12-slim` if your registry policy requires it.
- **No `# syntax=…` directive:** intentionally omitted from [Dockerfile](../Dockerfile). Modern BuildKit's built-in frontend (Docker Desktop / Compose v2) supports `RUN --mount=type=cache` without it; including the directive would force an extra Docker Hub fetch (`docker.io/docker/dockerfile:1`) before the build can start.
- **Pip cache:** `RUN --mount=type=cache,target=/root/.cache/pip` lets rebuilds reuse downloaded wheels on the Docker host without baking them into image layers. Use BuildKit (default in Compose v2; set `DOCKER_BUILDKIT=1` on older Linux). For fully offline builds, `make pip-cache` populates `pip-cache/` and the Dockerfile's first `pip install` line installs from it before falling back to PyPI.
- **TLS interception:** drop corporate root `.crt` files into `certs/` at the repo root; the Dockerfile installs them into the image's CA store and points `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` at the system bundle. Safe no-op when `certs/` is empty.
