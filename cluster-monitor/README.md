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

## Best Practices & Security (Top 5 priority #4)

`app/best_practices.py` - pure static analysis over already-fetched
cluster state (pods, Deployments/StatefulSets, PodDisruptionBudgets,
HorizontalPodAutoscalers, NetworkPolicies). No new metrics or history
needed here, unlike Storage/Scheduling above - just RBAC to read a few
more object kinds.

**Per-container security checks** (`detect_pod_security_issues`):

| Rule | Severity | Checks |
|---|---|---|
| `PrivilegedContainer` | critical | `securityContext.privileged: true` |
| `DangerousCapabilities` | critical | added Linux capability in a known-dangerous set (`SYS_ADMIN`, `NET_ADMIN`, `ALL`, ...) |
| `RunningAsRoot` | warning | nothing rules out root: no `runAsNonRoot: true` and no non-zero `runAsUser`, checked pod-level then container-level override |
| `HostNetwork` / `HostPID` / `HostIPC` | warning | shares the node's network/PID/IPC namespace |
| `HostPathVolume` | warning | any volume mounts a `hostPath` |
| `ImageSecurityIssues` | warning | no pinned tag, or `:latest` (registry:port prefixes handled correctly - see `_is_unpinned_image`) |
| `ServiceAccountAnalysis` | info | default ServiceAccount, token automount not explicitly disabled |
| `MissingResourceRequests` | warning | no `resources.requests` |
| `MissingResourceLimits` | info | no `resources.limits` |
| `MissingLivenessProbe` / `MissingReadinessProbe` | warning | probe absent |
| `MissingStartupProbe` | info | absent - only actually needed for slow-starting containers |

**Workload-level checks** (`detect_workload_best_practice_issues`, over
Deployments + StatefulSets):

| Rule | Severity | Checks |
|---|---|---|
| `SingleReplicaWorkload` | warning | `replicas == 1` |
| `MissingPDB` | info | 2+ replicas, no PodDisruptionBudget's `matchLabels` selector matches its pod template labels |
| `MissingHPA` | info | no HorizontalPodAutoscaler's `scaleTargetRef` points at it |

Plus one namespace-level check, `MissingNetworkPolicy` (info) - a
namespace with pods but zero NetworkPolicy objects, skipping
`kube-system`/`kube-public`/`kube-node-lease`. One issue per namespace,
not per pod.

**A known, expected effect of running this against a real cluster**:
infra components genuinely need what several of these rules flag.
Calico needs `privileged` + `hostNetwork`; kube-proxy needs
`hostNetwork` + `privileged`; several `kube-system`/add-on pods run as
root and skip probes entirely. Verified live against this lab's own
cluster: 143 active issues on first run, including 4
`PrivilegedContainer` (all correctly `calico-node`/`kube-proxy`, exactly
what a real scanner like kube-bench or Polaris would also flag for the
same pods) and a realistic spread of `RunningAsRoot`/missing-probe/
missing-PDB/missing-HPA findings elsewhere. That's not noise to
suppress - a namespace exclude-list would just be a different, more
arbitrary way of getting the same signal wrong, so there isn't one.

**Deliberately not built this pass**: `DeprecatedKubernetesAPIs`. A real
signal for this exists - the API server's own
`apiserver_requested_deprecated_apis` metric, incremented whenever any
client actually calls a deprecated API - but it means scraping the API
server's `/metrics`, a materially different, more sensitive data source
(a non-resource URL, not a typed API object) from everything else this
app reads. Worth a dedicated pass of its own rather than bolting onto
this one.

## Root Cause Analysis (Top 5 priority #5, final)

`app/root_cause.py` + `GET /api/incidents`. The plan doc's example shows
a timeline plus a root-cause probability breakdown (e.g. "Database
Connectivity 70%"). Fabricating a confidence number with no real
computation behind it would be dishonest - every other analysis module
in this project ties its numbers to something real (VPA's own model, an
actual linear fit, a literal RBAC-scoped count). This does the same:
each category's "probability" is just its share of a small, fixed set
of *real* correlating evidence found for that pod - normalized to add
up to 100 so it reads the way the plan doc's example does, but every
percentage point traces back to a specific, inspectable piece of
evidence in the same response, and there's an explicit "no correlating
signal found yet" case rather than ever forcing a distribution out of
nothing.

**What counts as an "incident"**: a pod with an active `CrashLoopBackOff`,
`OOMKilled`, or `ImagePullBackOff` - not just any `severity="critical"`
issue. That distinction mattered in practice: `PrivilegedContainer` and
`DangerousCapabilities` (Best Practices & Security) are also critical,
but they're static security-posture facts about by-design-privileged
infra (`calico-node`, `kube-proxy`), not something actively failing.
The first live version of this endpoint used severity alone and
surfaced every CNI pod as an "incident" on an otherwise healthy cluster
- fixed by scoping to `root_cause.INCIDENT_TRIGGER_RULES` instead (see
its docstring).

**The evidence rubric** (`_score_categories`) - fixed weights, no ML,
same spirit as `_sizing_recommendation` in cluster-stats or
`_scheduling_failure_causes` above:

| Category | Evidence | Weight |
|---|---|---|
| Resource Limits (Memory) | `OOMKilled` on this pod | 3 |
| Resource Limits (Memory) | node was under `HighMemoryUsage` | 1 |
| Image/Registry Issue | `ImagePullBackOff` on this pod | 3 |
| Network/Dependency Connectivity | a related issue's message mentions a connection failure (`connection refused`, `dial tcp`, `timed out`, ...) | 3 |
| Recent Deployment Rollout | the owning Deployment/StatefulSet's `Progressing` condition updated in the hour *before* the incident started | 2 |
| Node/Infrastructure | node was `NodeNotReady` | 3 |
| Node/Infrastructure | node was under `DiskPressure` | 2 |
| Application Error | `CrashLoopBackOff` with nothing else correlating | 1 (fallback) |

The owning workload is found by matching each Deployment/StatefulSet's
own `spec.selector` against the pod's labels - the same mechanism
Kubernetes itself uses, deliberately not an owner-reference chain walk
(a Deployment doesn't even directly own its pods - a ReplicaSet does),
consistent with this project's existing preference for avoiding fragile
owner matching. A rollout only counts as evidence if its timestamp
precedes the incident's start within `ROLLOUT_CORRELATION_WINDOW` (1
hour) - a rollout *after* the incident began can't have caused it, and
one from days ago is unrelated; both are checked with real datetime
arithmetic, not string matching.

The timeline merges every issue ever recorded for that pod, every issue
recorded for the node it ran on, and the rollout event (if one
correlates), sorted chronologically - a real, inspectable history, not
a narrative generated to look plausible.

`/api/incidents` computes this fresh on every request from the current
Issue history plus the last detection cycle's cached pod/workload state
(`main.py`'s `_latest_pods` et al.) - a read-time report over existing
data, not new state the background loop itself needs to write. No new
RBAC needed - built entirely on data Storage Analysis and Best
Practices & Security already fetch.

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

Read-only, cluster-wide, on exactly what each analysis pass reads -
`pods`, `nodes`, `events`, `metrics.k8s.io` nodes, `persistentvolumeclaims`/
`persistentvolumes`, `nodes/proxy` (Storage Analysis - see the callout
in `k8s/01-rbac.yaml`, broader than everything else here), and
`deployments`/`statefulsets`/`poddisruptionbudgets`/`networkpolicies`/
`horizontalpodautoscalers` (Best Practices & Security). No write verbs
on anything in the cluster - this app only ever observes and records to
its own Postgres.

## What's not built (yet)

Scoped out for this pass, per the plan doc's later phases and the "Top
5" priorities (see the root README's roadmap):

- **Health Score** - a single 0-100 rollup number
- **Deployment Health (full)** - rollout status and ReplicaSet history.
  `AppsV1Api` is wired up now (Best Practices & Security reads
  `.spec.replicas`/`.spec.template.metadata.labels`), but rollout
  status/history is a materially different feature, not yet built.
- **Namespace-level quotas** (ResourceQuota objects - distinct from the
  PVC capacity prediction Storage Analysis already covers)
- **Root Cause Analysis** (Top 5 priority #5 - not yet started)
- **Deprecated Kubernetes API detection** - see the callout in the
  Best Practices & Security section above for why this needs a
  different data source (the API server's own `/metrics`)
- **Recommendation Engine** (kubectl commands / runbook links per issue)
- **AI Assistant** (Phase 4 in the doc)
