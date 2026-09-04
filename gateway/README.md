# gateway

A single nginx reverse proxy in front of [`cluster-stats`](../cluster-stats/)
and [`cluster-monitor`](../cluster-monitor/), so both are reachable on
one port (`30090`) instead of two:

| Path | Routes to |
|---|---|
| `/` | this project's own landing page (`index.html`) |
| `/stats/` | `cluster-stats` |
| `/monitor/` | `cluster-monitor` |

## Why not just merge the two apps into one backend?

They already collide: both independently defined a `GET
/api/cluster/summary` endpoint. Routing by path to two separate
backends sidesteps that entirely - each app keeps its own native API
paths, tests, and deploy cycle, completely unaware the other exists.

## How the path-prefix routing actually works

`nginx.conf`'s `proxy_pass` targets end in a trailing slash
(`http://cluster-stats.cluster-stats.svc.cluster.local:8000/`), which
strips the `/stats` or `/monitor` prefix before forwarding - so each
backend keeps seeing its own untouched paths (`/api/...`, `/`),
exactly as when you run it standalone.

The harder part is the *frontend*: a page loaded at `/stats/` making a
plain `fetch("/api/nodes")` would hit the gateway's own `/api/nodes`
(there isn't one), not `/stats/api/nodes`. Both apps' frontends are
set up specifically to avoid this:

- `<base href="/stats/">` / `<base href="/monitor/">` in each
  `index.html`
- every fetch call and static asset reference is **relative** (no
  leading `/`) - `api/nodes`, `./assets/index-X.js`, etc.

With both in place, the browser resolves every relative reference
against the `<base>` href rather than the current URL - which also
sidesteps the classic "no trailing slash on the address bar" gotcha
that plain relative URLs would otherwise hit.

## Deploying

```bash
../deploy-image.sh gateway <tag> gateway
kubectl apply -f k8s/
```

Update the image tag in `k8s/01-app.yaml` to match before applying, as
with the other apps.

## Adding a third app

1. Give it the same treatment: `<base href="/whatever/">` +
   relative fetch/asset paths.
2. Convert its Service to `ClusterIP` (drop the NodePort).
3. Add a `location /whatever/ { proxy_pass http://...svc.cluster.local:PORT/; }`
   block (plus the bare-prefix redirect) to `nginx.conf`, rebuild, redeploy.
