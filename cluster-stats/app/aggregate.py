"""Turns raw Kubernetes API objects into the summaries the API/UI serve.

Kept as plain functions over dicts (not a full client SDK's typed models)
- the raw API JSON is exactly what we get from k8s_client, and every
  summary here is a pure function of it, easy to unit test in isolation.
"""
from __future__ import annotations

_MEM_UNITS = {
    # Binary (IEC) suffixes, most common in practice.
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5, "Ei": 1024**6,
    # Decimal (SI) suffixes.
    "k": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5, "E": 1000**6,
}


def parse_cpu_millicores(value: str | None) -> float:
    """'100m' -> 100.0, '1' -> 1000.0, '1.5' -> 1500.0, '500000n' -> 0.5"""
    if not value:
        return 0.0
    if value.endswith("n"):
        return float(value[:-1]) / 1_000_000
    if value.endswith("u"):
        return float(value[:-1]) / 1_000
    if value.endswith("m"):
        return float(value[:-1])
    return float(value) * 1000


def parse_memory_bytes(value: str | None) -> int:
    """'128Mi' -> 134217728, '1Gi' -> 1073741824, '500' -> 500"""
    if not value:
        return 0
    for suffix in sorted(_MEM_UNITS, key=len, reverse=True):
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * _MEM_UNITS[suffix])
    return int(float(value))


def _node_roles(node: dict) -> list[str]:
    labels = node["metadata"].get("labels", {})
    roles = [k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")]
    return roles or ["<none>"]


def _count_by(items: list, keyfn) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        k = keyfn(item)
        out[k] = out.get(k, 0) + 1
    return out


def _container_resources(containers: list[dict]) -> dict:
    cpu_req = mem_req = cpu_lim = mem_lim = 0.0
    for c in containers:
        res = c.get("resources", {})
        reqs, lims = res.get("requests", {}), res.get("limits", {})
        cpu_req += parse_cpu_millicores(reqs.get("cpu"))
        mem_req += parse_memory_bytes(reqs.get("memory"))
        cpu_lim += parse_cpu_millicores(lims.get("cpu"))
        mem_lim += parse_memory_bytes(lims.get("memory"))
    return {
        "cpu_requested_millicores": cpu_req,
        "memory_requested_bytes": int(mem_req),
        "cpu_limit_millicores": cpu_lim,
        "memory_limit_bytes": int(mem_lim),
    }


def _metrics_usage_by_key(pod_metrics: list[dict]) -> dict[tuple[str, str], tuple[float, int]]:
    usage = {}
    for pm in pod_metrics:
        key = (pm["metadata"]["namespace"], pm["metadata"]["name"])
        cpu = sum(parse_cpu_millicores(c["usage"].get("cpu")) for c in pm.get("containers", []))
        mem = sum(parse_memory_bytes(c["usage"].get("memory")) for c in pm.get("containers", []))
        usage[key] = (cpu, int(mem))
    return usage


def summarize_nodes(nodes: list[dict], node_metrics: list[dict]) -> list[dict]:
    metrics_by_name = {m["metadata"]["name"]: m for m in node_metrics}
    result = []
    for n in nodes:
        name = n["metadata"]["name"]
        capacity, allocatable = n["status"]["capacity"], n["status"]["allocatable"]
        conditions = {c["type"]: c["status"] for c in n["status"].get("conditions", [])}
        usage = metrics_by_name.get(name, {}).get("usage", {})

        cpu_alloc = parse_cpu_millicores(allocatable.get("cpu"))
        mem_alloc = parse_memory_bytes(allocatable.get("memory"))
        cpu_used = parse_cpu_millicores(usage.get("cpu"))
        mem_used = parse_memory_bytes(usage.get("memory"))
        info = n["status"]["nodeInfo"]

        result.append({
            "name": name,
            "ready": conditions.get("Ready") == "True",
            "roles": _node_roles(n),
            "internal_ip": next(
                (a["address"] for a in n["status"].get("addresses", []) if a["type"] == "InternalIP"),
                None,
            ),
            "cpu_capacity_millicores": parse_cpu_millicores(capacity.get("cpu")),
            "cpu_allocatable_millicores": cpu_alloc,
            "cpu_used_millicores": cpu_used,
            "cpu_used_pct": round(cpu_used / cpu_alloc * 100, 1) if cpu_alloc else None,
            "memory_capacity_bytes": parse_memory_bytes(capacity.get("memory")),
            "memory_allocatable_bytes": mem_alloc,
            "memory_used_bytes": mem_used,
            "memory_used_pct": round(mem_used / mem_alloc * 100, 1) if mem_alloc else None,
            "pod_capacity": int(allocatable.get("pods", 0)),
            "kubelet_version": info["kubeletVersion"],
            "os_image": info["osImage"],
            "container_runtime": info["containerRuntimeVersion"],
        })
    return result


def _count_by_namespace(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        ns = item["metadata"]["namespace"]
        counts[ns] = counts.get(ns, 0) + 1
    return counts


def summarize_namespaces(
    namespaces: list[dict],
    pods: list[dict],
    pod_metrics: list[dict],
    resource_kinds: dict[str, list[dict]] | None = None,
) -> list[dict]:
    """resource_kinds: {display name -> objects} for whatever else should
    be counted per namespace (Service, ConfigMap, Job, ...). Extensible
    without touching this function again - just pass another entry."""
    usage_by_key = _metrics_usage_by_key(pod_metrics)
    resource_kinds = resource_kinds or {}
    counts_by_kind = {kind: _count_by_namespace(items) for kind, items in resource_kinds.items()}

    by_ns = {
        ns["metadata"]["name"]: {
            "name": ns["metadata"]["name"],
            "status": ns["status"]["phase"],
            "pod_count": 0,
            "pods_by_phase": {},
            "cpu_requested_millicores": 0.0,
            "memory_requested_bytes": 0,
            "cpu_limit_millicores": 0.0,
            "memory_limit_bytes": 0,
            "cpu_used_millicores": 0.0,
            "memory_used_bytes": 0,
            "resource_counts": {
                kind: counts.get(ns["metadata"]["name"], 0) for kind, counts in counts_by_kind.items()
            },
        }
        for ns in namespaces
    }

    for p in pods:
        ns_name = p["metadata"]["namespace"]
        entry = by_ns.get(ns_name)
        if entry is None:
            continue
        entry["pod_count"] += 1
        phase = p["status"].get("phase", "Unknown")
        entry["pods_by_phase"][phase] = entry["pods_by_phase"].get(phase, 0) + 1

        res = _container_resources(p["spec"].get("containers", []))
        entry["cpu_requested_millicores"] += res["cpu_requested_millicores"]
        entry["memory_requested_bytes"] += res["memory_requested_bytes"]
        entry["cpu_limit_millicores"] += res["cpu_limit_millicores"]
        entry["memory_limit_bytes"] += res["memory_limit_bytes"]

        cpu_u, mem_u = usage_by_key.get((ns_name, p["metadata"]["name"]), (0.0, 0))
        entry["cpu_used_millicores"] += cpu_u
        entry["memory_used_bytes"] += mem_u

    return sorted(by_ns.values(), key=lambda e: e["name"])


def summarize_pods(pods: list[dict], pod_metrics: list[dict]) -> list[dict]:
    usage_by_key = _metrics_usage_by_key(pod_metrics)
    result = []
    for p in pods:
        ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
        res = _container_resources(p["spec"].get("containers", []))
        restarts = sum(cs.get("restartCount", 0) for cs in p["status"].get("containerStatuses", []))
        cpu_u, mem_u = usage_by_key.get((ns, name), (0.0, 0))
        result.append({
            "namespace": ns,
            "name": name,
            "node": p["spec"].get("nodeName"),
            "phase": p["status"].get("phase"),
            "restarts": restarts,
            "cpu_used_millicores": cpu_u,
            "memory_used_bytes": mem_u,
            **res,
        })
    return result


def _hpa_metric_summary(m: dict) -> dict:
    if m["type"] == "Resource":
        r = m["resource"]
        return {"type": "Resource", "name": r["name"], "target": r.get("target", {})}
    return {"type": m["type"]}


def _workload_entry(kind: str, obj: dict, desired: int, current: int, ready: int) -> dict:
    return {
        "kind": kind,
        "namespace": obj["metadata"]["namespace"],
        "name": obj["metadata"]["name"],
        "desired": desired,
        "current": current,
        "ready": ready,
    }


def summarize_workloads(deployments: list[dict], daemonsets: list[dict], statefulsets: list[dict]) -> list[dict]:
    """Deployments, DaemonSets, and StatefulSets in one normalized list -
    the three controller kinds that actually own pods with a resizable
    PodTemplateSpec (and so are the kinds ensure_vpas_for_workloads
    creates VPA recommendations for)."""
    result = []
    for d in deployments:
        spec, status = d["spec"], d.get("status", {})
        result.append(_workload_entry(
            "Deployment", d,
            desired=spec.get("replicas", 0),
            current=status.get("replicas", 0),
            ready=status.get("readyReplicas", 0),
        ))
    for ds in daemonsets:
        status = ds.get("status", {})
        result.append(_workload_entry(
            "DaemonSet", ds,
            desired=status.get("desiredNumberScheduled", 0),
            current=status.get("currentNumberScheduled", 0),
            ready=status.get("numberReady", 0),
        ))
    for ss in statefulsets:
        spec, status = ss["spec"], ss.get("status", {})
        result.append(_workload_entry(
            "StatefulSet", ss,
            desired=spec.get("replicas", 0),
            current=status.get("replicas", 0),
            ready=status.get("readyReplicas", 0),
        ))
    return sorted(result, key=lambda w: (w["namespace"], w["kind"], w["name"]))


def summarize_hpas(hpas: list[dict]) -> list[dict]:
    result = []
    for h in hpas:
        spec, status = h["spec"], h.get("status", {})
        result.append({
            "namespace": h["metadata"]["namespace"],
            "name": h["metadata"]["name"],
            "target_kind": spec["scaleTargetRef"]["kind"],
            "target_name": spec["scaleTargetRef"]["name"],
            "min_replicas": spec.get("minReplicas"),
            "max_replicas": spec.get("maxReplicas"),
            "current_replicas": status.get("currentReplicas"),
            "desired_replicas": status.get("desiredReplicas"),
            "metrics": [_hpa_metric_summary(m) for m in spec.get("metrics", [])],
        })
    return result


def _vpa_bound(bound: dict | None) -> dict | None:
    if not bound:
        return None
    return {
        "cpu_millicores": parse_cpu_millicores(bound.get("cpu")),
        "memory_bytes": parse_memory_bytes(bound.get("memory")),
    }


def summarize_vpas(vpas: list[dict]) -> list[dict]:
    result = []
    for v in vpas:
        spec, status = v["spec"], v.get("status", {})
        recs = status.get("recommendation", {}).get("containerRecommendations", []) or []
        result.append({
            "namespace": v["metadata"]["namespace"],
            "name": v["metadata"]["name"],
            "target_kind": spec["targetRef"]["kind"],
            "target_name": spec["targetRef"]["name"],
            "update_mode": spec.get("updatePolicy", {}).get("updateMode", "Off"),
            "auto_created": v["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") == "cluster-stats",
            "containers": [
                {
                    "container_name": c["containerName"],
                    "target": _vpa_bound(c.get("target")),
                    "lower_bound": _vpa_bound(c.get("lowerBound")),
                    "upper_bound": _vpa_bound(c.get("upperBound")),
                }
                for c in recs
            ],
        })
    return result


def summarize_cluster(nodes_summary: list[dict], ns_summary: list[dict], pods: list[dict]) -> dict:
    return {
        "node_count": len(nodes_summary),
        "nodes_ready": sum(1 for n in nodes_summary if n["ready"]),
        "namespace_count": len(ns_summary),
        "pod_count": len(pods),
        "pods_by_phase": _count_by(pods, lambda p: p["status"].get("phase", "Unknown")),
        "cpu_capacity_millicores": sum(n["cpu_capacity_millicores"] for n in nodes_summary),
        "cpu_allocatable_millicores": sum(n["cpu_allocatable_millicores"] for n in nodes_summary),
        "cpu_used_millicores": sum(n["cpu_used_millicores"] for n in nodes_summary),
        "memory_capacity_bytes": sum(n["memory_capacity_bytes"] for n in nodes_summary),
        "memory_allocatable_bytes": sum(n["memory_allocatable_bytes"] for n in nodes_summary),
        "memory_used_bytes": sum(n["memory_used_bytes"] for n in nodes_summary),
    }


def _fmt_bytes(b: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if b < 1024 or unit == "TiB":
            return f"{b:.0f}{unit}" if unit == "B" else f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}TiB"  # unreachable, keeps type-checkers happy


def _template_container_requests(workload_obj: dict) -> dict[str, dict]:
    """{container_name: {cpu_requested_millicores, memory_requested_bytes}}
    straight from the workload's pod template - Deployment, DaemonSet,
    and StatefulSet all share the same .spec.template.spec.containers
    shape, which is what a VPA recommendation is actually judged
    against (not any individual running pod)."""
    containers = workload_obj.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    result = {}
    for c in containers:
        reqs = c.get("resources", {}).get("requests", {})
        result[c["name"]] = {
            "cpu_requested_millicores": parse_cpu_millicores(reqs.get("cpu")),
            "memory_requested_bytes": parse_memory_bytes(reqs.get("memory")),
        }
    return result


def _template_container_limits(workload_obj: dict) -> dict[str, dict]:
    """Same shape as _template_container_requests, for .resources.limits."""
    containers = workload_obj.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    result = {}
    for c in containers:
        lims = c.get("resources", {}).get("limits", {})
        result[c["name"]] = {
            "cpu_limit_millicores": parse_cpu_millicores(lims.get("cpu")),
            "memory_limit_bytes": parse_memory_bytes(lims.get("memory")),
        }
    return result


def _sizing_recommendation(dimension: str, current: float, lower: float, upper: float, fmt) -> tuple[str, str] | None:
    """Returns (message, severity) if the current request is outside the
    VPA's [lower, upper] band, else None. 'severity' is "warning" for
    under-provisioned (real risk: throttling/OOM) and "info" for
    over-provisioned (waste, not a failure risk)."""
    if current < lower:
        if current == 0:
            return f"No {dimension} request set - VPA recommends at least {fmt(lower)}.", "warning"
        return (
            f"{dimension} request ({fmt(current)}) is below the VPA-recommended minimum "
            f"({fmt(lower)}) - risk of throttling/OOM under load.",
            "warning",
        )
    if current > upper:
        return (
            f"{dimension} request ({fmt(current)}) is above the VPA-recommended maximum "
            f"({fmt(upper)}) - likely over-provisioned.",
            "info",
        )
    return None


def build_recommendations(
    deployments: list[dict],
    daemonsets: list[dict],
    statefulsets: list[dict],
    vpas: list[dict],
    hpas: list[dict],
) -> list[dict]:
    """The actual "recommendation engine" piece: turns raw VPA/HPA state
    into actionable suggestions instead of just numbers in a table -
    "this container's CPU request is below what the VPA recommends" /
    "this HPA is pinned at max replicas" - each with a namespace/target
    so the UI can link back to the relevant row."""
    workloads_by_key = {
        (obj["metadata"]["namespace"], kind, obj["metadata"]["name"]): obj
        for kind, objs in (("Deployment", deployments), ("DaemonSet", daemonsets), ("StatefulSet", statefulsets))
        for obj in objs
    }

    recommendations = []

    for vpa in summarize_vpas(vpas):
        workload_obj = workloads_by_key.get((vpa["namespace"], vpa["target_kind"], vpa["target_name"]))
        if workload_obj is None:
            continue
        template_requests = _template_container_requests(workload_obj)
        for c in vpa["containers"]:
            current = template_requests.get(c["container_name"])
            lower, upper = c.get("lower_bound"), c.get("upper_bound")
            if current is None or lower is None or upper is None:
                continue

            for dimension, current_value, lower_value, upper_value, fmt in (
                ("CPU", current["cpu_requested_millicores"], lower["cpu_millicores"], upper["cpu_millicores"], lambda m: f"{m:.0f}m"),
                ("Memory", current["memory_requested_bytes"], lower["memory_bytes"], upper["memory_bytes"], _fmt_bytes),
            ):
                result = _sizing_recommendation(dimension, current_value, lower_value, upper_value, fmt)
                if result is None:
                    continue
                message, severity = result
                recommendations.append({
                    "namespace": vpa["namespace"],
                    "target_kind": vpa["target_kind"],
                    "target_name": vpa["target_name"],
                    "container": c["container_name"],
                    "type": "VPA",
                    "severity": severity,
                    "message": message,
                })

    for hpa in summarize_hpas(hpas):
        base = {
            "namespace": hpa["namespace"],
            "target_kind": hpa["target_kind"],
            "target_name": hpa["target_name"],
            "container": None,
            "type": "HPA",
        }
        if hpa["current_replicas"] is not None and hpa["current_replicas"] >= hpa["max_replicas"]:
            recommendations.append({
                **base,
                "severity": "warning",
                "message": f"At max replicas ({hpa['max_replicas']}) - if load keeps growing, raise maxReplicas.",
            })
        if hpa["min_replicas"] == hpa["max_replicas"]:
            recommendations.append({
                **base,
                "severity": "info",
                "message": f"minReplicas == maxReplicas ({hpa['min_replicas']}) - this HPA can never actually scale.",
            })

    return recommendations


# Resource Optimization ------------------------------------------------------
#
# Buckets every container that has both a template resource request and a
# VPA recommendation into over-provisioned / under-provisioned / practically
# unused, plus a full request-vs-recommended table and an estimated
# cost-savings rollup.
#
# "Recommended" here is the VPA's own `target` - the VPA recommender already
# models a container's historical usage distribution (that's its entire
# job), so reusing it avoids standing up a second, fragile usage-tracking
# pipeline (e.g. matching live pods back to an owning workload, which
# build_recommendations above deliberately avoids for the same reason).
# over/under-provisioned reuse the same lower_bound/upper_bound signal
# _sizing_recommendation already treats as ground truth elsewhere in this
# file, so a container flagged here agrees with what /api/recommendations
# would already say about it - "unused" is the one new, stricter threshold
# (10x over the target, not just outside the [lower, upper] band).
#
# Caveat worth keeping in mind: this is a recommendation based on the VPA's
# modeled usage, not a literal time-averaged metric reading - see
# cluster-stats/README.md.
_UNUSED_RATIO = 10.0

# Estimated, not billed: this lab has no cloud billing API to query, so
# "potential savings" is cores/GiB reclaimed times a configurable blended
# on-demand $/hour rate, purely to give the number a unit people intuitively
# grasp. Override via OPTIMIZATION_CPU_HOURLY_RATE_USD /
# OPTIMIZATION_MEM_HOURLY_RATE_PER_GIB_USD if a different rate better
# reflects your own environment.
DEFAULT_CPU_HOURLY_RATE_USD = 0.033
DEFAULT_MEM_HOURLY_RATE_PER_GIB_USD = 0.004
HOURS_PER_MONTH = 730


def build_resource_optimization(
    deployments: list[dict],
    daemonsets: list[dict],
    statefulsets: list[dict],
    vpas: list[dict],
    cpu_hourly_rate_usd: float = DEFAULT_CPU_HOURLY_RATE_USD,
    mem_hourly_rate_per_gib_usd: float = DEFAULT_MEM_HOURLY_RATE_PER_GIB_USD,
) -> dict:
    workloads_by_key = {
        (obj["metadata"]["namespace"], kind, obj["metadata"]["name"]): obj
        for kind, objs in (("Deployment", deployments), ("DaemonSet", daemonsets), ("StatefulSet", statefulsets))
        for obj in objs
    }

    rows = []
    for vpa in summarize_vpas(vpas):
        workload_obj = workloads_by_key.get((vpa["namespace"], vpa["target_kind"], vpa["target_name"]))
        if workload_obj is None:
            continue
        template_requests = _template_container_requests(workload_obj)
        template_limits = _template_container_limits(workload_obj)
        for c in vpa["containers"]:
            current = template_requests.get(c["container_name"])
            limits = template_limits.get(c["container_name"], {})
            target, lower, upper = c.get("target"), c.get("lower_bound"), c.get("upper_bound")
            if current is None or target is None or lower is None or upper is None:
                continue
            rows.append({
                "namespace": vpa["namespace"],
                "target_kind": vpa["target_kind"],
                "target_name": vpa["target_name"],
                "container": c["container_name"],
                "cpu_request_millicores": current["cpu_requested_millicores"],
                "cpu_recommended_millicores": target["cpu_millicores"],
                "cpu_lower_millicores": lower["cpu_millicores"],
                "cpu_upper_millicores": upper["cpu_millicores"],
                "cpu_limit_millicores": limits.get("cpu_limit_millicores", 0.0),
                "memory_request_bytes": current["memory_requested_bytes"],
                "memory_recommended_bytes": target["memory_bytes"],
                "memory_lower_bytes": lower["memory_bytes"],
                "memory_upper_bytes": upper["memory_bytes"],
                "memory_limit_bytes": limits.get("memory_limit_bytes", 0),
            })

    cpu_over_provisioned = [r for r in rows if r["cpu_request_millicores"] > r["cpu_upper_millicores"] > 0]
    memory_over_provisioned = [r for r in rows if r["memory_request_bytes"] > r["memory_upper_bytes"] > 0]
    under_provisioned = [
        r for r in rows
        if (r["cpu_lower_millicores"] > 0 and r["cpu_request_millicores"] < r["cpu_lower_millicores"])
        or (r["memory_lower_bytes"] > 0 and r["memory_request_bytes"] < r["memory_lower_bytes"])
    ]
    unused_resources = [
        r for r in rows
        if (r["cpu_recommended_millicores"] > 0 and r["cpu_request_millicores"] > r["cpu_recommended_millicores"] * _UNUSED_RATIO)
        or (r["memory_recommended_bytes"] > 0 and r["memory_request_bytes"] > r["memory_recommended_bytes"] * _UNUSED_RATIO)
    ]

    cpu_cores_saved = sum(max(0.0, r["cpu_request_millicores"] - r["cpu_recommended_millicores"]) for r in rows) / 1000
    memory_gib_saved = sum(max(0, r["memory_request_bytes"] - r["memory_recommended_bytes"]) for r in rows) / (1024 ** 3)
    estimated_monthly_cost_usd = (
        cpu_cores_saved * cpu_hourly_rate_usd * HOURS_PER_MONTH
        + memory_gib_saved * mem_hourly_rate_per_gib_usd * HOURS_PER_MONTH
    )

    return {
        "cpu_over_provisioned": cpu_over_provisioned,
        "memory_over_provisioned": memory_over_provisioned,
        "under_provisioned": under_provisioned,
        "unused_resources": unused_resources,
        "rows": rows,
        "potential_savings": {
            "cpu_cores": round(cpu_cores_saved, 2),
            "memory_gib": round(memory_gib_saved, 2),
            "estimated_monthly_cost_usd": round(estimated_monthly_cost_usd, 2),
            "cpu_hourly_rate_usd": cpu_hourly_rate_usd,
            "memory_hourly_rate_per_gib_usd": mem_hourly_rate_per_gib_usd,
        },
    }


# Cost Optimization (beyond the original Top 5) ------------------------------
#
# Idle/Underutilized Nodes, from the exact same per-node CPU/memory usage
# summarize_nodes already computes - no new data source, no new RBAC.
# Plain fixed thresholds on cpu_used_pct/memory_used_pct, not a
# statistical model.
#
# The rest of the plan doc's Cost Optimization tree is already covered
# elsewhere and deliberately not duplicated here: Over-Provisioned Pods,
# Resource Waste, and Estimated Cost Savings are this app's own
# build_resource_optimization/"potential_savings" above; Unused PVCs is
# cluster-monitor's Storage Analysis (UnusedPVC). Idle Load Balancers is
# not built anywhere - the Kubernetes API exposes no traffic/connection
# metrics for a Service at all, LoadBalancer or otherwise, idle or not;
# that would need a service mesh or the cloud provider's own metrics,
# neither of which exists in this lab.
IDLE_NODE_THRESHOLD_PCT = 10.0
UNDERUTILIZED_NODE_THRESHOLD_PCT = 30.0


def build_node_utilization_report(nodes_summary: list[dict]) -> dict:
    idle, underutilized = [], []
    for n in nodes_summary:
        cpu_pct, mem_pct = n.get("cpu_used_pct"), n.get("memory_used_pct")
        if cpu_pct is None or mem_pct is None:
            continue  # zero allocatable reported - nothing meaningful to classify
        entry = {"name": n["name"], "cpu_used_pct": cpu_pct, "memory_used_pct": mem_pct}
        if cpu_pct < IDLE_NODE_THRESHOLD_PCT and mem_pct < IDLE_NODE_THRESHOLD_PCT:
            idle.append(entry)
        elif cpu_pct < UNDERUTILIZED_NODE_THRESHOLD_PCT and mem_pct < UNDERUTILIZED_NODE_THRESHOLD_PCT:
            underutilized.append(entry)
    return {"idle_nodes": idle, "underutilized_nodes": underutilized}
