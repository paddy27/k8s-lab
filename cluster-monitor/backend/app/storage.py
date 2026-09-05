"""Storage / PVC analysis (Top 5 priority #3).

Two things the Kubernetes API genuinely cannot tell you about a PVC: how
full it actually is, and where that's trending. A PersistentVolumeClaim
object only ever carries *requested*/*bound* capacity, never usage - the
only place real usage lives is the kubelet's own stats/summary endpoint
(see k8s_client.list_all_volume_stats and this app's RBAC grant on
nodes/proxy). This module is the pure-function layer on top of that data,
same philosophy as detector.py: plain dicts/tuples in, plain issue dicts
out, no cluster or DB access here - db.py owns turning usage snapshots
into a stored history, this module owns making sense of that history.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.detector import _issue

PVC_ALMOST_FULL_CRITICAL_PCT = 90.0
PVC_ALMOST_FULL_WARNING_PCT = 75.0

# Only worth surfacing a prediction once it's close enough to matter -
# a PVC on track to fill up in 90 days isn't yet an actionable issue.
PVC_EXHAUSTION_WARNING_DAYS = 7.0

# A minimum sample *count* alone isn't enough of a gate - confirmed the
# hard way when this function was reused for node CPU usage
# (predictions.py): 2-3 samples 5-7 minutes apart on a nearly-idle node
# (CPU bouncing 56m -> 42m -> 87m, ordinary noise) extrapolated into a
# "0.5 days to exhaustion" false alarm on the very first live run.
# Requiring a minimum elapsed *time span* across the samples fixes this
# for every caller, not just node CPU - a short window is exactly when a
# single noisy blip dominates the fit; a longer one averages it out.
MIN_TREND_WINDOW = timedelta(hours=1)


def _fmt_bytes(b: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if b < 1024 or unit == "TiB":
            return f"{b:.0f}{unit}" if unit == "B" else f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}TiB"  # unreachable, keeps type-checkers happy


def predict_days_to_exhaustion(samples: list[tuple[datetime, int]], capacity_bytes: int) -> float | None:
    """Ordinary least-squares fit of used_bytes over time, extrapolated
    out to capacity_bytes. None when there isn't enough history yet
    (fewer than 2 samples, or samples spanning less than
    MIN_TREND_WINDOW - see its comment), usage isn't actually trending
    upward, or capacity is unknown - same "needs time to build a
    picture" caveat as the VPA recommender elsewhere in this project
    (cluster-stats), not a guess dressed up as a hard number."""
    if len(samples) < 2 or capacity_bytes <= 0:
        return None
    if samples[-1][0] - samples[0][0] < MIN_TREND_WINDOW:
        return None

    t0 = samples[0][0]
    xs = [(t - t0).total_seconds() for t, _ in samples]
    ys = [float(u) for _, u in samples]
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:  # all samples at the same instant - nothing to fit
        return None

    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom  # bytes/second
    if slope <= 0:  # flat or shrinking usage - no exhaustion to predict
        return None

    remaining = capacity_bytes - ys[-1]
    if remaining <= 0:
        return 0.0
    return (remaining / slope) / 86400


def build_storage_issues(
    pvcs: list[dict],
    pvs: list[dict],
    pods: list[dict],
    volume_stats: list[dict],
    samples_by_pvc: dict[tuple[str, str], list[tuple[datetime, int, int]]],
) -> list[dict]:
    """samples_by_pvc: {(namespace, pvc_name): [(sampled_at, used_bytes,
    capacity_bytes), ...]} ascending by time - db.load_recent_pvc_samples'
    output shape, passed straight through."""
    stats_by_pvc = {(s["namespace"], s["pvc_name"]): s for s in volume_stats if s.get("capacity_bytes")}
    mounted_claims = {
        (pod["metadata"]["namespace"], v["persistent_volume_claim"]["claim_name"])
        for pod in pods
        for v in (pod.get("spec", {}).get("volumes") or [])
        if v.get("persistent_volume_claim")
    }

    issues = []
    for pvc in pvcs:
        namespace = pvc["metadata"]["namespace"]
        name = pvc["metadata"]["name"]
        phase = pvc.get("status", {}).get("phase")

        if phase == "Pending":
            issues.append(_issue(
                "FailedPVCBinding", "critical", namespace, "PersistentVolumeClaim", name,
                "Stuck Pending - check the StorageClass/provisioner and available capacity.",
            ))
            continue  # unbound - no usage to report or predict yet

        stat = stats_by_pvc.get((namespace, name))
        if stat:
            pct = stat["used_bytes"] / stat["capacity_bytes"] * 100
            severity = (
                "critical" if pct >= PVC_ALMOST_FULL_CRITICAL_PCT else
                "warning" if pct >= PVC_ALMOST_FULL_WARNING_PCT else
                None
            )
            if severity:
                issues.append(_issue(
                    "PVCAlmostFull", severity, namespace, "PersistentVolumeClaim", name,
                    f"{pct:.0f}% full ({_fmt_bytes(stat['used_bytes'])} of {_fmt_bytes(stat['capacity_bytes'])}).",
                ))

        samples = samples_by_pvc.get((namespace, name))
        if samples:
            capacity_bytes = samples[-1][2]
            days = predict_days_to_exhaustion([(t, u) for t, u, _ in samples], capacity_bytes)
            if days is not None and days <= PVC_EXHAUSTION_WARNING_DAYS:
                issues.append(_issue(
                    "PVCCapacityExhaustionPredicted", "critical" if days <= 2 else "warning",
                    namespace, "PersistentVolumeClaim", name,
                    f"Growing toward full capacity - estimated exhaustion in {days:.1f} day(s) "
                    "at the current growth rate.",
                ))

        if phase == "Bound" and (namespace, name) not in mounted_claims:
            issues.append(_issue(
                "UnusedPVC", "info", namespace, "PersistentVolumeClaim", name,
                "Bound but not currently mounted by any pod - check whether it's still needed.",
            ))

    for pv in pvs:
        if pv.get("status", {}).get("phase") == "Released":
            issues.append(_issue(
                "OrphanedPV", "warning", None, "PersistentVolume", pv["metadata"]["name"],
                "Released - its claim was deleted but the reclaim policy retained the underlying "
                "storage; won't be reused until manually cleaned up.",
            ))

    return issues
