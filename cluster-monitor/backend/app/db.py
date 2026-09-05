"""PostgreSQL-backed issue history.

Each detection cycle reconciles the freshly-detected issue list against
what's stored: new fingerprints are inserted, ones that recur have
their last_seen bumped, and previously-active ones that didn't show up
this cycle get marked resolved. This is what gives the "Issues" page
both a live view (WHERE active) and a history (everything else).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg2://monitor:monitor@localhost:5432/cluster_monitor"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Issue(Base):
    __tablename__ = "issues"

    id = Column(Integer, primary_key=True)
    fingerprint = Column(String, nullable=False, unique=True, index=True)
    rule = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    namespace = Column(String, nullable=True)
    resource_kind = Column(String, nullable=False)
    resource_name = Column(String, nullable=False)
    message = Column(String, nullable=False)
    first_seen = Column(DateTime(timezone=True), nullable=False)
    last_seen = Column(DateTime(timezone=True), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    active = Column(Boolean, nullable=False, default=True)


class PvcUsageSample(Base):
    """One row per sampling cycle per PVC-backed volume that's currently
    mounted - append-only, so storage.predict_days_to_exhaustion has a
    real history to fit a trend to. Sampled far less often than issue
    detection itself (see main.py's STORAGE_SAMPLE_EVERY_N_CYCLES) - a
    PVC filling up over days/weeks doesn't need a 30-second resolution,
    and this table would otherwise grow unbounded."""
    __tablename__ = "pvc_usage_samples"

    id = Column(Integer, primary_key=True)
    namespace = Column(String, nullable=False, index=True)
    pvc_name = Column(String, nullable=False, index=True)
    used_bytes = Column(BigInteger, nullable=False)
    capacity_bytes = Column(BigInteger, nullable=False)
    sampled_at = Column(DateTime(timezone=True), nullable=False, index=True)


PVC_SAMPLE_RETENTION_DAYS = 30


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def reconcile_issues(db: Session, detected: list[dict]) -> None:
    """detected: the current-cycle output of detector.detect_all_issues."""
    now = datetime.now(timezone.utc)
    detected_by_fingerprint = {d["fingerprint"]: d for d in detected}

    existing = {i.fingerprint: i for i in db.query(Issue).all()}

    for fingerprint, data in detected_by_fingerprint.items():
        row = existing.get(fingerprint)
        if row is None:
            db.add(Issue(
                fingerprint=fingerprint,
                rule=data["rule"],
                severity=data["severity"],
                namespace=data["namespace"],
                resource_kind=data["resource_kind"],
                resource_name=data["resource_name"],
                message=data["message"],
                first_seen=now,
                last_seen=now,
                active=True,
            ))
        else:
            row.last_seen = now
            row.message = data["message"]
            row.severity = data["severity"]
            row.active = True
            row.resolved_at = None

    for fingerprint, row in existing.items():
        if fingerprint not in detected_by_fingerprint and row.active:
            row.active = False
            row.resolved_at = now

    db.commit()


def record_pvc_usage_samples(db: Session, volume_stats: list[dict]) -> None:
    """volume_stats: k8s_client.list_all_volume_stats' output - one row
    per PVC-backed volume actually mounted right now. A PVC with no pod
    currently mounting it (and so no kubelet stats for it) simply gets no
    sample this cycle, same as any other missing-data gap."""
    now = datetime.now(timezone.utc)
    for stat in volume_stats:
        if not stat.get("capacity_bytes"):
            continue
        db.add(PvcUsageSample(
            namespace=stat["namespace"],
            pvc_name=stat["pvc_name"],
            used_bytes=stat["used_bytes"],
            capacity_bytes=stat["capacity_bytes"],
            sampled_at=now,
        ))
    db.commit()


def prune_old_pvc_usage_samples(db: Session, retention_days: int = PVC_SAMPLE_RETENTION_DAYS) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    db.query(PvcUsageSample).filter(PvcUsageSample.sampled_at < cutoff).delete()
    db.commit()


def load_recent_pvc_samples(
    db: Session, retention_days: int = PVC_SAMPLE_RETENTION_DAYS
) -> dict[tuple[str, str], list[tuple[datetime, int, int]]]:
    """{(namespace, pvc_name): [(sampled_at, used_bytes, capacity_bytes),
    ...]}, ascending by time - storage.build_storage_issues' expected
    input shape."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    rows = (
        db.query(PvcUsageSample)
        .filter(PvcUsageSample.sampled_at >= cutoff)
        .order_by(PvcUsageSample.sampled_at.asc())
        .all()
    )
    by_pvc: dict[tuple[str, str], list[tuple[datetime, int, int]]] = {}
    for r in rows:
        by_pvc.setdefault((r.namespace, r.pvc_name), []).append((r.sampled_at, r.used_bytes, r.capacity_bytes))
    return by_pvc
