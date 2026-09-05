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

from app import best_practices, detector, k8s_client, storage
from app.db import (
    Issue,
    SessionLocal,
    get_db,
    init_db,
    load_recent_pvc_samples,
    prune_old_pvc_usage_samples,
    record_pvc_usage_samples,
    reconcile_issues,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cluster-monitor")

DETECTION_INTERVAL_SECONDS = 30
EVENT_LOOKBACK_MINUTES = 30

# PVC usage is sampled far less often than issues are detected - a volume
# filling up over days/weeks doesn't need 30-second resolution, and the
# pvc_usage_samples table would otherwise grow unbounded. ~10 cycles at
# 30s/cycle is roughly every 5 minutes.
STORAGE_SAMPLE_EVERY_N_CYCLES = 10

_cycle_count = 0


async def _detection_loop(core, custom, apps, policy, networking, autoscaling) -> None:
    global _cycle_count
    while True:
        try:
            pods = await asyncio.to_thread(k8s_client.list_pods, core)
            nodes = await asyncio.to_thread(k8s_client.list_nodes, core)
            node_metrics = await asyncio.to_thread(k8s_client.list_node_metrics, custom)
            events = await asyncio.to_thread(k8s_client.list_recent_warning_events, core, EVENT_LOOKBACK_MINUTES)
            pvcs = await asyncio.to_thread(k8s_client.list_persistentvolumeclaims, core)
            pvs = await asyncio.to_thread(k8s_client.list_persistentvolumes, core)
            volume_stats = await asyncio.to_thread(k8s_client.list_all_volume_stats, core, nodes)
            deployments = await asyncio.to_thread(k8s_client.list_deployments, apps)
            statefulsets = await asyncio.to_thread(k8s_client.list_statefulsets, apps)
            pdbs = await asyncio.to_thread(k8s_client.list_poddisruptionbudgets, policy)
            networkpolicies = await asyncio.to_thread(k8s_client.list_networkpolicies, networking)
            hpas = await asyncio.to_thread(k8s_client.list_hpas, autoscaling)

            db = SessionLocal()
            try:
                if _cycle_count % STORAGE_SAMPLE_EVERY_N_CYCLES == 0:
                    record_pvc_usage_samples(db, volume_stats)
                    prune_old_pvc_usage_samples(db)
                samples_by_pvc = load_recent_pvc_samples(db)

                issues = detector.detect_all_issues(pods, nodes, node_metrics, events)
                issues += storage.build_storage_issues(pvcs, pvs, pods, volume_stats, samples_by_pvc)
                issues += best_practices.detect_all_best_practice_issues(
                    pods, deployments, statefulsets, pdbs, hpas, networkpolicies,
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
