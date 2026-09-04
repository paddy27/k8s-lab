# cluster-monitor

An intelligent Kubernetes cluster health platform, built to the stack
and Phase 2 scope of the shared "Kubernetes Cluster Monitoring
Dashboard" plan doc: React + Vite + Tailwind frontend, FastAPI +
official `kubernetes` Python client backend, PostgreSQL for issue
history. A separate build from [`../cluster-stats`](../cluster-stats/)
(which covers VPA/HPA resource recommendations) - this one is about
**detecting what's actually wrong**, not resource sizing.

## What it detects (Phase 2: Intelligent Detection)

A background loop polls the cluster every 30s and reconciles what it
finds against Postgres - new problems are recorded, ones that clear up
are marked resolved, ones still present just get their `last_seen`
bumped. That reconciliation (`app/db.py: reconcile_issues`) is what
turns point-in-time snapshots into an actual issue history.

| Rule | Severity | Source |
|---|---|---|
| `CrashLoopBackOff` | critical | pod container status |
| `ImagePullBackOff` / `ErrImagePull` | critical | pod container status |
| `OOMKilled` | critical | pod container's last terminated state |
| `NodeNotReady` | critical | node condition |
| `DiskPressure` / `MemoryPressure` | critical | node condition |
| `PendingPod` | warning | pod phase |
| `FrequentRestarts` | warning | pod container restart count (>= 5) |
| `HighCPUUsage` / `HighMemoryUsage` | warning | metrics-server, >= 85% of allocatable |
| `Event:<reason>` | critical for a known set (`FailedScheduling`, `FailedMount`, `FailedAttachVolume`, `FailedCreatePodSandBox`, `OOMKilling`, `Evicted`, `NodeNotReady`), warning otherwise | recent Warning-type Kubernetes Events, deduped and counted |

The event-based catch-all exists because some real problems
(`FailedMount`, `FailedScheduling`) have no dedicated status field to
poll - they only ever show up as Events.

## Architecture

```
frontend/  React + Vite + Tailwind - Overview + Issues pages
backend/   FastAPI - detection loop + REST API, backed by Postgres
k8s/       namespace, RBAC, Postgres, app manifests
Dockerfile multi-stage: builds the frontend, then bakes the built
           static files into the backend's image (one container, one
           Deployment - no separate nginx/frontend pod)
```

No Redis (yet) - a single polling loop writing straight to Postgres
doesn't need a task queue. Add one if/when background work actually
needs to fan out across workers.

## A real bug hit deploying this: cryptography SIGILLs on this host

First deploy crash-looped with exit code 132 (SIGILL) and an empty
`kubectl logs` - the process died before anything could flush. Bisected
with `python -X faulthandler -c 'import kubernetes'` run directly on
`buildserver` (same arm64 VirtualBox VM as the cluster nodes) down to:
`kubernetes.config` -> `google.auth.transport.requests` ->
`google.oauth2.service_account` -> `google.auth.crypt.es` ->
`cryptography.exceptions`, at which point loading `cryptography`'s
compiled Rust extension crashes outright. This chain is loaded purely
by importing the `kubernetes` package's public API - not by anything
this app actually calls (no GKE/service-account JSON auth is ever
used here).

The unconstrained dependency resolution had pulled `cryptography`
50.0.1 - an unusually new release with no track record on this kind
of virtualized arm64 CPU. Pinning `cryptography==42.0.5` (a
long-established release) in `requirements.txt` fixed it outright, no
Rust toolchain or building-from-source required. Verified by running
`import kubernetes` directly in the built image on `buildserver`
before ever rolling it out to the cluster.

## Running it

**In the cluster**: `../deploy-image.sh cluster-monitor <tag> cluster-monitor`
(builds on `buildserver`, pushes to its registry, pre-pulls onto both
nodes - see the repo root README for why), then
`kubectl apply -f k8s/`. Reachable at `http://192.168.56.11:30091`.

**Locally, backend only, against the real cluster**:

```bash
kubectl proxy --port=8001 &
cd backend
pip install -r requirements-dev.txt
DATABASE_URL=sqlite:///./local.db K8S_API_URL=http://localhost:8001 \
  uvicorn app.main:app --port 8500 --reload
```

(SQLite works fine for local poking around - `app/db.py` uses no
Postgres-specific features. The real deployment always uses Postgres.)

**Frontend dev server** (proxies API calls to the backend above):

```bash
cd frontend
npm install
npm run dev
```

## Tests

```bash
cd backend
pip install -r requirements-dev.txt
pytest
```

Covers `app/detector.py` (every rule, as pure functions over plain
fixture dicts) and `app/db.py`'s reconciliation logic (insert / bump /
resolve / reactivate, against an in-memory SQLite DB - no real Postgres
needed for the test suite).

## RBAC

Read-only, cluster-wide, on exactly what detection reads: `pods`,
`nodes`, `events`, and `metrics.k8s.io` nodes. No write verbs on
anything in the cluster - this app only ever observes and records to
its own Postgres.

## What's not built (yet)

Scoped out for this pass, per the plan doc's later phases:

- **Health Score** - a single 0-100 rollup number
- **Deployment Health** - rollout status, ReplicaSet history, desired
  vs. available replicas (needs `AppsV1Api`, deliberately not wired up
  yet - see the comment in `app/k8s_client.py`)
- **Namespace-level quotas / PVC usage**
- **Recommendation Engine** (kubectl commands / runbook links per issue)
- **AI Assistant** (Phase 4 in the doc)
