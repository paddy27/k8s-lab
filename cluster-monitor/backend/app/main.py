from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app import best_practices, detector, k8s_client, predictions, root_cause, storage
from app.db import (
    Issue,
    SessionLocal,
    get_db,
    init_db,
    load_recent_cluster_snapshots,
    load_recent_node_samples,
    load_recent_pvc_samples,
    prune_old_cluster_snapshot_samples,
    prune_old_node_usage_samples,
    prune_old_pvc_usage_samples,
    record_cluster_snapshot_sample,
    record_node_usage_samples,
    record_pvc_usage_samples,
    reconcile_issues,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cluster-monitor")

DETECTION_INTERVAL_SECONDS = 30
EVENT_LOOKBACK_MINUTES = 30

# PVC/node/cluster usage history (Storage Analysis + Trend & Prediction
# Analysis) is sampled far less often than issues are detected - a
# volume/node filling up over days/weeks doesn't need 30-second
# resolution, and these tables would otherwise grow unbounded. ~10
# cycles at 30s/cycle is roughly every 5 minutes.
STORAGE_SAMPLE_EVERY_N_CYCLES = 10

_cycle_count = 0

# Cached from the last detection cycle so /api/incidents (Root Cause
# Analysis) can reuse already-fetched cluster state instead of hitting
# the API server again on every request - it's already refreshed every
# DETECTION_INTERVAL_SECONDS regardless of whether anyone's looking at it.
_latest_pods: list[dict] = []
_latest_deployments: list[dict] = []
_latest_statefulsets: list[dict] = []


async def _detection_loop(core, custom, apps, policy, networking, autoscaling) -> None:
    global _cycle_count, _latest_pods, _latest_deployments, _latest_statefulsets
    while True:
        try:
            pods = await asyncio.to_thread(k8s_client.list_pods, core)
            nodes = await asyncio.to_thread(k8s_client.list_nodes, core)
            node_metrics = await asyncio.to_thread(k8s_client.list_node_metrics, custom)
            events = await asyncio.to_thread(k8s_client.list_recent_warning_events, core, EVENT_LOOKBACK_MINUTES)
            pvcs = await asyncio.to_thread(k8s_client.list_persistentvolumeclaims, core)
            pvs = await asyncio.to_thread(k8s_client.list_persistentvolumes, core)
            allocatable_by_node = {
                n["metadata"]["name"]: {
                    "cpu_millicores": detector.parse_cpu_millicores(n.get("status", {}).get("allocatable", {}).get("cpu")),
                    "memory_bytes": detector.parse_memory_bytes(n.get("status", {}).get("allocatable", {}).get("memory")),
                }
                for n in nodes
            }
            volume_stats, node_resource_stats = await asyncio.to_thread(
                k8s_client.list_all_node_stats, core, nodes, allocatable_by_node,
            )
            deployments = await asyncio.to_thread(k8s_client.list_deployments, apps)
            statefulsets = await asyncio.to_thread(k8s_client.list_statefulsets, apps)
            pdbs = await asyncio.to_thread(k8s_client.list_poddisruptionbudgets, policy)
            networkpolicies = await asyncio.to_thread(k8s_client.list_networkpolicies, networking)
            hpas = await asyncio.to_thread(k8s_client.list_hpas, autoscaling)

            _latest_pods, _latest_deployments, _latest_statefulsets = pods, deployments, statefulsets

            db = SessionLocal()
            try:
                if _cycle_count % STORAGE_SAMPLE_EVERY_N_CYCLES == 0:
                    record_pvc_usage_samples(db, volume_stats)
                    prune_old_pvc_usage_samples(db)
                    record_node_usage_samples(db, node_resource_stats)
                    prune_old_node_usage_samples(db)
                    total_restarts = sum(
                        cs.get("restart_count", 0)
                        for p in pods
                        for cs in (p.get("status", {}).get("container_statuses") or [])
                    )
                    record_cluster_snapshot_sample(db, pod_count=len(pods), total_restart_count=total_restarts)
                    prune_old_cluster_snapshot_samples(db)
                samples_by_pvc = load_recent_pvc_samples(db)
                samples_by_node = load_recent_node_samples(db)
                cluster_snapshots = load_recent_cluster_snapshots(db)

                issues = detector.detect_all_issues(pods, nodes, node_metrics, events)
                issues += storage.build_storage_issues(pvcs, pvs, pods, volume_stats, samples_by_pvc)
                issues += best_practices.detect_all_best_practice_issues(
                    pods, deployments, statefulsets, pdbs, hpas, networkpolicies,
                )
                issues += predictions.build_all_prediction_issues(
                    samples_by_node, [(t, r) for t, _, r in cluster_snapshots],
                )

                reconcile_issues(db, issues)
            finally:
                db.close()

            _cycle_count += 1
            logger.info("detection cycle: %d issue(s) currently active", len(issues))
        except Exception:
            logger.exception("detection cycle failed")
        await asyncio.sleep(DETECTION_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    core, custom, apps, policy, networking, autoscaling = k8s_client.build_api_clients()
    app.state.core, app.state.custom = core, custom
    app.state.detection_task = asyncio.create_task(
        _detection_loop(core, custom, apps, policy, networking, autoscaling)
    )
    yield
    app.state.detection_task.cancel()


app = FastAPI(title="cluster-monitor", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


def _serialize_issue(issue: Issue) -> dict:
    return {
        "id": issue.id,
        "rule": issue.rule,
        "severity": issue.severity,
        "namespace": issue.namespace,
        "resource_kind": issue.resource_kind,
        "resource_name": issue.resource_name,
        "message": issue.message,
        "first_seen": issue.first_seen.isoformat() if issue.first_seen else None,
        "last_seen": issue.last_seen.isoformat() if issue.last_seen else None,
        "resolved_at": issue.resolved_at.isoformat() if issue.resolved_at else None,
        "active": issue.active,
    }


@app.get("/api/issues")
def list_issues(
    active: Optional[bool] = None,
    namespace: Optional[str] = None,
    severity: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(Issue)
    if active is not None:
        query = query.filter(Issue.active == active)
    if namespace:
        query = query.filter(Issue.namespace == namespace)
    if severity:
        query = query.filter(Issue.severity == severity)
    rows = query.order_by(Issue.last_seen.desc()).limit(500).all()
    return [_serialize_issue(r) for r in rows]


@app.get("/api/predictions")
def get_predictions(db: Session = Depends(get_db)):
    """Raw forecast numbers backing Trend & Prediction Analysis, for a
    dashboard to chart directly - distinct from the NodeCapacityExhaustion
    Predicted/MemoryGrowthPredicted/DiskExhaustionPredicted/
    RestartTrendIncreasing issues in /api/issues, which are the
    "act on this" view of the same underlying history. Cluster Capacity
    Forecast and Pod Growth Trend have no natural pass/fail threshold,
    so they're only ever exposed here, never as an issue."""
    samples_by_node = load_recent_node_samples(db)
    cluster_snapshots = load_recent_cluster_snapshots(db)
    return {
        "cluster_capacity_forecast": predictions.build_cluster_capacity_forecast(samples_by_node),
        "pod_growth_trend": predictions.build_pod_growth_trend([(t, p) for t, p, _ in cluster_snapshots]),
    }


@app.get("/api/issues/summary")
def issues_summary(db: Session = Depends(get_db)):
    active_issues = db.query(Issue).filter(Issue.active == True).all()  # noqa: E712
    return {
        "total_active": len(active_issues),
        "critical": sum(1 for i in active_issues if i.severity == "critical"),
        "warning": sum(1 for i in active_issues if i.severity == "warning"),
        "by_rule": {
            rule: sum(1 for i in active_issues if i.rule == rule)
            for rule in sorted({i.rule for i in active_issues})
        },
    }


def _issue_row_to_evidence_dict(issue: Issue) -> dict:
    """Like _serialize_issue, but keeps first_seen as a real datetime -
    root_cause.py does date arithmetic (the rollout-correlation window)
    on it, so it can't be pre-stringified the way the API-facing
    serializer does."""
    return {"rule": issue.rule, "message": issue.message, "first_seen": issue.first_seen}


@app.get("/api/incidents")
def list_incidents(db: Session = Depends(get_db)):
    """Root Cause Analysis (Top 5 priority #5): one incident report per
    pod currently failing in a way that actually warrants root-causing
    (root_cause.INCIDENT_TRIGGER_RULES - CrashLoopBackOff/OOMKilled/
    ImagePullBackOff, not just any severity="critical" issue: see that
    constant's docstring for why PrivilegedContainer et al. don't
    belong here). Computed fresh on every request from the current
    Issue history + the last detection cycle's cached pod/workload
    state (see _latest_pods et al.) rather than something the
    background loop itself writes - this is a read-time report over
    existing data, not new state to reconcile."""
    active_incidents = (
        db.query(Issue)
        .filter(Issue.active == True, Issue.rule.in_(root_cause.INCIDENT_TRIGGER_RULES), Issue.resource_kind == "Pod")  # noqa: E712
        .all()
    )

    pods_by_key = {(p["metadata"]["namespace"], p["metadata"]["name"]): p for p in _latest_pods}

    incidents = []
    seen = set()
    for issue in active_incidents:
        key = (issue.namespace, issue.resource_name)
        if key in seen:
            continue
        seen.add(key)

        related_issues = [
            _issue_row_to_evidence_dict(i) for i in
            db.query(Issue)
            .filter(Issue.namespace == issue.namespace, Issue.resource_kind == "Pod", Issue.resource_name == issue.resource_name)
            .all()
        ]

        pod = pods_by_key.get(key)
        node_name = pod.get("spec", {}).get("node_name") if pod else None
        node_issues = [
            _issue_row_to_evidence_dict(i) for i in
            db.query(Issue).filter(Issue.resource_kind == "Node", Issue.resource_name == node_name).all()
        ] if node_name else []

        workload = root_cause.workload_for_pod(pod, _latest_deployments, _latest_statefulsets) if pod else None

        incidents.append(root_cause.analyze_incident(issue.namespace, issue.resource_name, related_issues, node_issues, workload))

    return incidents


@app.get("/api/cluster/summary")
async def cluster_summary():
    nodes = await asyncio.to_thread(k8s_client.list_nodes, app.state.core)
    pods = await asyncio.to_thread(k8s_client.list_pods, app.state.core)

    nodes_ready = sum(
        1 for n in nodes
        if any(c["type"] == "Ready" and c["status"] == "True" for c in n.get("status", {}).get("conditions", []))
    )
    pods_by_phase: dict[str, int] = {}
    for p in pods:
        phase = p.get("status", {}).get("phase", "Unknown")
        pods_by_phase[phase] = pods_by_phase.get(phase, 0) + 1

    return {
        "node_count": len(nodes),
        "nodes_ready": nodes_ready,
        "pod_count": len(pods),
        "pods_by_phase": pods_by_phase,
    }


_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="assets")


@app.get("/")
@app.get("/{full_path:path}")
def spa(full_path: str = ""):
    """Serves the built React app for any non-API path - a client-side
    router (if/when one's added) can then take over in the browser."""
    index = _STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"detail": "frontend not built - see frontend/README or run `npm run build`"}
