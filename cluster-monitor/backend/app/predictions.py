"""Trend & Prediction Analysis - extends the "Top 5" beyond the original
agreed set, at the user's request to continue through the rest of the
plan doc's wishlist.

Node/cluster capacity forecasting reuses the *exact* linear-regression
exhaustion model already built and unit-tested for Storage Analysis
(storage.predict_days_to_exhaustion) rather than inventing a second one -
"when will this node's CPU/memory/disk fill up" is the same math as
"when will this PVC fill up", just against a different capacity ceiling.
Pod Growth Trend and Restart Trend have no natural capacity ceiling, so
they're reported as a plain trend (direction + rate), not an ETA -
forcing an "exhaustion date" onto an unbounded quantity would be a
fabricated precision this project has consistently avoided elsewhere.

Pure functions over plain dicts/tuples, same philosophy as detector.py/
storage.py/best_practices.py/root_cause.py.
"""
from __future__ import annotations

from datetime import datetime

from app.detector import _issue
from app.storage import predict_days_to_exhaustion

# A bit longer than PVC's 7-day window - this is capacity *planning*,
# not an imminent-failure alarm the way a near-full PVC is.
EXHAUSTION_WARNING_DAYS = 14.0
EXHAUSTION_CRITICAL_DAYS = 3.0

# Only worth flagging a restart-count trend once it's climbing at a
# real clip, not any positive slope at all (a handful of restarts
# during a rollout would otherwise trip this every time).
RESTART_TREND_THRESHOLD_PER_DAY = 1.0


def _linear_slope_per_day(samples: list[tuple[datetime, float]]) -> float | None:
    """Same OLS fit as predict_days_to_exhaustion, but returns the raw
    slope (units/day) instead of extrapolating to a capacity - for
    trends with no natural ceiling to extrapolate toward."""
    if len(samples) < 2:
        return None
    t0 = samples[0][0]
    xs = [(t - t0).total_seconds() for t, _ in samples]
    ys = [float(v) for _, v in samples]
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    slope_per_second = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    return slope_per_second * 86400


def _exhaustion_severity(days: float) -> str:
    return "critical" if days <= EXHAUSTION_CRITICAL_DAYS else "warning"


def build_node_capacity_predictions(samples_by_node: dict[str, list[tuple[datetime, dict]]]) -> list[dict]:
    """samples_by_node: {node_name: [(sampled_at, stats_dict), ...]}
    ascending by time, stats_dict shaped like
    k8s_client._node_resource_stats_from_summary's output (minus
    node_name). Node Capacity Exhaustion (CPU), Memory Growth
    Prediction, and Disk Exhaustion Prediction (node root filesystem) -
    three dimensions, same model each time."""
    issues = []
    for node_name, samples in samples_by_node.items():
        if len(samples) < 2:
            continue
        latest = samples[-1][1]

        cpu_days = predict_days_to_exhaustion(
            [(t, s["cpu_used_millicores"]) for t, s in samples], latest["cpu_allocatable_millicores"],
        )
        if cpu_days is not None and cpu_days <= EXHAUSTION_WARNING_DAYS:
            issues.append(_issue(
                "NodeCapacityExhaustionPredicted", _exhaustion_severity(cpu_days), None, "Node", node_name,
                f"CPU usage trending toward full allocatable capacity - estimated in {cpu_days:.1f} "
                "day(s) at the current growth rate.",
            ))

        mem_days = predict_days_to_exhaustion(
            [(t, s["memory_used_bytes"]) for t, s in samples], latest["memory_allocatable_bytes"],
        )
        if mem_days is not None and mem_days <= EXHAUSTION_WARNING_DAYS:
            issues.append(_issue(
                "MemoryGrowthPredicted", _exhaustion_severity(mem_days), None, "Node", node_name,
                f"Memory usage trending toward full allocatable capacity - estimated in {mem_days:.1f} "
                "day(s) at the current growth rate.",
            ))

        disk_capacity = latest.get("disk_capacity_bytes")
        if disk_capacity:
            disk_samples = [(t, s["disk_used_bytes"]) for t, s in samples if s.get("disk_used_bytes") is not None]
            disk_days = predict_days_to_exhaustion(disk_samples, disk_capacity)
            if disk_days is not None and disk_days <= EXHAUSTION_WARNING_DAYS:
                issues.append(_issue(
                    "DiskExhaustionPredicted", _exhaustion_severity(disk_days), None, "Node", node_name,
                    f"Node root filesystem usage trending toward full capacity - estimated in "
                    f"{disk_days:.1f} day(s) at the current growth rate.",
                ))

    return issues


def build_cluster_capacity_forecast(samples_by_node: dict[str, list[tuple[datetime, dict]]]) -> dict:
    """Cluster-wide CPU/memory forecast: sums every node's usage and
    allocatable at each shared sample timestamp, then applies the same
    regression to the combined series. Bucketing by exact timestamp
    (not a nearest-match/interpolation) is valid here, not approximate:
    every node sampled in the same detection cycle is written with the
    identical `sampled_at` value (db.record_node_usage_samples uses one
    `now` per call), so nodes sampled together share exact keys."""
    by_time: dict[datetime, dict] = {}
    for samples in samples_by_node.values():
        for t, s in samples:
            bucket = by_time.setdefault(t, {"cpu_used": 0.0, "cpu_alloc": 0.0, "mem_used": 0.0, "mem_alloc": 0.0})
            bucket["cpu_used"] += s["cpu_used_millicores"]
            bucket["cpu_alloc"] += s["cpu_allocatable_millicores"]
            bucket["mem_used"] += s["memory_used_bytes"]
            bucket["mem_alloc"] += s["memory_allocatable_bytes"]

    ordered = sorted(by_time.items())
    if len(ordered) < 2:
        return {"cpu_days_to_exhaustion": None, "memory_days_to_exhaustion": None}

    latest_cpu_alloc = ordered[-1][1]["cpu_alloc"]
    latest_mem_alloc = ordered[-1][1]["mem_alloc"]
    return {
        "cpu_days_to_exhaustion": predict_days_to_exhaustion([(t, b["cpu_used"]) for t, b in ordered], latest_cpu_alloc),
        "memory_days_to_exhaustion": predict_days_to_exhaustion([(t, b["mem_used"]) for t, b in ordered], latest_mem_alloc),
    }


def build_restart_trend_issues(cluster_restart_samples: list[tuple[datetime, int]]) -> list[dict]:
    """Cluster-wide total container restart count over time. A rising
    trend across the *whole* cluster (not one flaky pod) suggests a
    systemic cause - a bad rollout, resource pressure, a flaky shared
    dependency - worth a distinct signal from any single pod's own
    FrequentRestarts/CrashLoopBackOff issue."""
    slope = _linear_slope_per_day(cluster_restart_samples)
    if slope is not None and slope >= RESTART_TREND_THRESHOLD_PER_DAY:
        return [_issue(
            "RestartTrendIncreasing", "warning", None, "Cluster", "cluster",
            f"Cluster-wide container restart count is trending up at roughly {slope:.1f}/day - "
            "check for a systemic issue (bad rollout, resource pressure, a flaky shared dependency) "
            "rather than treating each restart as an isolated pod problem.",
        )]
    return []


def build_pod_growth_trend(cluster_pod_count_samples: list[tuple[datetime, int]]) -> dict:
    """Informational only, not an issue - a growing pod count isn't
    inherently a problem (it might be entirely intentional scaling), so
    this is exposed as plain trend data for a dashboard to chart, not a
    reconciled issue with an implicit "this should stop" judgment."""
    slope = _linear_slope_per_day(cluster_pod_count_samples)
    return {"pods_per_day": round(slope, 2) if slope is not None else None}


def build_all_prediction_issues(samples_by_node: dict, cluster_restart_samples: list[tuple[datetime, int]]) -> list[dict]:
    return [
        *build_node_capacity_predictions(samples_by_node),
        *build_restart_trend_issues(cluster_restart_samples),
    ]
