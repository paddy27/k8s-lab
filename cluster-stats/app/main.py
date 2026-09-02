from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app import aggregate, k8s_client
from app.vpa_manager import ensure_vpas_for_workloads

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cluster-stats")

VPA_RECONCILE_INTERVAL_SECONDS = 60


async def _vpa_reconcile_loop(client) -> None:
    while True:
        try:
            created = await ensure_vpas_for_workloads(client)
            if created:
                logger.info("created %d new VPA object(s)", created)
        except Exception:
            logger.exception("VPA reconcile loop failed")
        await asyncio.sleep(VPA_RECONCILE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = k8s_client.build_client()
    app.state.vpa_task = asyncio.create_task(_vpa_reconcile_loop(app.state.client))
    yield
    app.state.vpa_task.cancel()
    await app.state.client.aclose()


app = FastAPI(title="cluster-stats", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/cluster/summary")
async def cluster_summary():
    client = app.state.client
    nodes, node_metrics, namespaces, pods = await asyncio.gather(
        k8s_client.list_nodes(client),
        k8s_client.list_node_metrics(client),
        k8s_client.list_namespaces(client),
        k8s_client.list_pods(client),
    )
    nodes_summary = aggregate.summarize_nodes(nodes, node_metrics)
    ns_summary = aggregate.summarize_namespaces(namespaces, pods, [])
    return aggregate.summarize_cluster(nodes_summary, ns_summary, pods)


@app.get("/api/nodes")
async def nodes():
    client = app.state.client
    n, m = await asyncio.gather(k8s_client.list_nodes(client), k8s_client.list_node_metrics(client))
    return aggregate.summarize_nodes(n, m)


@app.get("/api/namespaces")
async def namespaces():
    client = app.state.client
    ns, pods, pod_metrics, deployments, daemonsets, statefulsets, services, configmaps, pvcs, jobs, cronjobs = await asyncio.gather(
        k8s_client.list_namespaces(client),
        k8s_client.list_pods(client),
        k8s_client.list_pod_metrics(client),
        k8s_client.list_deployments(client),
        k8s_client.list_daemonsets(client),
        k8s_client.list_statefulsets(client),
        k8s_client.list_services(client),
        k8s_client.list_configmaps(client),
        k8s_client.list_persistentvolumeclaims(client),
        k8s_client.list_jobs(client),
        k8s_client.list_cronjobs(client),
    )
    resource_kinds = {
        "Deployment": deployments,
        "DaemonSet": daemonsets,
        "StatefulSet": statefulsets,
        "Service": services,
        "ConfigMap": configmaps,
        "PVC": pvcs,
        "Job": jobs,
        "CronJob": cronjobs,
    }
    return aggregate.summarize_namespaces(ns, pods, pod_metrics, resource_kinds)


@app.get("/api/pods")
async def pods(namespace: Optional[str] = None):
    client = app.state.client
    p, m = await asyncio.gather(k8s_client.list_pods(client), k8s_client.list_pod_metrics(client))
    if namespace:
        p = [pod for pod in p if pod["metadata"]["namespace"] == namespace]
    return aggregate.summarize_pods(p, m)


@app.get("/api/workloads")
async def workloads(namespace: Optional[str] = None):
    client = app.state.client
    d, ds, ss = await asyncio.gather(
        k8s_client.list_deployments(client),
        k8s_client.list_daemonsets(client),
        k8s_client.list_statefulsets(client),
    )
    result = aggregate.summarize_workloads(d, ds, ss)
    if namespace:
        result = [w for w in result if w["namespace"] == namespace]
    return result


@app.get("/api/autoscaling/hpa")
async def hpas(namespace: Optional[str] = None):
    result = aggregate.summarize_hpas(await k8s_client.list_hpas(app.state.client))
    if namespace:
        result = [h for h in result if h["namespace"] == namespace]
    return result


@app.get("/api/autoscaling/vpa")
async def vpas(namespace: Optional[str] = None, only_with_data: bool = False):
    result = aggregate.summarize_vpas(await k8s_client.list_vpas(app.state.client))
    if namespace:
        result = [v for v in result if v["namespace"] == namespace]
    if only_with_data:
        result = [v for v in result if v["containers"]]
    return result


_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
def dashboard():
    return FileResponse(_STATIC_DIR / "index.html")
