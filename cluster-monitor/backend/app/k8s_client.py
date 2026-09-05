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

CoreV1Api + CustomObjectsApi (metrics.k8s.io) cover Phase 2 (pod/node/
event issue detection) and Storage Analysis. AppsV1Api/PolicyV1Api/
NetworkingV1Api/AutoscalingV2Api were added for Best Practices & Security
(Top 5 priority #4) - Deployment/StatefulSet replica counts and pod
template labels, PodDisruptionBudget/HorizontalPodAutoscaler coverage
matching, and NetworkPolicy presence. AppsV1Api's fuller use (rollout
status, ReplicaSet history) still belongs to a later "Deployment Health"
phase - only .spec.replicas and .spec.template.metadata.labels are read
so far.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from kubernetes import client, config


def build_api_clients() -> tuple[
    client.CoreV1Api, client.CustomObjectsApi, client.AppsV1Api,
    client.PolicyV1Api, client.NetworkingV1Api, client.AutoscalingV2Api,
]:
    override_url = os.environ.get("K8S_API_URL")
    if override_url:
        cfg = client.Configuration()
        cfg.host = override_url
        api_client = client.ApiClient(configuration=cfg)
    else:
        config.load_incluster_config()
        api_client = client.ApiClient()

    return (
        client.CoreV1Api(api_client),
        client.CustomObjectsApi(api_client),
        client.AppsV1Api(api_client),
        client.PolicyV1Api(api_client),
        client.NetworkingV1Api(api_client),
        client.AutoscalingV2Api(api_client),
    )


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


def list_persistentvolumeclaims(core: client.CoreV1Api) -> list[dict]:
    return core.list_persistent_volume_claim_for_all_namespaces().to_dict()["items"]


def list_persistentvolumes(core: client.CoreV1Api) -> list[dict]:
    return core.list_persistent_volume().to_dict()["items"]


def get_node_stats_summary(core: client.CoreV1Api, node_name: str) -> dict | None:
    """Raw parsed JSON from the kubelet's own stats/summary endpoint -
    proxied through the API server (nodes/proxy RBAC, this app never
    talks to a node directly). None (not an exception) if this node's
    proxy is unreachable, RBAC hasn't propagated yet, or the response
    can't be parsed - every caller treats this data as a nice-to-have
    layered on top of the actual API objects' own status, not a hard
    dependency for this app to run.

    Shared by _volume_stats_from_summary (Storage Analysis) and
    node_resource_stats_from_summary (Trend & Prediction Analysis) so
    each node's kubelet is hit once per detection cycle, not twice, for
    data that comes from the exact same endpoint either way.

    _preload_content=False is load-bearing, not cosmetic: the generated
    client has no declared response schema for an arbitrary proxy path,
    so its default deserialization path parses the JSON body and then
    re-stringifies it with Python's str() - producing a single-quoted
    dict repr, not valid JSON, which silently breaks json.loads on
    anything but trivial payloads. Passing _preload_content=False skips
    that and hands back the raw urllib3 response so .data can be
    json.loads'd directly. Confirmed the hard way against a real kubelet:
    without this, every call here raised
    `json.decoder.JSONDecodeError: Expecting property name enclosed in
    double quotes` on the very first real response."""
    try:
        response = core.connect_get_node_proxy_with_path(node_name, "stats/summary", _preload_content=False)
        return json.loads(response.data)
    except (client.ApiException, ValueError):
        return None


def _volume_stats_from_summary(data: dict) -> list[dict]:
    """Per-PVC used/capacity/available bytes for every PVC-backed volume
    currently mounted on this node. This is the *only* place real PVC
    usage lives: PersistentVolumeClaim/PersistentVolume objects only ever
    carry requested/bound capacity, never how much of it is used."""
    stats = []
    for pod in data.get("pods", []) or []:
        pod_ref = pod.get("podRef", {})
        for vol in pod.get("volume", []) or []:
            pvc_ref = vol.get("pvcRef")
            if not pvc_ref:
                continue  # emptyDir/configMap/... - not a PVC, nothing to track
            stats.append({
                "namespace": pvc_ref.get("namespace"),
                "pvc_name": pvc_ref.get("name"),
                "pod_namespace": pod_ref.get("namespace"),
                "pod_name": pod_ref.get("name"),
                "used_bytes": vol.get("usedBytes"),
                "capacity_bytes": vol.get("capacityBytes"),
                "available_bytes": vol.get("availableBytes"),
            })
    return stats


def _node_resource_stats_from_summary(node_name: str, data: dict, allocatable: dict) -> dict | None:
    """Node-level CPU/memory/disk usage for Trend & Prediction Analysis -
    the same stats/summary payload's top-level "node" block (distinct
    from the per-pod "pods" list _volume_stats_from_summary reads).
    allocatable: {"cpu_millicores":, "memory_bytes":} from the live Node
    object (aggregate.py-style parsing), so a usage sample always
    carries the capacity it should eventually be compared against."""
    node_block = data.get("node") or {}
    cpu, memory, fs = node_block.get("cpu") or {}, node_block.get("memory") or {}, node_block.get("fs") or {}
    if not cpu or not memory:
        return None
    return {
        "node_name": node_name,
        "cpu_used_millicores": (cpu.get("usageNanoCores") or 0) / 1_000_000,
        "cpu_allocatable_millicores": allocatable["cpu_millicores"],
        "memory_used_bytes": memory.get("workingSetBytes") or memory.get("usageBytes") or 0,
        "memory_allocatable_bytes": allocatable["memory_bytes"],
        "disk_used_bytes": fs.get("usedBytes"),
        "disk_capacity_bytes": fs.get("capacityBytes"),
    }


def list_all_node_stats(
    core: client.CoreV1Api, nodes: list[dict], allocatable_by_node: dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    """(volume_stats, node_resource_stats) - one stats/summary call per
    node, shared between Storage Analysis (per-PVC usage) and Trend &
    Prediction Analysis (node-level CPU/memory/disk usage), so a node
    whose proxy is unreachable simply contributes nothing to either
    rather than being fetched twice and failing twice.
    allocatable_by_node: {node_name: {"cpu_millicores":, "memory_bytes":}}."""
    volume_stats, node_resource_stats = [], []
    for node in nodes:
        name = node["metadata"]["name"]
        data = get_node_stats_summary(core, name)
        if data is None:
            continue
        volume_stats.extend(_volume_stats_from_summary(data))
        allocatable = allocatable_by_node.get(name)
        if allocatable:
            info = _node_resource_stats_from_summary(name, data, allocatable)
            if info:
                node_resource_stats.append(info)
    return volume_stats, node_resource_stats


def list_deployments(apps: client.AppsV1Api) -> list[dict]:
    return apps.list_deployment_for_all_namespaces().to_dict()["items"]


def list_statefulsets(apps: client.AppsV1Api) -> list[dict]:
    return apps.list_stateful_set_for_all_namespaces().to_dict()["items"]


def list_poddisruptionbudgets(policy: client.PolicyV1Api) -> list[dict]:
    return policy.list_pod_disruption_budget_for_all_namespaces().to_dict()["items"]


def list_networkpolicies(networking: client.NetworkingV1Api) -> list[dict]:
    return networking.list_network_policy_for_all_namespaces().to_dict()["items"]


def list_hpas(autoscaling: client.AutoscalingV2Api) -> list[dict]:
    return autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces().to_dict()["items"]


def list_services(core: client.CoreV1Api) -> list[dict]:
    return core.list_service_for_all_namespaces().to_dict()["items"]


def list_endpoints(core: client.CoreV1Api) -> list[dict]:
    return core.list_endpoints_for_all_namespaces().to_dict()["items"]


def list_ingresses(networking: client.NetworkingV1Api) -> list[dict]:
    return networking.list_ingress_for_all_namespaces().to_dict()["items"]


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
