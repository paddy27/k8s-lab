from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import (
    Base,
    ClusterSnapshotSample,
    Issue,
    NodeUsageSample,
    PvcUsageSample,
    load_recent_cluster_snapshots,
    load_recent_node_samples,
    load_recent_pvc_samples,
    prune_old_cluster_snapshot_samples,
    prune_old_node_usage_samples,
    prune_old_pvc_usage_samples,
    reconcile_issues,
    record_cluster_snapshot_sample,
    record_node_usage_samples,
    record_pvc_usage_samples,
)


@pytest.fixture
def db():
    """A fresh in-memory SQLite DB per test - reconcile_issues uses no
    Postgres-specific features, so this is a faithful, fast substitute."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _detected(rule="CrashLoopBackOff", name="backend-1", message="crash-looping"):
    return {
        "fingerprint": f"{rule}:obs:Pod:{name}",
        "rule": rule,
        "severity": "critical",
        "namespace": "obs",
        "resource_kind": "Pod",
        "resource_name": name,
        "message": message,
    }


def test_reconcile_inserts_new_issue(db):
    reconcile_issues(db, [_detected()])

    [row] = db.query(Issue).all()
    assert row.rule == "CrashLoopBackOff"
    assert row.active is True
    assert row.resolved_at is None
    assert row.first_seen == row.last_seen


def test_reconcile_updates_last_seen_and_message_on_recurrence(db):
    reconcile_issues(db, [_detected(message="crash-looping (3 restarts)")])
    first_seen = db.query(Issue).one().first_seen

    reconcile_issues(db, [_detected(message="crash-looping (4 restarts)")])

    [row] = db.query(Issue).all()
    assert row.message == "crash-looping (4 restarts)"
    assert row.first_seen == first_seen  # unchanged - this is the same ongoing issue
    assert row.active is True


def test_reconcile_marks_issue_resolved_when_no_longer_detected(db):
    reconcile_issues(db, [_detected()])

    reconcile_issues(db, [])  # nothing detected this cycle

    [row] = db.query(Issue).all()
    assert row.active is False
    assert row.resolved_at is not None


def test_reconcile_reactivates_a_resolved_issue_that_recurs(db):
    reconcile_issues(db, [_detected()])
    reconcile_issues(db, [])  # resolved
    assert db.query(Issue).one().active is False

    reconcile_issues(db, [_detected()])  # comes back

    row = db.query(Issue).one()
    assert row.active is True
    assert row.resolved_at is None


def test_reconcile_keeps_unrelated_issues_untouched(db):
    reconcile_issues(db, [_detected(name="a"), _detected(name="b")])

    reconcile_issues(db, [_detected(name="a")])  # "b" no longer detected

    rows = {r.resource_name: r for r in db.query(Issue).all()}
    assert rows["a"].active is True
    assert rows["b"].active is False


def _volume_stat(namespace="obs", pvc_name="data", used_bytes=1024, capacity_bytes=10 * 1024**3):
    return {"namespace": namespace, "pvc_name": pvc_name, "used_bytes": used_bytes, "capacity_bytes": capacity_bytes}


def test_record_pvc_usage_samples_inserts_one_row_per_stat(db):
    record_pvc_usage_samples(db, [_volume_stat(pvc_name="a"), _volume_stat(pvc_name="b")])

    assert db.query(PvcUsageSample).count() == 2


def test_record_pvc_usage_samples_skips_entries_with_no_capacity(db):
    """A PVC-backed volume the kubelet reported with no capacity figure
    (e.g. mid-mount) isn't useful history - skip it rather than storing
    a sample that would poison the trend fit with a zero/None capacity."""
    record_pvc_usage_samples(db, [{"namespace": "obs", "pvc_name": "a", "used_bytes": 100, "capacity_bytes": None}])

    assert db.query(PvcUsageSample).count() == 0


def test_load_recent_pvc_samples_groups_by_pvc_ascending_by_time(db):
    record_pvc_usage_samples(db, [_volume_stat(pvc_name="a", used_bytes=100)])
    record_pvc_usage_samples(db, [_volume_stat(pvc_name="a", used_bytes=200)])

    by_pvc = load_recent_pvc_samples(db)

    [(namespace, pvc_name)] = by_pvc.keys()
    assert (namespace, pvc_name) == ("obs", "a")
    used_values = [s[1] for s in by_pvc[("obs", "a")]]
    assert used_values == [100, 200]  # ascending by time, not insertion order coincidence


def test_prune_old_pvc_usage_samples_removes_only_stale_rows(db):
    fresh = PvcUsageSample(namespace="obs", pvc_name="a", used_bytes=1, capacity_bytes=10,
                            sampled_at=datetime.now(timezone.utc))
    stale = PvcUsageSample(namespace="obs", pvc_name="a", used_bytes=1, capacity_bytes=10,
                            sampled_at=datetime.now(timezone.utc) - timedelta(days=60))
    db.add_all([fresh, stale])
    db.commit()

    prune_old_pvc_usage_samples(db, retention_days=30)

    remaining = db.query(PvcUsageSample).all()
    assert len(remaining) == 1
    assert remaining[0].used_bytes == fresh.used_bytes and remaining[0].sampled_at == fresh.sampled_at


def _node_stat(node_name="k8s-worker1", cpu_used=500, cpu_alloc=2000, mem_used=1024, mem_alloc=4096,
               disk_used=None, disk_cap=None):
    return {
        "node_name": node_name, "cpu_used_millicores": cpu_used, "cpu_allocatable_millicores": cpu_alloc,
        "memory_used_bytes": mem_used, "memory_allocatable_bytes": mem_alloc,
        "disk_used_bytes": disk_used, "disk_capacity_bytes": disk_cap,
    }


def test_record_node_usage_samples_inserts_one_row_per_node(db):
    record_node_usage_samples(db, [_node_stat("k8s-master"), _node_stat("k8s-worker1")])

    assert db.query(NodeUsageSample).count() == 2


def test_load_recent_node_samples_groups_by_node_ascending_by_time(db):
    record_node_usage_samples(db, [_node_stat("k8s-worker1", cpu_used=100)])
    record_node_usage_samples(db, [_node_stat("k8s-worker1", cpu_used=200)])

    by_node = load_recent_node_samples(db)

    [cpu_values] = [[s[1]["cpu_used_millicores"] for s in samples] for samples in by_node.values()]
    assert cpu_values == [100, 200]


def test_prune_old_node_usage_samples_removes_only_stale_rows(db):
    fresh = NodeUsageSample(node_name="a", cpu_used_millicores=1, cpu_allocatable_millicores=10,
                             memory_used_bytes=1, memory_allocatable_bytes=10, sampled_at=datetime.now(timezone.utc))
    stale = NodeUsageSample(node_name="a", cpu_used_millicores=1, cpu_allocatable_millicores=10,
                             memory_used_bytes=1, memory_allocatable_bytes=10,
                             sampled_at=datetime.now(timezone.utc) - timedelta(days=60))
    db.add_all([fresh, stale])
    db.commit()

    prune_old_node_usage_samples(db, retention_days=30)

    assert db.query(NodeUsageSample).count() == 1


def test_record_and_load_cluster_snapshot_samples_ascending_by_time(db):
    record_cluster_snapshot_sample(db, pod_count=20, total_restart_count=3)
    record_cluster_snapshot_sample(db, pod_count=22, total_restart_count=5)

    snapshots = load_recent_cluster_snapshots(db)

    pod_counts = [s[1] for s in snapshots]
    restart_counts = [s[2] for s in snapshots]
    assert pod_counts == [20, 22]
    assert restart_counts == [3, 5]


def test_prune_old_cluster_snapshot_samples_removes_only_stale_rows(db):
    fresh = ClusterSnapshotSample(pod_count=1, total_restart_count=0, sampled_at=datetime.now(timezone.utc))
    stale = ClusterSnapshotSample(pod_count=1, total_restart_count=0,
                                   sampled_at=datetime.now(timezone.utc) - timedelta(days=60))
    db.add_all([fresh, stale])
    db.commit()

    prune_old_cluster_snapshot_samples(db, retention_days=30)

    assert db.query(ClusterSnapshotSample).count() == 1
