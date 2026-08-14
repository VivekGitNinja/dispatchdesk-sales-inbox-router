"""db.py — SQLAlchemy engine, ORM models, serializers, and the team roster.

Holds the persistence layer only. No HTTP or routing logic lives here.
`import db` triggers `create_all`, so app startup always has the schema.
"""

import datetime
import os
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import (Boolean, Column, DateTime, Float, Integer, String, Text, UniqueConstraint, create_engine)
from sqlalchemy.orm import declarative_base, sessionmaker

from schemas import role_for_category

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_default_db = "sqlite:///" + os.path.join(BASE_DIR, "inbox_router.db")
DATABASE_URL = os.getenv("DATABASE_URL", _default_db)
CANDIDATE_ID_DEFAULT = os.getenv("CANDIDATE_ID", "demo@dispatchdesk.ai").strip().lower()

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False
if DATABASE_URL.startswith("postgres"):
    connect_args["sslmode"] = os.getenv("PGSSLMODE", "require")

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Task(Base):
    __tablename__ = "tasks"
    task_id = Column(String, primary_key=True)
    candidate_id = Column(String, index=True)
    source_email_id = Column(String, index=True)
    thread_id = Column(String, index=True)
    title = Column(Text)
    description = Column(Text)
    assignee_id = Column(String)
    category = Column(String)
    priority = Column(String)
    due_date = Column(String, nullable=True)
    deal_value_inr = Column(Integer, nullable=True)
    company_name = Column(String, nullable=True)
    confidence = Column(Float)
    update_count = Column(Integer, default=0)
    batch_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    __table_args__ = (UniqueConstraint("candidate_id", "source_email_id", name="uq_candidate_source_email"),)


class EmailLog(Base):
    __tablename__ = "email_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    candidate_id = Column(String, index=True)
    batch_id = Column(String, index=True)
    email_id = Column(String, index=True)
    thread_id = Column(String, index=True)
    subject = Column(Text)
    from_name = Column(Text)
    from_email = Column(Text)
    received_at = Column(String)
    cleaned_body = Column(Text)
    decision = Column(String)
    category = Column(String)
    assignee_id = Column(String)
    priority = Column(String)
    due_date = Column(String)
    deal_value_inr = Column(Integer)
    company_name = Column(String)
    confidence = Column(Float)
    skip_reason = Column(String)
    reasoning = Column(Text)
    task_id = Column(String)
    is_update = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    __table_args__ = (UniqueConstraint("candidate_id", "email_id", name="uq_candidate_email"),)


DB_ERROR = None
try:
    Base.metadata.create_all(bind=engine)
except Exception as exc:  # pragma: no cover - env-specific (e.g. read-only FS on serverless)
    # Keep the app importable so the UI and /health keep working. Endpoints
    # that touch the DB will fail with a clear message until DATABASE_URL is
    # configured (e.g. Vercel Postgres).
    DB_ERROR = f"Database unavailable: {exc}. Set DATABASE_URL to a reachable Postgres."
    engine = None

    def SessionLocal():  # type: ignore[no-redef]
        raise RuntimeError(DB_ERROR)


def new_task_id() -> str:
    return "tsk_" + uuid.uuid4().hex[:6]


def task_to_dict(task: Task) -> Dict[str, Any]:
    """Serialize a Task. `target_role` is derived from category so the role
    routing dimension is always present without a schema migration."""
    return {
        "task_id": task.task_id,
        "candidate_id": task.candidate_id,
        "source_email_id": task.source_email_id,
        "thread_id": task.thread_id,
        "title": task.title,
        "description": task.description,
        "assignee_id": task.assignee_id,
        "target_role": role_for_category(task.category).value,
        "category": task.category,
        "priority": task.priority,
        "due_date": task.due_date,
        "deal_value_inr": task.deal_value_inr,
        "company_name": task.company_name,
        "confidence": task.confidence,
        "update_count": task.update_count,
        "batch_id": task.batch_id,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


def log_to_dict(log: EmailLog) -> Dict[str, Any]:
    return {
        "email_id": log.email_id,
        "thread_id": log.thread_id,
        "subject": log.subject,
        "from_name": log.from_name,
        "from_email": log.from_email,
        "received_at": log.received_at,
        "decision": log.decision,
        "category": log.category,
        "assignee_id": log.assignee_id,
        "target_role": role_for_category(log.category).value,
        "priority": log.priority,
        "due_date": log.due_date,
        "deal_value_inr": log.deal_value_inr,
        "company_name": log.company_name,
        "confidence": log.confidence,
        "skip_reason": log.skip_reason,
        "reasoning": log.reasoning,
        "task_id": log.task_id,
        "is_update": log.is_update,
        "created_at": log.created_at.isoformat() if log.created_at else None,
        "body_preview": (log.cleaned_body or "")[:200],
    }


TEAM = {
    "team": [
        {"user_id": "u_aarti", "name": "Aarti Menon", "department": "Sales — Enterprise", "role": "FOUNDER_OPS",
         "scope": "RFPs, RFIs, tenders, and inbound deals above ₹10,00,000"},
        {"user_id": "u_rohit", "name": "Rohit Sharma", "department": "Sales — SMB", "role": "SALES_TEAM",
         "scope": "Product enquiries, demo requests, deals at or below ₹10,00,000"},
        {"user_id": "u_meera", "name": "Meera Iyer", "department": "Marketing", "role": "SALES_TEAM",
         "scope": "Webinars, event and conference sponsorships, content collaborations, PR and media"},
        {"user_id": "u_karan", "name": "Karan Doshi", "department": "Alliances", "role": "SALES_TEAM",
         "scope": "Reseller, channel partner, and technology integration proposals"},
        {"user_id": "u_divya", "name": "Divya Rao", "department": "Finance", "role": "FINANCE_TEAM",
         "scope": "Invoices, purchase orders, payment reminders, GST and vendor billing"},
        {"user_id": "u_triage", "name": "Triage Queue", "department": "Operations", "role": "SUPPORT_TEAM",
         "scope": "Ambiguous items requiring human review"},
    ]
}
