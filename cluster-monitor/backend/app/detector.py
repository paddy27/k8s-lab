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


def detect_all_issues(pods: list[dict], nodes: list[dict], node_metrics: list[dict], events: list[dict]) -> list[dict]:
    return [
        *detect_pod_issues(pods),
        *detect_node_issues(nodes, node_metrics),
        *detect_event_issues(events),
    ]
