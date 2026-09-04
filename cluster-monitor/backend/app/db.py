"""PostgreSQL-backed issue history.

Each detection cycle reconciles the freshly-detected issue list against
what's stored: new fingerprints are inserted, ones that recur have
their last_seen bumped, and previously-active ones that didn't show up
this cycle get marked resolved. This is what gives the "Issues" page
both a live view (WHERE active) and a history (everything else).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String, create_engine
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
