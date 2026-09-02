"""Thin client for talking to the Kubernetes API server directly.

In-cluster (the real deployment): uses the ServiceAccount token + CA cert
that Kubernetes auto-mounts into every pod.

Local dev: set K8S_API_URL (e.g. http://localhost:8001 from
`kubectl proxy`, which handles auth itself - no token needed).
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx

_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def build_client() -> httpx.AsyncClient:
    override_url = os.environ.get("K8S_API_URL")
    if override_url:
        return httpx.AsyncClient(base_url=override_url, timeout=10.0)

    token = (_SA_DIR / "token").read_text().strip()
    api_host = os.environ["KUBERNETES_SERVICE_HOST"]
    api_port = os.environ["KUBERNETES_SERVICE_PORT"]
    return httpx.AsyncClient(
        base_url=f"https://{api_host}:{api_port}",
        headers={"Authorization": f"Bearer {token}"},
        verify=str(_SA_DIR / "ca.crt"),
        timeout=10.0,
    )


async def get_json(client: httpx.AsyncClient, path: str) -> dict:
    resp = await client.get(path)
    resp.raise_for_status()
    return resp.json()


# k8s's "partial object metadata" representation: name/namespace/labels/
# etc. only, never spec or data. Used for resources we only ever count -
# no reason to pull (or even be able to pull, ideally) full object bodies
# just to compute how many of something exist per namespace.
_METADATA_ONLY_ACCEPT = "application/json;as=PartialObjectMetadataList;v=v1;g=meta.k8s.io,application/json"


async def get_metadata_only(client: httpx.AsyncClient, path: str) -> list[dict]:
    resp = await client.get(path, headers={"Accept": _METADATA_ONLY_ACCEPT})
    resp.raise_for_status()
    # A PartialObjectMetadataList with zero items comes back as
    # "items": null rather than "items": [] - confirmed against a real
    # cluster (jobs/cronjobs/pvcs all empty here). Normal (non-metadata)
    # List responses don't do this; this representation apparently does.
    return resp.json().get("items") or []


# --- Raw resource fetches -------------------------------------------------

async def list_nodes(client: httpx.AsyncClient) -> list[dict]:
    return (await get_json(client, "/api/v1/nodes"))["items"]


async def list_node_metrics(client: httpx.AsyncClient) -> list[dict]:
    """Live CPU/memory usage per node, from metrics-server. Empty list if
    metrics-server isn't installed/ready rather than raising - usage is a
    nice-to-have, the rest of the app should work without it."""
    try:
        data = await get_json(client, "/apis/metrics.k8s.io/v1beta1/nodes")
        return data["items"]
    except httpx.HTTPStatusError:
        return []


async def list_namespaces(client: httpx.AsyncClient) -> list[dict]:
    return (await get_json(client, "/api/v1/namespaces"))["items"]


async def list_pods(client: httpx.AsyncClient) -> list[dict]:
    return (await get_json(client, "/api/v1/pods"))["items"]


async def list_pod_metrics(client: httpx.AsyncClient) -> list[dict]:
    try:
        data = await get_json(client, "/apis/metrics.k8s.io/v1beta1/pods")
        return data["items"]
    except httpx.HTTPStatusError:
        return []


async def list_deployments(client: httpx.AsyncClient) -> list[dict]:
    return (await get_json(client, "/apis/apps/v1/deployments"))["items"]


async def list_daemonsets(client: httpx.AsyncClient) -> list[dict]:
    return (await get_json(client, "/apis/apps/v1/daemonsets"))["items"]


async def list_statefulsets(client: httpx.AsyncClient) -> list[dict]:
    return (await get_json(client, "/apis/apps/v1/statefulsets"))["items"]


async def list_hpas(client: httpx.AsyncClient) -> list[dict]:
    return (await get_json(client, "/apis/autoscaling/v2/horizontalpodautoscalers"))["items"]


async def list_vpas(client: httpx.AsyncClient) -> list[dict]:
    try:
        data = await get_json(client, "/apis/autoscaling.k8s.io/v1/verticalpodautoscalers")
        return data["items"]
    except httpx.HTTPStatusError:
        return []  # VPA CRDs not installed


# --- Count-only resources: metadata only, never full bodies. Deliberately
#     no Secrets here - see app/main.py / project README for why.

async def list_services(client: httpx.AsyncClient) -> list[dict]:
    return await get_metadata_only(client, "/api/v1/services")


async def list_configmaps(client: httpx.AsyncClient) -> list[dict]:
    return await get_metadata_only(client, "/api/v1/configmaps")


async def list_persistentvolumeclaims(client: httpx.AsyncClient) -> list[dict]:
    return await get_metadata_only(client, "/api/v1/persistentvolumeclaims")


async def list_jobs(client: httpx.AsyncClient) -> list[dict]:
    return await get_metadata_only(client, "/apis/batch/v1/jobs")


async def list_cronjobs(client: httpx.AsyncClient) -> list[dict]:
    return await get_metadata_only(client, "/apis/batch/v1/cronjobs")


async def create_vpa(client: httpx.AsyncClient, namespace: str, body: dict) -> None:
    resp = await client.post(
        f"/apis/autoscaling.k8s.io/v1/namespaces/{namespace}/verticalpodautoscalers",
        json=body,
    )
    if resp.status_code not in (200, 201, 409):  # 409 = already exists, fine
        resp.raise_for_status()
