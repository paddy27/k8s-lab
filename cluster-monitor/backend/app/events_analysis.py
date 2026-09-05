"""Kubernetes Events Analysis (beyond the original Top 5 - the final
item from the original wishlist).

A dedicated, filterable view over the raw Kubernetes Event stream -
distinct from the "Issues" abstraction everything else in this app
builds. Issues are deduped, reconciled state that persists across
detection cycles (first_seen/last_seen/resolved); this is the live
event stream itself, browsable and filterable by namespace/kind/name/
reason/severity, matching the plan doc's ask for a separate events view
rather than folding everything into recommendations.

Severity classification reuses detector.CRITICAL_EVENT_REASONS - the
exact same "is this actually severe or just routine" judgment the
existing Event:<reason> issue detection already makes, so a Warning
event that reads as "critical" here agrees with what would eventually
become a critical issue if it persisted long enough to be reconciled.

Pure functions over plain dicts, same philosophy as every other
analysis module here.
"""
from __future__ import annotations

from app.detector import CRITICAL_EVENT_REASONS


def _event_severity(event: dict) -> str:
    if event.get("type") != "Warning":
        return "normal"
    return "critical" if event.get("reason") in CRITICAL_EVENT_REASONS else "warning"


def summarize_events(events: list[dict]) -> dict:
    """{"critical":, "warning":, "normal":, "top_reasons": [{"reason":, "count":}, ...]}"""
    counts = {"critical": 0, "warning": 0, "normal": 0}
    reason_counts: dict[str, int] = {}
    for e in events:
        counts[_event_severity(e)] += 1
        reason = e.get("reason") or "Unknown"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    top_reasons = sorted(reason_counts.items(), key=lambda kv: -kv[1])[:5]
    return {**counts, "top_reasons": [{"reason": r, "count": c} for r, c in top_reasons]}


def serialize_event(event: dict) -> dict:
    involved = event.get("involved_object") or {}
    last_seen = event.get("last_timestamp") or event.get("event_time") or event.get("metadata", {}).get("creation_timestamp")
    first_seen = event.get("first_timestamp") or last_seen
    return {
        "severity": _event_severity(event),
        "type": event.get("type"),
        "reason": event.get("reason"),
        "message": event.get("message"),
        "namespace": involved.get("namespace"),
        "involved_kind": involved.get("kind"),
        "involved_name": involved.get("name"),
        "count": event.get("count") or 1,
        "first_seen": first_seen.isoformat() if first_seen else None,
        "last_seen": last_seen.isoformat() if last_seen else None,
    }


def filter_events(
    events: list[dict],
    namespace: str | None = None,
    kind: str | None = None,
    name: str | None = None,
    reason: str | None = None,
    severity: str | None = None,
) -> list[dict]:
    """kind/name filter by the event's involvedObject - "Node" + a node
    name covers the wishlist's "Node" filter, any other kind + name
    covers "Pod"/"Workload" (Deployment/StatefulSet/ReplicaSet/...) the
    same way, without needing a separate code path per kind."""
    result = []
    for e in events:
        involved = e.get("involved_object") or {}
        if namespace and involved.get("namespace") != namespace:
            continue
        if kind and involved.get("kind") != kind:
            continue
        if name and involved.get("name") != name:
            continue
        if reason and e.get("reason") != reason:
            continue
        if severity and _event_severity(e) != severity:
            continue
        result.append(e)
    return result
