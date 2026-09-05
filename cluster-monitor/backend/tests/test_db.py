from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import (
    Base,
    Issue,
    PvcUsageSample,
    load_recent_pvc_samples,
    prune_old_pvc_usage_samples,
    reconcile_issues,
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
