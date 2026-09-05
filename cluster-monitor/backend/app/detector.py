"""Phase 2 issue detection: turns raw pod/node/event state into a flat
list of {fingerprint, rule, severity, namespace, resource_kind,
resource_name, message} dicts.

Pure functions over plain dicts (as returned by k8s_client's
`.to_dict()` calls) - no cluster access here, so every rule is testable
with a plain fixture dict.

Covers the plan doc's Phase 2 list: CrashLoopBackOff, ImagePullBackOff,
OOMKilled, high CPU/memory, NodeNotReady, disk pressure, pending pods -
plus FrequentRestarts and an event-driven catch-all for things with no
corresponding status field (FailedScheduling, FailedMount, ...).

Also covers Scheduling Analysis (Top 5 priority #2): root-causing *why*
a pod is Pending (insufficient CPU/memory, taints, node/pod affinity -
see _SCHEDULING_FAILURE_CAUSES) and detecting Pod Distribution Imbalance
(a workload's replicas all landing on one node).
"""
from __future__ import annotations

_MEM_UNITS = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5, "Ei": 1024**6,
    "k": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5, "E": 1000**6,
}

# Below this, a node's CPU/memory usage isn't worth flagging.
HIGH_USAGE_THRESHOLD_PCT = 85.0
FREQUENT_RESTART_THRESHOLD = 5

# Event reasons that indicate a real, actionable failure vs. routine
# noise (e.g. "Pulling", "Created", "Started" are Warning-free anyway,
# but plenty of Warning events are benign retries). Anything not in
# this set is still surfaced, just at "warning" instead of "critical".
CRITICAL_EVENT_REASONS = {
    "FailedScheduling", "FailedMount", "FailedAttachVolume", "FailedCreatePodSandBox",
    "NodeNotReady", "OOMKilling", "Evicted",
}


def parse_cpu_millicores(value: str | None) -> float:
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
    if not value:
        return 0
    for suffix in sorted(_MEM_UNITS, key=len, reverse=True):
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * _MEM_UNITS[suffix])
    return int(float(value))


def _fingerprint(rule: str, namespace: str | None, kind: str, name: str) -> str:
    return f"{rule}:{namespace or '-'}:{kind}:{name}"


def _issue(rule: str, severity: str, namespace: str | None, kind: str, name: str, message: str) -> dict:
    return {
        "fingerprint": _fingerprint(rule, namespace, kind, name),
        "rule": rule,
        "severity": severity,
        "namespace": namespace,
        "resource_kind": kind,
        "resource_name": name,
        "message": message,
    }


# Scheduling Analysis: the scheduler's own PodScheduled=False condition
# message already names the actual reason a Pending pod can't be placed
# (e.g. "0/2 nodes are available: 1 Insufficient cpu, 1 Insufficient
# memory.") - no separate inference needed, just pattern-match the
# substrings it's known to use. A single message can name more than one
# cause at once, so this returns every match, not just the first.
_SCHEDULING_FAILURE_CAUSES = [
    # (rule, severity, substrings to match against the lowercased message)
    ("InsufficientCPU", "critical", ("insufficient cpu",)),
    ("InsufficientMemory", "critical", ("insufficient memory",)),
    ("PodAntiAffinity", "warning", ("anti-affinity",)),
    ("NodeAffinity", "warning", ("node affinity", "node selector")),
    ("PodAffinity", "warning", ("match pod affinity",)),
    ("TaintsAndTolerations", "warning", ("taint",)),
]


def _pod_scheduled_failure_message(pod: dict) -> str | None:
    for c in pod.get("status", {}).get("conditions") or []:
        if c.get("type") == "PodScheduled" and c.get("status") == "False":
            return c.get("message") or c.get("reason")
    return None


def _scheduling_failure_causes(message: str) -> list[tuple[str, str]]:
    lowered = message.lower()
    return [(rule, severity) for rule, severity, needles in _SCHEDULING_FAILURE_CAUSES if any(n in lowered for n in needles)]


def detect_pod_issues(pods: list[dict]) -> list[dict]:
    issues = []
    for pod in pods:
        namespace = pod["metadata"]["namespace"]
        name = pod["metadata"]["name"]
        phase = pod.get("status", {}).get("phase")

        if phase == "Pending":
            issues.append(_issue(
                "PendingPod", "warning", namespace, "Pod", name,
                "Pod has been stuck in Pending - check for scheduling constraints or resource shortages.",
            ))
            # Layer on the scheduler's actual reason, if it's told us one -
            # each distinct cause gets its own rule/fingerprint so it can
            # be tracked (and resolve) independently of the generic
            # PendingPod issue above.
            scheduled_message = _pod_scheduled_failure_message(pod)
            if scheduled_message:
                for rule, severity in _scheduling_failure_causes(scheduled_message):
                    issues.append(_issue(rule, severity, namespace, "Pod", name, scheduled_message))

        for cs in pod.get("status", {}).get("container_statuses") or []:
            container = cs.get("name")
            restart_count = cs.get("restart_count", 0)

            waiting = (cs.get("state") or {}).get("waiting") or {}
            reason = waiting.get("reason")
            if reason == "CrashLoopBackOff":
                issues.append(_issue(
                    "CrashLoopBackOff", "critical", namespace, "Pod", name,
                    f"Container '{container}' is crash-looping ({restart_count} restarts). "
                    f"Check `kubectl logs {name} -n {namespace} -c {container} --previous`.",
                ))
            elif reason in ("ImagePullBackOff", "ErrImagePull"):
                issues.append(_issue(
                    "ImagePullBackOff", "critical", namespace, "Pod", name,
                    f"Container '{container}' can't pull its image ({waiting.get('message', reason)}).",
                ))

            last_terminated = (cs.get("last_state") or {}).get("terminated") or {}
            if last_terminated.get("reason") == "OOMKilled":
                issues.append(_issue(
                    "OOMKilled", "critical", namespace, "Pod", name,
                    f"Container '{container}' was OOMKilled - consider raising its memory limit.",
                ))

            if restart_count >= FREQUENT_RESTART_THRESHOLD:
                issues.append(_issue(
                    "FrequentRestarts", "warning", namespace, "Pod", name,
                    f"Container '{container}' has restarted {restart_count} times.",
                ))

    return issues


def detect_node_issues(nodes: list[dict], node_metrics: list[dict]) -> list[dict]:
    issues = []
    metrics_by_name = {m["metadata"]["name"]: m for m in node_metrics}

    for node in nodes:
        name = node["metadata"]["name"]
        conditions = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}

        if conditions.get("Ready") != "True":
            issues.append(_issue(
                "NodeNotReady", "critical", None, "Node", name,
                "Node is not Ready - check kubelet status and node connectivity.",
            ))
        if conditions.get("DiskPressure") == "True":
            issues.append(_issue(
                "DiskPressure", "critical", None, "Node", name,
                "Node is under disk pressure - images/pods may be evicted.",
            ))
        if conditions.get("MemoryPressure") == "True":
            issues.append(_issue(
                "MemoryPressure", "critical", None, "Node", name,
                "Node is under memory pressure - pods may be evicted.",
            ))

        metrics = metrics_by_name.get(name)
        allocatable = node.get("status", {}).get("allocatable", {})
        if metrics:
            usage = metrics.get("usage", {})
            cpu_alloc = parse_cpu_millicores(allocatable.get("cpu"))
            mem_alloc = parse_memory_bytes(allocatable.get("memory"))
            cpu_used = parse_cpu_millicores(usage.get("cpu"))
            mem_used = parse_memory_bytes(usage.get("memory"))

            if cpu_alloc and (cpu_used / cpu_alloc * 100) >= HIGH_USAGE_THRESHOLD_PCT:
                pct = cpu_used / cpu_alloc * 100
                issues.append(_issue(
                    "HighCPUUsage", "warning", None, "Node", name,
                    f"CPU usage at {pct:.0f}% of allocatable.",
                ))
            if mem_alloc and (mem_used / mem_alloc * 100) >= HIGH_USAGE_THRESHOLD_PCT:
                pct = mem_used / mem_alloc * 100
                issues.append(_issue(
                    "HighMemoryUsage", "warning", None, "Node", name,
                    f"Memory usage at {pct:.0f}% of allocatable.",
                ))

    return issues


def detect_event_issues(events: list[dict]) -> list[dict]:
    """One issue per distinct (involved object, reason) - Events repeat
    every time the underlying condition recurs, so this collapses a
    flood of identical events into a single issue with a count."""
    grouped: dict[tuple, dict] = {}
    for e in events:
        involved = e.get("involved_object") or {}
        kind = involved.get("kind", "Unknown")
        name = involved.get("name", "unknown")
        namespace = involved.get("namespace")
        reason = e.get("reason", "Unknown")
        key = (namespace, kind, name, reason)

        if key not in grouped:
            severity = "critical" if reason in CRITICAL_EVENT_REASONS else "warning"
            grouped[key] = {
                **_issue(f"Event:{reason}", severity, namespace, kind, name, e.get("message", reason)),
                "_count": 0,
            }
        grouped[key]["_count"] += 1

    issues = []
    for issue in grouped.values():
        count = issue.pop("_count")
        if count > 1:
            issue["message"] = f"{issue['message']} (x{count})"
        issues.append(issue)
    return issues


# Only worth asking "is this spread out?" once there are enough replicas
# for "spread out" to mean anything - 1-2 replicas landing on the same
# node isn't a distribution problem, it's just what 1-2 replicas look like.
MIN_REPLICAS_FOR_DISTRIBUTION_CHECK = 3


def _pod_controller_owner(pod: dict) -> tuple[str, str] | None:
    """None for a static/mirror pod (kubelet-managed control-plane
    components like etcd/kube-apiserver/kube-scheduler) - those carry an
    ownerReference of kind "Node", which is real but not a workload:
    every static pod on the same node shares that identical owner even
    though they're unrelated single-instance components, and a static
    pod's placement isn't a scheduler decision to begin with, so
    "imbalance" doesn't apply to it. Confirmed the hard way: this exact
    check is why the very first pass of this detector flagged etcd,
    kube-apiserver, kube-scheduler, and kube-controller-manager together
    as "4 replicas" all "imbalanced" onto k8s-master - they're not
    replicas of anything, they're 4 different single-instance pods that
    happen to share a Node owner."""
    for ref in pod.get("metadata", {}).get("owner_references") or []:
        if ref.get("controller") and ref.get("kind") != "Node":
            return ref.get("kind"), ref.get("name")
    return None


def detect_scheduling_distribution_issues(pods: list[dict], nodes: list[dict]) -> list[dict]:
    """"Pod Distribution Imbalance": every running replica of some
    workload landed on a single node. Grouped by each pod's *immediate*
    controller (a Deployment's pods are owned by a ReplicaSet, not the
    Deployment itself) - deliberately not resolved further up to the
    Deployment name, to avoid the same fragile owner-chain-walking
    cluster-stats' recommendation engine already avoids.

    Only flagged when the cluster actually has more than one Ready node -
    on a single-node cluster this isn't a misconfiguration to fix, it's
    just what one node looks like."""
    ready_nodes = {
        n["metadata"]["name"] for n in nodes
        if {c["type"]: c["status"] for c in n.get("status", {}).get("conditions", [])}.get("Ready") == "True"
    }
    if len(ready_nodes) < 2:
        return []

    groups: dict[tuple, dict] = {}
    for pod in pods:
        if pod.get("status", {}).get("phase") != "Running":
            continue
        node_name = pod.get("spec", {}).get("node_name")
        owner = _pod_controller_owner(pod)
        if not node_name or owner is None:
            continue
        key = (pod["metadata"]["namespace"], *owner)
        entry = groups.setdefault(key, {"pod_count": 0, "nodes": set()})
        entry["pod_count"] += 1
        entry["nodes"].add(node_name)

    issues = []
    for (namespace, owner_kind, owner_name), entry in groups.items():
        if entry["pod_count"] < MIN_REPLICAS_FOR_DISTRIBUTION_CHECK or len(entry["nodes"]) != 1:
            continue
        [only_node] = entry["nodes"]
        issues.append(_issue(
            "PodDistributionImbalance", "warning", namespace, owner_kind, owner_name,
            f"All {entry['pod_count']} running replicas are scheduled on the same node "
            f"({only_node}) - if that node goes down, every replica goes down with it. "
            "Consider a podAntiAffinity or topologySpreadConstraints rule.",
        ))
    return issues


def detect_all_issues(pods: list[dict], nodes: list[dict], node_metrics: list[dict], events: list[dict]) -> list[dict]:
    return [
        *detect_pod_issues(pods),
        *detect_node_issues(nodes, node_metrics),
        *detect_event_issues(events),
        *detect_scheduling_distribution_issues(pods, nodes),
    ]
