"""Auto-creates recommendation-only VPA objects for every Deployment,
DaemonSet, and StatefulSet that doesn't already have one.

The VPA recommender only computes recommendations for workloads that have
a VerticalPodAutoscaler object pointing at them - it doesn't do this for
every pod automatically. Rather than make the user manually create one
per workload, this keeps every workload covered on an ongoing basis.

updateMode is always "Off": this only ever *observes* and *recommends*.
It never resizes or evicts anything - that would need the VPA updater,
which we deliberately did not install.
"""
from __future__ import annotations

import logging

import httpx

from app import k8s_client

logger = logging.getLogger("cluster-stats.vpa")

_MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
_MANAGED_BY_VALUE = "cluster-stats"

# (targetRef.kind, apiVersion) for each controller kind we cover.
_WORKLOAD_KINDS = (
    ("Deployment", k8s_client.list_deployments),
    ("DaemonSet", k8s_client.list_daemonsets),
    ("StatefulSet", k8s_client.list_statefulsets),
)


async def ensure_vpas_for_workloads(client: httpx.AsyncClient) -> int:
    """Returns the number of VPA objects newly created."""
    existing_vpas = await k8s_client.list_vpas(client)
    covered = {
        (v["metadata"]["namespace"], v["spec"]["targetRef"]["kind"], v["spec"]["targetRef"]["name"])
        for v in existing_vpas
    }

    created = 0
    for kind, list_fn in _WORKLOAD_KINDS:
        for obj in await list_fn(client):
            ns, name = obj["metadata"]["namespace"], obj["metadata"]["name"]
            if (ns, kind, name) in covered:
                continue

            body = {
                "apiVersion": "autoscaling.k8s.io/v1",
                "kind": "VerticalPodAutoscaler",
                "metadata": {
                    # kind prefix avoids a name collision if a namespace
                    # happens to have e.g. both a Deployment and a
                    # DaemonSet called the same thing.
                    "name": f"{kind.lower()}-{name}-auto"[:253],
                    "namespace": ns,
                    "labels": {_MANAGED_BY_LABEL: _MANAGED_BY_VALUE},
                },
                "spec": {
                    "targetRef": {"apiVersion": "apps/v1", "kind": kind, "name": name},
                    "updatePolicy": {"updateMode": "Off"},
                },
            }
            try:
                await k8s_client.create_vpa(client, ns, body)
                created += 1
            except httpx.HTTPStatusError as exc:
                logger.warning("failed to create VPA for %s %s/%s: %s", kind, ns, name, exc)

    return created
