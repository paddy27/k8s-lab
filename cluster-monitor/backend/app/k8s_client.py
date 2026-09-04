"""Kubernetes access via the official `kubernetes` Python client.

In-cluster: loads the ServiceAccount token/CA automatically
(`config.load_incluster_config()`).

Local dev: set K8S_API_URL (e.g. http://localhost:8001 from
`kubectl proxy`, which handles auth itself) and this builds a bare,
unauthenticated Configuration pointed at it instead.

Every fetch returns `.to_dict()` output (plain nested dicts, snake_case
keys - the client's own attribute names, not raw K8s camelCase JSON)
rather than the typed model objects. Keeps app/detector.py a pure
function over plain data, exactly like the rest of this repo's apps -
easy to unit test with plain fixtures, no client-library mocking.

Only CoreV1Api + CustomObjectsApi (for metrics.k8s.io) so far - this is
Phase 2 (pod/node/event issue detection). AppsV1Api (Deployment
rollout status, ReplicaSet history) belongs to a later "Deployment
Health" phase; add it back here when that's actually built, not before.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from kubernetes import client, config


def build_api_clients() -> tuple[client.CoreV1Api, client.CustomObjectsApi]:
    override_url = os.environ.get("K8S_API_URL")
    if override_url:
        cfg = client.Configuration()
        cfg.host = override_url
        api_client = client.ApiClient(configuration=cfg)
    else:
        config.load_incluster_config()
        api_client = client.ApiClient()

    return client.CoreV1Api(api_client), client.CustomObjectsApi(api_client)


def list_nodes(core: client.CoreV1Api) -> list[dict]:
    return core.list_node().to_dict()["items"]


def list_node_metrics(custom: client.CustomObjectsApi) -> list[dict]:
    """Live per-node CPU/memory usage from metrics-server. Empty list if
    metrics-server isn't installed/ready - usage is a nice-to-have."""
    try:
        data = custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes")
        return data.get("items", [])
    except client.ApiException:
        return []


def list_pods(core: client.CoreV1Api) -> list[dict]:
    return core.list_pod_for_all_namespaces().to_dict()["items"]


def list_recent_warning_events(core: client.CoreV1Api, lookback_minutes: int = 30) -> list[dict]:
    """Warning-type Events from the last `lookback_minutes` - the direct
    source for things with no corresponding pod/node status field
    (FailedScheduling, FailedMount, FailedAttachVolume, ...)."""
    events = core.list_event_for_all_namespaces(field_selector="type=Warning").to_dict()["items"]
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    recent = []
    for e in events:
        last_seen = e.get("last_timestamp") or e.get("event_time") or e.get("metadata", {}).get("creation_timestamp")
        if last_seen is not None and last_seen >= cutoff:
            recent.append(e)
    return recent
