from datetime import datetime, timedelta, timezone

from app.storage import build_storage_issues, predict_days_to_exhaustion

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_predict_days_to_exhaustion_extrapolates_linear_growth():
    # +1 GiB/day, 10 GiB capacity, currently at 5 GiB -> 5 more days
    gib = 1024**3
    samples = [
        (T0, 4 * gib),
        (T0 + timedelta(days=1), 5 * gib),
    ]

    days = predict_days_to_exhaustion(samples, capacity_bytes=10 * gib)

    assert days is not None
    assert abs(days - 5.0) < 0.01


def test_predict_days_to_exhaustion_none_when_shrinking_or_flat():
    gib = 1024**3
    flat = [(T0, 5 * gib), (T0 + timedelta(days=1), 5 * gib)]
    shrinking = [(T0, 5 * gib), (T0 + timedelta(days=1), 4 * gib)]

    assert predict_days_to_exhaustion(flat, capacity_bytes=10 * gib) is None
    assert predict_days_to_exhaustion(shrinking, capacity_bytes=10 * gib) is None


def test_predict_days_to_exhaustion_none_when_samples_span_too_short_a_window():
    """Regression test: 3 samples 5-7 minutes apart on a nearly-idle
    node (CPU bouncing 56m -> 42m -> 87m, ordinary noise) extrapolated
    into a false "0.5 days to exhaustion" alarm on this exact real data
    the first time predictions.py reused this function for node CPU.
    A short window is exactly when one noisy blip dominates the fit."""
    gib = 1024**3
    noisy_short_window = [
        (T0, 56 * gib // 1000),
        (T0 + timedelta(minutes=5), 42 * gib // 1000),
        (T0 + timedelta(minutes=7), 87 * gib // 1000),
    ]

    assert predict_days_to_exhaustion(noisy_short_window, capacity_bytes=2 * gib) is None


def test_predict_days_to_exhaustion_none_with_insufficient_history():
    assert predict_days_to_exhaustion([], capacity_bytes=10 * 1024**3) is None
    assert predict_days_to_exhaustion([(T0, 1024)], capacity_bytes=10 * 1024**3) is None


def _pvc(namespace, name, phase="Bound"):
    return {"metadata": {"namespace": namespace, "name": name}, "status": {"phase": phase}}


def _pod_mounting(namespace, pod_name, claim_name):
    return {
        "metadata": {"namespace": namespace, "name": pod_name},
        "spec": {"volumes": [{"name": "data", "persistent_volume_claim": {"claim_name": claim_name}}]},
    }


def test_build_storage_issues_flags_pending_pvc_as_failed_binding():
    pvcs = [_pvc("obs", "data", phase="Pending")]

    issues = build_storage_issues(pvcs, [], [], [], {})

    assert len(issues) == 1
    assert issues[0]["rule"] == "FailedPVCBinding"
    assert issues[0]["severity"] == "critical"


def test_build_storage_issues_flags_almost_full_by_severity():
    pvcs = [_pvc("obs", "critical-pvc"), _pvc("obs", "warning-pvc"), _pvc("obs", "fine-pvc")]
    gib = 1024**3
    volume_stats = [
        {"namespace": "obs", "pvc_name": "critical-pvc", "used_bytes": 95 * gib, "capacity_bytes": 100 * gib},
        {"namespace": "obs", "pvc_name": "warning-pvc", "used_bytes": 80 * gib, "capacity_bytes": 100 * gib},
        {"namespace": "obs", "pvc_name": "fine-pvc", "used_bytes": 10 * gib, "capacity_bytes": 100 * gib},
    ]
    pods = [_pod_mounting("obs", f"pod-{i}", n) for i, n in enumerate(["critical-pvc", "warning-pvc", "fine-pvc"])]

    issues = build_storage_issues(pvcs, [], pods, volume_stats, {})

    by_name = {i["resource_name"]: i for i in issues if i["rule"] == "PVCAlmostFull"}
    assert by_name["critical-pvc"]["severity"] == "critical"
    assert by_name["warning-pvc"]["severity"] == "warning"
    assert "fine-pvc" not in by_name


def test_build_storage_issues_flags_predicted_exhaustion_from_samples():
    gib = 1024**3
    pvcs = [_pvc("obs", "growing")]
    pods = [_pod_mounting("obs", "pod-0", "growing")]
    samples_by_pvc = {
        ("obs", "growing"): [
            (T0, 8 * gib, 10 * gib),
            (T0 + timedelta(days=1), 9 * gib, 10 * gib),  # +1 GiB/day -> 1 day to exhaustion
        ]
    }

    issues = build_storage_issues(pvcs, [], pods, [], samples_by_pvc)

    [issue] = [i for i in issues if i["rule"] == "PVCCapacityExhaustionPredicted"]
    assert issue["severity"] == "critical"  # <= 2 days out
    assert "exhaustion" in issue["message"]


def test_build_storage_issues_flags_unused_pvc_not_mounted_by_any_pod():
    pvcs = [_pvc("obs", "orphan-ish")]

    issues = build_storage_issues(pvcs, [], [], [], {})  # no pods mount it

    assert any(i["rule"] == "UnusedPVC" and i["severity"] == "info" for i in issues)


def test_build_storage_issues_silent_when_pvc_is_mounted():
    pvcs = [_pvc("obs", "in-use")]
    pods = [_pod_mounting("obs", "pod-0", "in-use")]

    issues = build_storage_issues(pvcs, [], pods, [], {})

    assert not any(i["rule"] == "UnusedPVC" for i in issues)


def test_build_storage_issues_flags_orphaned_released_pv():
    pvs = [{"metadata": {"name": "pv-1"}, "status": {"phase": "Released"}}]

    issues = build_storage_issues([], pvs, [], [], {})

    assert len(issues) == 1
    assert issues[0]["rule"] == "OrphanedPV"
    assert issues[0]["resource_kind"] == "PersistentVolume"


def test_build_storage_issues_silent_for_healthy_bound_pvc_no_history():
    pvcs = [_pvc("obs", "healthy")]
    pods = [_pod_mounting("obs", "pod-0", "healthy")]
    volume_stats = [{"namespace": "obs", "pvc_name": "healthy", "used_bytes": 1024, "capacity_bytes": 10 * 1024**3}]

    assert build_storage_issues(pvcs, [], pods, volume_stats, {}) == []
