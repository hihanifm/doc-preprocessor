# URL Usage Guideline — Reverse Proxy Safety

This app may run directly (e.g. `http://host:5000/`) **or** behind a path-prefix reverse
proxy (e.g. `http://proxy/myapp/`). The proxy strips the prefix before forwarding — the
backend never sees it. All client-side URLs must survive both deployments with no code change.

---

## Rules

### 1. Never hardcode a leading `/` in any browser-constructed URL

Applies to: `fetch()`, `axios` / `ky` / any HTTP client, `<form action>`, `<a href>`,
`<img src>`, `<script src>`, `EventSource`, and `WebSocket` paths.

```js
// wrong — resolves to http://proxy/api/data, proxy can't route it back
fetch('/api/data')

// correct — resolves to http://proxy/myapp/api/data, proxy strips prefix → /api/data
fetch('api/data')
```

### 2. For JS routers, set `basename` / `base` once — don't rewrite every `push`

```js
// React Router v6
<BrowserRouter basename={window.__BASE_PATH__ || '/'}>

// Vue Router
createRouter({ history: createWebHistory(import.meta.env.BASE_URL) })

// Vite (vite.config.js)
export default { base: process.env.BASE_URL || '/' }
```

Internal `router.push('/page')` calls are handled by the router after that — do not
rewrite them manually.

### 3. For centralised HTTP clients, set `baseURL` once at initialisation

```js
// axios
const api = axios.create({ baseURL: window.__BASE_PATH__ || '' });

// then everywhere: api.get('users')  — never  api.get('/users')
```

### 4. Fully absolute URLs are unaffected

`https://external.api/data` bypasses the proxy by design — no change needed.

---

## Proxy-side requirement

The prefix must end with a trailing slash in the browser URL (users land at `/myapp/`,
not `/myapp`). Standard across nginx, Traefik, Caddy, and HAProxy.

```nginx
# nginx example
location /myapp/ {
    proxy_pass http://backend:5000/;   # trailing slash strips the prefix
    proxy_set_header Host $host;
}
```

---

## Quick reference

| Scenario | Rule |
|---|---|
| Plain `fetch` / HTTP client calls | Document-relative paths — no leading `/` |
| JS router navigation | Set `basename` / `base` once in router config |
| Asset URLs (`img`, `script`, `link`) | Relative paths, or set `<base href>` in `<head>` |
| Centralised client (axios, ky, etc.) | Set `baseURL` at construction time |
| External API calls | Fully absolute — no change needed |

---

**One principle:** let the browser resolve URLs relative to where the page actually lives,
rather than assuming the app is always mounted at `/`.
