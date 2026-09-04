import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, Issue, reconcile_issues


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
