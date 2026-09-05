from datetime import datetime, timedelta, timezone

from app.predictions import (
    build_cluster_capacity_forecast,
    build_node_capacity_predictions,
    build_pod_growth_trend,
    build_restart_trend_issues,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _stats(cpu_used, cpu_alloc=2000, mem_used=0, mem_alloc=4 * 1024**3, disk_used=None, disk_cap=None):
    return {
        "cpu_used_millicores": cpu_used,
        "cpu_allocatable_millicores": cpu_alloc,
        "memory_used_bytes": mem_used,
        "memory_allocatable_bytes": mem_alloc,
        "disk_used_bytes": disk_used,
        "disk_capacity_bytes": disk_cap,
    }


def test_build_node_capacity_predictions_flags_growing_cpu():
    # +500m/day, 2000m allocatable, currently at 1800m -> 0.4 days to exhaustion (critical)
    samples_by_node = {
        "k8s-worker1": [
            (T0, _stats(cpu_used=1300)),
            (T0 + timedelta(days=1), _stats(cpu_used=1800)),
        ]
    }

    issues = build_node_capacity_predictions(samples_by_node)

    [issue] = [i for i in issues if i["rule"] == "NodeCapacityExhaustionPredicted"]
    assert issue["severity"] == "critical"
    assert issue["resource_name"] == "k8s-worker1"


def test_build_node_capacity_predictions_flags_growing_memory():
    gib = 1024**3
    samples_by_node = {
        "k8s-worker1": [
            (T0, _stats(cpu_used=100, mem_used=2 * gib, mem_alloc=4 * gib)),
            (T0 + timedelta(days=1), _stats(cpu_used=100, mem_used=3 * gib, mem_alloc=4 * gib)),
        ]
    }

    issues = build_node_capacity_predictions(samples_by_node)

    assert any(i["rule"] == "MemoryGrowthPredicted" for i in issues)


def test_build_node_capacity_predictions_flags_disk_exhaustion():
    gib = 1024**3
    samples_by_node = {
        "k8s-worker1": [
            (T0, _stats(cpu_used=100, disk_used=5 * gib, disk_cap=10 * gib)),
            (T0 + timedelta(days=1), _stats(cpu_used=100, disk_used=6 * gib, disk_cap=10 * gib)),
        ]
    }

    issues = build_node_capacity_predictions(samples_by_node)

    assert any(i["rule"] == "DiskExhaustionPredicted" for i in issues)


def test_build_node_capacity_predictions_silent_when_flat_or_insufficient_history():
    samples_by_node = {
        "k8s-worker1": [(T0, _stats(cpu_used=500))],  # only 1 sample
        "k8s-worker2": [
            (T0, _stats(cpu_used=500)),
            (T0 + timedelta(days=1), _stats(cpu_used=500)),  # flat
        ],
    }

    assert build_node_capacity_predictions(samples_by_node) == []


def test_build_node_capacity_predictions_silent_when_far_from_exhaustion():
    # +1m/day on a 2000m node currently at 100m - centuries away, not 14 days
    samples_by_node = {
        "k8s-worker1": [
            (T0, _stats(cpu_used=99)),
            (T0 + timedelta(days=1), _stats(cpu_used=100)),
        ]
    }

    assert build_node_capacity_predictions(samples_by_node) == []


def test_build_cluster_capacity_forecast_sums_across_nodes_at_shared_timestamps():
    # Two nodes, same timestamps (as record_node_usage_samples would write them).
    # Combined: 1000 -> 1800 over 1 day, alloc 2000+2000=4000 -> plenty of room, no crisis,
    # but should still produce a real (non-None) days figure since it's growing.
    samples_by_node = {
        "k8s-worker1": [(T0, _stats(cpu_used=500)), (T0 + timedelta(days=1), _stats(cpu_used=900))],
        "k8s-worker2": [(T0, _stats(cpu_used=500)), (T0 + timedelta(days=1), _stats(cpu_used=900))],
    }

    forecast = build_cluster_capacity_forecast(samples_by_node)

    assert forecast["cpu_days_to_exhaustion"] is not None
    assert forecast["memory_days_to_exhaustion"] is None  # flat memory in the fixture


def test_build_cluster_capacity_forecast_none_with_insufficient_history():
    forecast = build_cluster_capacity_forecast({})
    assert forecast == {"cpu_days_to_exhaustion": None, "memory_days_to_exhaustion": None}


def test_build_restart_trend_issues_flags_sustained_growth():
    samples = [(T0, 5), (T0 + timedelta(days=1), 10), (T0 + timedelta(days=2), 15)]  # +5/day

    issues = build_restart_trend_issues(samples)

    assert len(issues) == 1
    assert issues[0]["rule"] == "RestartTrendIncreasing"
    assert "5.0/day" in issues[0]["message"] or "5/day" in issues[0]["message"]


def test_build_restart_trend_issues_silent_when_flat_or_below_threshold():
    flat = [(T0, 5), (T0 + timedelta(days=1), 5)]
    minor = [(T0, 5), (T0 + timedelta(days=1), 5.3)]  # well under the 1.0/day threshold

    assert build_restart_trend_issues(flat) == []
    assert build_restart_trend_issues(minor) == []


def test_build_pod_growth_trend_reports_slope_not_an_eta():
    samples = [(T0, 20), (T0 + timedelta(days=1), 25)]  # +5 pods/day

    trend = build_pod_growth_trend(samples)

    assert trend["pods_per_day"] == 5.0


def test_build_pod_growth_trend_none_with_insufficient_history():
    assert build_pod_growth_trend([(T0, 20)]) == {"pods_per_day": None}
