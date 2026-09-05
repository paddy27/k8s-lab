"""Root Cause Analysis / Incident Analysis (Top 5 priority #5, final).

The plan doc's example shows a timeline plus a root-cause probability
breakdown (e.g. "Database Connectivity 70%"). Fabricating a confidence
number with no real computation behind it would be dishonest - every
other analysis module in this project ties its numbers to something
real (VPA's own model, an actual linear fit, a literal RBAC-scoped
count). This module does the same: each category's "probability" is
just its share of a small, fixed set of *real* correlating evidence
(other issues on the same pod, the node it ran on, a nearby Deployment/
StatefulSet rollout) - normalized to add up to 100 so it reads the same
way the plan doc's example does, but every percentage point traces back
to a specific, inspectable piece of evidence in the same response, and
there is an explicit "not enough signal" case rather than ever forcing
a distribution out of nothing.

Pure functions over plain dicts, same philosophy as detector.py/
best_practices.py/storage.py - the DB queries and pod/workload lookups
live in main.py's endpoint handler, this module just scores and
explains what it's given.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# What counts as an "incident" worth root-causing: an actual pod-level
# failure, not just any severity="critical" issue - PrivilegedContainer
# and DangerousCapabilities (Best Practices & Security) are also
# critical, but they're static security posture facts about
# by-design-privileged infra (calico-node, kube-proxy), not something
# actively failing. Conflating the two would surface every CNI pod as
# an "incident" on a perfectly healthy cluster - confirmed the hard way
# on this lab's own first live run of /api/incidents.
INCIDENT_TRIGGER_RULES = {"CrashLoopBackOff", "OOMKilled", "ImagePullBackOff"}

# A rollout only counts as correlating evidence if it happened shortly
# before the incident started - a rollout *after* the incident began
# can't have caused it, and one from days ago is unrelated.
ROLLOUT_CORRELATION_WINDOW = timedelta(hours=1)

_CONNECTIVITY_MARKERS = ("connection refused", "dial tcp", "i/o timeout", "no route to host", "timed out")

_RECOMMENDED_ACTIONS = {
    "Resource Limits (Memory)": (
        "Check actual memory usage against the container's limit (`kubectl top pod`) and raise the "
        "memory limit/request if it's genuinely undersized - see cluster-stats' Resource Optimization "
        "report for a concrete suggested value."
    ),
    "Image/Registry Issue": (
        "Verify the image reference and tag actually exist in the registry, and that image pull "
        "credentials/network access to the registry are working."
    ),
    "Network/Dependency Connectivity": (
        "Check connectivity from this pod to whatever it depends on (`kubectl exec ... -- curl/nc`) "
        "and confirm the dependency itself is healthy."
    ),
    "Recent Deployment Rollout": (
        "Review what changed in the most recent rollout (`kubectl rollout history` / `kubectl describe`) "
        "- this is the most likely trigger given the timing."
    ),
    "Node/Infrastructure": (
        "Check the node's health directly (`kubectl describe node`) - this pod's problem may be a "
        "symptom of a node-level issue, not the application itself."
    ),
    "Application Error": (
        "No infrastructure-level cause correlates - check the container's own logs "
        "(`kubectl logs ... --previous`) for an application-level error."
    ),
}


def workload_for_pod(pod: dict, deployments: list[dict], statefulsets: list[dict]) -> tuple[str, dict] | None:
    """Matches by each workload's own `spec.selector` against the pod's
    labels - the same mechanism Kubernetes itself uses to decide which
    pods belong to a Deployment/StatefulSet, deliberately not an
    owner-reference chain walk (a Deployment doesn't even directly own
    its pods - a ReplicaSet does), consistent with this project's
    existing preference for avoiding fragile owner matching."""
    pod_labels = pod.get("metadata", {}).get("labels") or {}
    namespace = pod["metadata"]["namespace"]
    for kind, objs in (("Deployment", deployments), ("StatefulSet", statefulsets)):
        for obj in objs:
            if obj["metadata"]["namespace"] != namespace:
                continue
            selector = (obj.get("spec", {}).get("selector") or {}).get("match_labels") or {}
            if selector and all(pod_labels.get(k) == v for k, v in selector.items()):
                return kind, obj
    return None


def _rollout_info(workload_obj: dict) -> dict | None:
    """{"updated_at": datetime, "reason": str} from the workload's own
    Progressing condition, or None if it has never reported one."""
    for cond in workload_obj.get("status", {}).get("conditions") or []:
        if cond.get("type") == "Progressing":
            updated_at = cond.get("last_update_time")
            if updated_at is None:
                return None
            return {"updated_at": updated_at, "reason": cond.get("reason")}
    return None


def _earliest(issues: list[dict]) -> datetime | None:
    timestamps = [i["first_seen"] for i in issues if i.get("first_seen")]
    return min(timestamps) if timestamps else None


def _score_categories(
    related_issues: list[dict],
    node_issues: list[dict],
    workload_kind: str | None,
    rollout: dict | None,
    incident_started_at: datetime | None,
) -> dict[str, tuple[int, list[str]]]:
    """{category: (weight, [evidence strings])} - every weight traces to
    one of these explicit, real checks. No ML, no learned weights - a
    fixed, inspectable rubric, same spirit as _sizing_recommendation in
    cluster-stats or _scheduling_failure_causes in detector.py."""
    scores: dict[str, tuple[int, list[str]]] = {}

    def add(category: str, weight: int, evidence: str) -> None:
        w, ev = scores.get(category, (0, []))
        scores[category] = (w + weight, ev + [evidence])

    rules_present = {i["rule"] for i in related_issues}
    node_rules_present = {i["rule"] for i in node_issues}
    messages = " ".join(i.get("message", "") for i in related_issues).lower()

    if "OOMKilled" in rules_present:
        add("Resource Limits (Memory)", 3, "OOMKilled observed on this pod")
    if "HighMemoryUsage" in node_rules_present:
        add("Resource Limits (Memory)", 1, "the node this pod ran on was under high memory usage")

    if "ImagePullBackOff" in rules_present:
        add("Image/Registry Issue", 3, "ImagePullBackOff observed on this pod")

    if any(marker in messages for marker in _CONNECTIVITY_MARKERS):
        add("Network/Dependency Connectivity", 3, "a probe/event message mentions a connection failure")

    if rollout and incident_started_at is not None:
        updated_at = rollout["updated_at"]
        if isinstance(updated_at, datetime) and timedelta(0) <= (incident_started_at - updated_at) <= ROLLOUT_CORRELATION_WINDOW:
            add(
                "Recent Deployment Rollout", 2,
                f"the owning {workload_kind} rolled out shortly before this incident started "
                f"({rollout['reason'] or 'Progressing'})",
            )

    if "NodeNotReady" in node_rules_present:
        add("Node/Infrastructure", 3, "the node this pod ran on was NotReady around the same time")
    if "DiskPressure" in node_rules_present:
        add("Node/Infrastructure", 2, "the node this pod ran on was under disk pressure")

    if "CrashLoopBackOff" in rules_present and not scores:
        add(
            "Application Error", 1,
            "container is crash-looping with no other correlating signal found - "
            "likely an application-level bug or bad config, check logs directly",
        )

    return scores


def _build_timeline(related_issues: list[dict], node_issues: list[dict], workload_kind: str | None, rollout: dict | None) -> list[dict]:
    """related_issues/node_issues carry first_seen as real datetime
    objects (not yet serialized) - needed for the sort below and for
    _score_categories' date arithmetic. Converted to ISO strings only
    in the returned entries."""
    entries = []
    if rollout and isinstance(rollout.get("updated_at"), datetime):
        entries.append({
            "time": rollout["updated_at"],
            "label": f"{workload_kind} rollout: {rollout['reason'] or 'Progressing'}",
        })
    for issue in related_issues:
        if issue.get("first_seen"):
            entries.append({"time": issue["first_seen"], "label": f"{issue['rule']}: {issue['message']}"})
    for issue in node_issues:
        if issue.get("first_seen"):
            entries.append({"time": issue["first_seen"], "label": f"(node) {issue['rule']}: {issue['message']}"})
    entries.sort(key=lambda e: e["time"])
    return [{"time": e["time"].isoformat(), "label": e["label"]} for e in entries]


def analyze_incident(
    namespace: str,
    pod_name: str,
    related_issues: list[dict],
    node_issues: list[dict],
    workload: tuple[str, dict] | None,
) -> dict:
    """related_issues/node_issues: serialized Issue rows (main.py's
    _serialize_issue output) - all issues ever recorded for this pod,
    and for the node it ran on, respectively. workload:
    workload_for_pod's output, or None if no Deployment/StatefulSet
    selector matched this pod."""
    workload_kind = workload[0] if workload else None
    rollout = _rollout_info(workload[1]) if workload else None
    incident_started_at = _earliest(related_issues)

    scores = _score_categories(related_issues, node_issues, workload_kind, rollout, incident_started_at)
    total_weight = sum(w for w, _ in scores.values())

    if total_weight == 0:
        root_causes = []
        recommended_action = (
            "No correlating signal found yet - check the container's own logs directly "
            "(`kubectl logs ... --previous`)."
        )
    else:
        root_causes = sorted(
            (
                {"category": category, "probability_pct": round(weight / total_weight * 100, 1), "evidence": evidence}
                for category, (weight, evidence) in scores.items()
            ),
            key=lambda r: -r["probability_pct"],
        )
        recommended_action = _RECOMMENDED_ACTIONS.get(
            root_causes[0]["category"],
            "Investigate the top-scored category's evidence below.",
        )

    return {
        "namespace": namespace,
        "pod_name": pod_name,
        "timeline": _build_timeline(related_issues, node_issues, workload_kind, rollout),
        "root_causes": root_causes,
        "recommended_action": recommended_action,
    }
