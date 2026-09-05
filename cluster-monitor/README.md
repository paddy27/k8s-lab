# cluster-monitor

An intelligent Kubernetes cluster health platform, built to the stack
and Phase 2 scope of the shared "Kubernetes Cluster Monitoring
Dashboard" plan doc: React + Vite + Tailwind frontend, FastAPI +
official `kubernetes` Python client backend, PostgreSQL for issue
history. A separate build from [`../cluster-stats`](../cluster-stats/)
(which covers VPA/HPA resource recommendations) - this one is about
**detecting what's actually wrong**, not resource sizing.

![cluster-monitor dashboard](../docs/screenshots/cluster-monitor.png)

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

## Scheduling Analysis (Top 5 priority #2)

`PendingPod` above just says *that* a pod is stuck - these root-cause
*why*, straight from the scheduler's own `PodScheduled=False` condition
message (`_pod_scheduled_failure_message` /
`_scheduling_failure_causes` in `app/detector.py`) rather than
re-deriving it: a message like `"0/2 nodes are available: 1
Insufficient cpu, 1 Insufficient memory."` already names the cause, so
this just pattern-matches the substrings the scheduler is known to use.
A single message can name more than one cause - all matching ones are
flagged, each as its own independently-trackable issue alongside the
generic `PendingPod`:

| Rule | Severity | Matched from the scheduler's message |
|---|---|---|
| `InsufficientCPU` | critical | `"insufficient cpu"` |
| `InsufficientMemory` | critical | `"insufficient memory"` |
| `TaintsAndTolerations` | warning | `"taint"` |
| `NodeAffinity` | warning | `"node affinity"` / `"node selector"` |
| `PodAffinity` | warning | `"match pod affinity"` |
| `PodAntiAffinity` | warning | `"anti-affinity"` |

## Storage Analysis (Top 5 priority #3)

`app/storage.py` + a new `pvc_usage_samples` table (`app/db.py`). The
Kubernetes API genuinely cannot answer "how full is this PVC?" - a
PersistentVolumeClaim/PersistentVolume object only ever carries
*requested*/*bound* capacity, never usage. The only place real usage
lives is the kubelet's own `stats/summary` endpoint, reached via the API
server's node proxy (`k8s_client.list_all_volume_stats` /
`connect_get_node_proxy_with_path`, RBAC `nodes/proxy` get - see the
callout in `k8s/01-rbac.yaml`, it's a broader grant than anything else
in this app and worth reading before assuming it's free).

| Rule | Severity | Source |
|---|---|---|
| `FailedPVCBinding` | critical | PVC stuck `Pending` |
| `PVCAlmostFull` | critical ≥ 90%, warning ≥ 75% | latest kubelet usage/capacity sample |
| `PVCCapacityExhaustionPredicted` | critical ≤ 2 days out, warning ≤ 7 | linear fit over stored samples, extrapolated to capacity |
| `UnusedPVC` | info | `Bound`, but no running pod currently mounts it |
| `OrphanedPV` | warning | PV `Released` - claim deleted, reclaim policy kept the storage |

Usage is sampled roughly every 5 minutes (`main.py`'s
`STORAGE_SAMPLE_EVERY_N_CYCLES`, not every 30s detection cycle - a
volume filling up over days doesn't need that resolution, and the table
would otherwise grow unbounded; `db.prune_old_pvc_usage_samples` caps
history at 30 days). `predict_days_to_exhaustion` is an ordinary
least-squares fit of used bytes over time - same "needs a few samples
before it means anything" caveat as the VPA recommender elsewhere in
this project, not a guess dressed up as a hard number.

**A real limitation, found running this against the lab's own
cluster**: this lab's only StorageClass is `local-path-provisioner`,
which is `hostPath` under the hood - and the kubelet's volume-stats
collector has no `MetricsProvider` implementation for the `hostPath`
plugin, so `usedBytes`/`capacityBytes` are simply never reported for
*any* PVC here, no matter how full it actually gets.
`PVCAlmostFull`/`PVCCapacityExhaustionPredicted` are correct and
covered by `tests/test_storage.py`'s synthetic-data tests, and will
work against any real CSI driver that implements volume metrics (EBS
CSI, PD CSI, Ceph, Longhorn, OpenEBS, ... - the overwhelming majority of
production storage classes) - just not against this lab's own storage
backend. `FailedPVCBinding`, `UnusedPVC`, and `OrphanedPV` are
unaffected - they only read PVC/PV object status, never usage.

**Deliberately not built this pass**: `VolumeAttachmentIssues` (already
covered by the existing `Event:FailedMount`/`Event:FailedAttachVolume`
catch-all - a dedicated rule would just duplicate it) and
`StorageClassAnalysis` (misconfigured/missing StorageClass references -
not enough signal to be worth a dedicated rule with only one
StorageClass in this lab).

Plus one cross-pod check, `PodDistributionImbalance`
(`detect_scheduling_distribution_issues`): every *running* replica of
some workload landed on a single node - grouped by each pod's immediate
controller (a Deployment's pods are owned by a ReplicaSet, not the
Deployment itself - deliberately not resolved further up the owner
chain, same reasoning `cluster-stats`' recommendation engine already
documents for avoiding fragile owner matching). Only flagged with 3+
replicas (2 replicas on one node isn't "imbalanced", it's just what 2
replicas look like) and only on a cluster with more than one Ready node
(nothing to spread across otherwise - not a misconfiguration to fix).

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
`kubectl apply -f k8s/`.

The Service is `ClusterIP` (not directly exposed) - reachable at
`http://192.168.56.11:30090/monitor/` via [`../gateway`](../gateway/),
which also serves [`cluster-stats`](../cluster-stats/) at `/stats/` on
the same port. `index.html` has `<base href="/monitor/">` and every
`fetch()` in `src/api.js` uses a relative (no leading `/`) path
specifically so this works regardless of the browser's trailing slash
- see the comments in both files if you're changing either.

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
