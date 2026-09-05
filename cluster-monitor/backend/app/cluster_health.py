"""Cluster-Level Analysis - a top-level health rollup over data every
other module in this app already collects. No new K8s objects needed:

- Node readiness/capacity comes from `list_nodes` (already fetched
  every detection cycle).
- Control-plane component health comes from the same `pods` list
  detector.py already scans - kubeadm's control-plane components
  (etcd, kube-apiserver, kube-scheduler, kube-controller-manager) run
  as regular Pods in kube-system, just kubelet-managed static ones (see
  detector.py's PodDistributionImbalance note on why they carry an
  ownerReference of kind "Node" rather than a workload controller -
  same pods, different angle here).
- Resource saturation comes from the exact same per-node CPU/memory
  usage Trend & Prediction Analysis already samples every cycle
  (predictions.py / node_usage_samples) - this module just reads the
  current cycle's numbers, no new fetch.
- Issue counts come from the same Postgres table every other
  "how bad is this" figure in this app already comes from.

The health score is a plain, fixed weighted deduction - not a
statistically validated model, not a fabricated confidence number.
Every point lost is named in the same response (`deductions`), same
"no black box" principle as root_cause.py's evidence-weighted root
causes. On a cluster with a fully managed control plane (EKS, GKE, ...)
where these pods don't exist at all, control_plane_components is
simply empty and contributes nothing to the score - this check only
applies where kubeadm-style static pods actually exist.
"""
from __future__ import annotations

from app.detector import parse_cpu_millicores, parse_memory_bytes

_CONTROL_PLANE_COMPONENTS = {
    "etcd": "etcd",
    "kube-apiserver": "API Server",
    "kube-scheduler": "Scheduler",
    "kube-controller-manager": "Controller Manager",
}

SATURATION_CRITICAL_PCT = 90.0

CRITICAL_ISSUE_PENALTY = 8
WARNING_ISSUE_PENALTY = 2
MAX_ISSUE_PENALTY = 50  # a cluster with hundreds of minor warnings shouldn't bottom out purely on count
NODE_NOT_READY_PENALTY = 20
CONTROL_PLANE_UNHEALTHY_PENALTY = 15
SATURATION_PENALTY = 10


def _node_ready(node: dict) -> bool:
    conditions = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
    return conditions.get("Ready") == "True"


def control_plane_pod_health(pods: list[dict]) -> list[dict]:
    """One entry per control-plane static pod found in kube-system,
    matched by the well-known kubeadm naming convention
    (<component>-<node-name>)."""
    result = []
    for pod in pods:
        if pod["metadata"]["namespace"] != "kube-system":
            continue
        name = pod["metadata"]["name"]
        for prefix, label in _CONTROL_PLANE_COMPONENTS.items():
            if name.startswith(prefix + "-"):
                statuses = pod.get("status", {}).get("container_statuses") or []
                ready = bool(statuses) and all(cs.get("ready") for cs in statuses)
                restarts = sum(cs.get("restart_count", 0) for cs in statuses)
                result.append({
                    "component": label,
                    "pod_name": name,
                    "node": pod.get("spec", {}).get("node_name"),
                    "ready": ready,
                    "restarts": restarts,
                })
                break
    return result


def compute_cluster_capacity(nodes: list[dict]) -> dict:
    cpu_allocatable = sum(parse_cpu_millicores(n["status"]["allocatable"].get("cpu")) for n in nodes)
    memory_allocatable = sum(parse_memory_bytes(n["status"]["allocatable"].get("memory")) for n in nodes)
    return {
        "node_count": len(nodes),
        "nodes_ready": sum(1 for n in nodes if _node_ready(n)),
        "cpu_allocatable_millicores": cpu_allocatable,
        "memory_allocatable_bytes": memory_allocatable,
    }


def compute_resource_saturation(nodes: list[dict], node_resource_stats: list[dict]) -> dict:
    """node_resource_stats: k8s_client.list_all_node_stats' second
    return value from the current cycle - not stored history, this is
    "how saturated is the cluster right now", not a trend (that's
    predictions.py's job)."""
    stats_by_node = {s["node_name"]: s for s in node_resource_stats}
    cpu_allocatable = sum(parse_cpu_millicores(n["status"]["allocatable"].get("cpu")) for n in nodes)
    memory_allocatable = sum(parse_memory_bytes(n["status"]["allocatable"].get("memory")) for n in nodes)
    cpu_used = sum(stats_by_node.get(n["metadata"]["name"], {}).get("cpu_used_millicores", 0) for n in nodes)
    memory_used = sum(stats_by_node.get(n["metadata"]["name"], {}).get("memory_used_bytes", 0) for n in nodes)
    return {
        "cpu_used_pct": round(cpu_used / cpu_allocatable * 100, 1) if cpu_allocatable else None,
        "memory_used_pct": round(memory_used / memory_allocatable * 100, 1) if memory_allocatable else None,
    }


def compute_health_score(
    nodes: list[dict],
    control_plane_health: list[dict],
    saturation: dict,
    active_critical_count: int,
    active_warning_count: int,
) -> dict:
    """{"score": 0-100, "deductions": [{"reason":, "points":}, ...]} -
    every point lost traces to one of these five checks, nothing else."""
    deductions = []

    not_ready = [n for n in nodes if not _node_ready(n)]
    if not_ready:
        points = min(len(not_ready) * NODE_NOT_READY_PENALTY, 100)
        deductions.append({"reason": f"{len(not_ready)} node(s) not Ready", "points": points})

    unhealthy = [c for c in control_plane_health if not c["ready"]]
    if unhealthy:
        points = min(len(unhealthy) * CONTROL_PLANE_UNHEALTHY_PENALTY, 100)
        names = ", ".join(c["component"] for c in unhealthy)
        deductions.append({"reason": f"{len(unhealthy)} control-plane component(s) not Ready ({names})", "points": points})

    if active_critical_count or active_warning_count:
        points = min(
            active_critical_count * CRITICAL_ISSUE_PENALTY + active_warning_count * WARNING_ISSUE_PENALTY,
            MAX_ISSUE_PENALTY,
        )
        deductions.append({
            "reason": f"{active_critical_count} critical + {active_warning_count} warning active issue(s)",
            "points": points,
        })

    cpu_pct = saturation.get("cpu_used_pct")
    if cpu_pct is not None and cpu_pct >= SATURATION_CRITICAL_PCT:
        deductions.append({"reason": f"cluster-wide CPU at {cpu_pct}% of allocatable", "points": SATURATION_PENALTY})

    mem_pct = saturation.get("memory_used_pct")
    if mem_pct is not None and mem_pct >= SATURATION_CRITICAL_PCT:
        deductions.append({"reason": f"cluster-wide memory at {mem_pct}% of allocatable", "points": SATURATION_PENALTY})

    score = max(0, 100 - sum(d["points"] for d in deductions))
    return {"score": score, "deductions": deductions}
