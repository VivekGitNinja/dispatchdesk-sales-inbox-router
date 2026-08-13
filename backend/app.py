"""app.py — FastAPI entry point for DispatchDesk Sales Inbox Router.

Kept deliberately thin: HTTP endpoints + middleware only. The heavy logic lives
in sibling modules:
- schemas.py      → Pydantic v2 models, enums, taxonomy, payload validation
- db.py           → SQLAlchemy engine, models, serializers, team roster
- rules_engine.py → cleaning, extraction, fallback router, rules, roles, Gemini
- chat_rag.py     → grounded analytics chat
"""

import datetime
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from sqlalchemy import func, or_

from chat_rag import build_chat_answer, phrase_chat_with_gemini
from db import (CANDIDATE_ID_DEFAULT, EmailLog, SessionLocal, Task, TEAM,
                log_to_dict, new_task_id, task_to_dict)
from rules_engine import (analyze_email, apply_rules, clean_email_body,
                          extract_company, extract_date_from_text,
                          fallback_analyze, parse_inr_from_text)
from schemas import ChatRequest, normalize_candidate_id, validate_task_payload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# App + CORS (configurable; default open for local dev / open-source demo)
# ────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="DispatchDesk Sales Inbox Router")

_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_TOKEN = os.getenv("API_TOKEN", "").strip()
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "0"))  # 0 = disabled
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(10 * 1024 * 1024)))  # 10 MB default

# Per-client sliding-window buckets (in-process; sufficient for a single node).
_rate_buckets: Dict[str, List[float]] = {}


# ────────────────────────────────────────────────────────────────────────────
# Middleware: auth → rate limit → body-size guard
# ────────────────────────────────────────────────────────────────────────────

@app.middleware("http")
async def api_token_auth(request: Request, call_next):
    """Optional bearer-token auth. When API_TOKEN is set, every request except
    the frontend/health endpoints must present it."""
    if not API_TOKEN:
        return await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/frontend") or path == "/health":
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {API_TOKEN}":
        return JSONResponse(status_code=401, content={"error": "unauthorized", "detail": "Missing or invalid API token"})
    return await call_next(request)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Sliding-window per-client rate limit on data endpoints (opt-in)."""
    if RATE_LIMIT_PER_MINUTE <= 0:
        return await call_next(request)
    path = request.url.path
    if path == "/" or path == "/health" or path.startswith("/frontend"):
        return await call_next(request)

    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - 60.0
    window = _rate_buckets.setdefault(client, [])
    while window and window[0] < cutoff:
        window.pop(0)
    if len(window) >= RATE_LIMIT_PER_MINUTE:
        return JSONResponse(status_code=429, content={"error": "rate_limited", "detail": "Too many requests"})
    window.append(now)
    return await call_next(request)


@app.middleware("http")
async def body_size_middleware(request: Request, call_next):
    """Reject oversized request bodies before they are read into memory."""
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"error": "payload_too_large",
                                                      "detail": f"Request body exceeds {MAX_BODY_BYTES} bytes"})
    return await call_next(request)


# ────────────────────────────────────────────────────────────────────────────
# Manual Task API
# ────────────────────────────────────────────────────────────────────────────

@app.post("/tasks")
async def create_task(request: Request):
    payload = await request.json()
    data, error = validate_task_payload(payload, partial=False)
    if error:
        return error

    with SessionLocal() as db:
        existing = db.query(Task).filter(Task.candidate_id == data["candidate_id"], Task.source_email_id == data["source_email_id"]).first()
        if existing:
            return JSONResponse(status_code=201, content={
                "task_id": existing.task_id,
                "candidate_id": existing.candidate_id,
                "source_email_id": existing.source_email_id,
                "created_at": existing.created_at.isoformat() if existing.created_at else None,
            })

        task = Task(task_id=new_task_id(), **data)
        db.add(task)
        db.commit()
        db.refresh(task)
        return JSONResponse(status_code=201, content={
            "task_id": task.task_id,
            "candidate_id": task.candidate_id,
            "source_email_id": task.source_email_id,
            "created_at": task.created_at.isoformat() if task.created_at else None,
        })


@app.patch("/tasks/{task_id}")
async def patch_task(task_id: str, request: Request):
    payload = await request.json()
    allowed_fields = ["title", "description", "assignee_id", "category", "priority", "due_date", "deal_value_inr", "company_name", "confidence"]
    limited_payload = {k: v for k, v in payload.items() if k in allowed_fields}
    data, error = validate_task_payload(limited_payload, partial=True)
    if error:
        return error

    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if not task:
            return JSONResponse(status_code=404, content={"error": "task_not_found", "task_id": task_id})

        changed = False
        for key, value in data.items():
            if getattr(task, key) != value:
                setattr(task, key, value)
                changed = True

        if changed:
            task.updated_at = datetime.datetime.utcnow()
            task.update_count = (task.update_count or 0) + 1
            db.commit()
            db.refresh(task)

        return task_to_dict(task)


@app.get("/tasks")
def list_tasks(candidate_id: Optional[str] = None, thread_id: Optional[str] = None, source_email_id: Optional[str] = None, assignee_id: Optional[str] = None):
    if not candidate_id:
        return JSONResponse(status_code=400, content={"error": "missing_required_field", "field": "candidate_id"})

    cid = normalize_candidate_id(candidate_id)
    with SessionLocal() as db:
        query = db.query(Task).filter(Task.candidate_id == cid)
        if thread_id:
            query = query.filter(Task.thread_id == thread_id)
        if source_email_id:
            query = query.filter(Task.source_email_id == source_email_id)
        if assignee_id:
            query = query.filter(Task.assignee_id == assignee_id)

        tasks = query.order_by(Task.created_at.asc()).all()
        return [task_to_dict(t) for t in tasks]


@app.delete("/tasks/{task_id}")
def delete_task(task_id: str):
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if not task:
            return JSONResponse(status_code=404, content={"error": "task_not_found", "task_id": task_id})
        db.delete(task)
        db.commit()
        return {"deleted": True, "task_id": task_id}


@app.get("/users")
def users():
    return TEAM


# ────────────────────────────────────────────────────────────────────────────
# Ingestion
# ────────────────────────────────────────────────────────────────────────────

@app.post("/ingest")
async def ingest(request: Request):
    payload = await request.json()
    cid = normalize_candidate_id(payload.get("candidate_id"))
    if not cid:
        return JSONResponse(status_code=400, content={"error": "missing_required_field", "field": "candidate_id"})

    emails = payload.get("emails", [])
    if not isinstance(emails, list):
        return JSONResponse(status_code=400, content={"error": "invalid_payload", "field": "emails"})

    batch_id = uuid.uuid4().hex[:8]
    counts = {"processed": 0, "tasks_created": 0, "tasks_updated": 0, "skipped": 0, "errors": []}

    with SessionLocal() as db:
        for email in emails:
            email_id = email.get("email_id")
            thread_id = email.get("thread_id")

            if not email_id:
                continue

            existing_log = db.query(EmailLog).filter(EmailLog.candidate_id == cid, EmailLog.email_id == email_id).first()
            if existing_log:
                continue

            cleaned = clean_email_body(email.get("body"))

            existing_task = None
            if thread_id:
                task = db.query(Task).filter(Task.candidate_id == cid, Task.thread_id == thread_id).first()
                if task:
                    existing_task = task_to_dict(task)

            analysis = await analyze_email(email, cleaned, existing_task)

            # Thread reconciliation: replies on existing threads must update, not create
            if existing_task and analysis.get("action") != "skip":
                analysis["action"] = "update_task"
            elif email.get("is_reply") and existing_task:
                analysis["action"] = "update_task"

            log = EmailLog(
                candidate_id=cid,
                batch_id=batch_id,
                email_id=email_id,
                thread_id=thread_id,
                subject=email.get("subject"),
                from_name=email.get("from_name"),
                from_email=email.get("from_email"),
                received_at=email.get("received_at"),
                cleaned_body=cleaned,
                decision=analysis.get("action"),
                category=analysis.get("category"),
                assignee_id=analysis.get("assignee_id"),
                priority=analysis.get("priority"),
                due_date=analysis.get("due_date"),
                deal_value_inr=analysis.get("deal_value_inr"),
                company_name=analysis.get("company_name"),
                confidence=analysis.get("confidence") or 0.0,
                skip_reason=analysis.get("skip_reason"),
                reasoning=analysis.get("reasoning"),
            )

            if analysis.get("action") == "skip":
                counts["skipped"] += 1
                log.decision = "skipped"

            elif analysis.get("action") == "update_task" and existing_task:
                task = db.query(Task).filter(Task.task_id == existing_task["task_id"]).first()
                for key in ["title", "description", "assignee_id", "category", "priority", "due_date", "deal_value_inr", "company_name", "confidence"]:
                    if analysis.get(key) is not None:
                        setattr(task, key, analysis.get(key))
                task.update_count += 1
                task.updated_at = datetime.datetime.utcnow()
                counts["tasks_updated"] += 1
                log.is_update = True
                log.task_id = task.task_id

            else:
                task = Task(
                    task_id=new_task_id(),
                    candidate_id=cid,
                    source_email_id=email_id,
                    thread_id=thread_id,
                    title=analysis.get("title") or email.get("subject"),
                    description=analysis.get("description"),
                    assignee_id=analysis.get("assignee_id"),
                    category=analysis.get("category"),
                    priority=analysis.get("priority") or "medium",
                    due_date=analysis.get("due_date"),
                    deal_value_inr=analysis.get("deal_value_inr"),
                    company_name=analysis.get("company_name"),
                    confidence=analysis.get("confidence") or 0.0,
                    batch_id=batch_id
                )
                db.add(task)
                counts["tasks_created"] += 1
                log.task_id = task.task_id

            counts["processed"] += 1
            db.add(log)
            db.commit()

    return counts


# ────────────────────────────────────────────────────────────────────────────
# Frontend data API
# ────────────────────────────────────────────────────────────────────────────

@app.get("/api/tasks")
def api_tasks(candidate_id: Optional[str] = None):
    cid = normalize_candidate_id(candidate_id or CANDIDATE_ID_DEFAULT)
    with SessionLocal() as db:
        tasks = db.query(Task).filter(Task.candidate_id == cid).order_by(Task.created_at.desc()).all()
        skipped_logs = db.query(EmailLog).filter(EmailLog.candidate_id == cid, EmailLog.decision == "skipped").order_by(EmailLog.created_at.desc()).limit(500).all()
        return {
            "candidate_id": cid,
            "tasks": [task_to_dict(t) for t in tasks],
            "skipped": [log_to_dict(l) for l in skipped_logs],
        }


@app.get("/api/stats")
def api_stats(candidate_id: Optional[str] = None):
    cid = normalize_candidate_id(candidate_id or CANDIDATE_ID_DEFAULT)
    with SessionLocal() as db:
        processed = db.query(func.count(EmailLog.id)).filter(EmailLog.candidate_id == cid).scalar() or 0
        tasks_created = db.query(func.count(Task.task_id)).filter(Task.candidate_id == cid).scalar() or 0
        tasks_updated = db.query(func.coalesce(func.sum(Task.update_count), 0)).filter(Task.candidate_id == cid).scalar() or 0
        skipped = db.query(func.count(EmailLog.id)).filter(EmailLog.candidate_id == cid, EmailLog.decision == "skipped").scalar() or 0

        category_counts = dict(
            db.query(Task.category, func.count(Task.task_id))
            .filter(Task.candidate_id == cid)
            .group_by(Task.category)
            .all()
        )

        skipped_counts = dict(
            db.query(EmailLog.skip_reason, func.count(EmailLog.id))
            .filter(EmailLog.candidate_id == cid, EmailLog.decision == "skipped")
            .group_by(EmailLog.skip_reason)
            .all()
        )

        by_batch = dict(
            db.query(EmailLog.batch_id, func.count(EmailLog.id))
            .filter(EmailLog.candidate_id == cid)
            .group_by(EmailLog.batch_id)
            .all()
        )

        spurious = db.query(func.count(Task.task_id)).join(EmailLog, Task.source_email_id == EmailLog.email_id).filter(
            Task.candidate_id == cid,
            EmailLog.decision != "skipped",
            or_(EmailLog.category.in_(("skip_auto_reply", "skip_newsletter", "skip_vendor_spam")))
        ).scalar() or 0

        return {
            "candidate_id": cid,
            "processed": int(processed),
            "tasks_created": int(tasks_created),
            "tasks_updated": int(tasks_updated),
            "skipped": int(skipped),
            "spurious_flagged": int(spurious),
            "category_counts": category_counts,
            "skipped_counts": skipped_counts,
            "by_batch": by_batch,
        }


@app.get("/health")
def health():
    return {"status": "ok", "candidate_id": CANDIDATE_ID_DEFAULT}


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    cid = normalize_candidate_id(req.candidate_id or CANDIDATE_ID_DEFAULT)
    with SessionLocal() as db:
        answer, supporting = build_chat_answer(db, cid, req.query)
        if supporting.get("out_of_scope"):
            supporting = {}
        else:
            answer = await phrase_chat_with_gemini(req.query, supporting, answer)
        return {"answer": answer, "supporting_data": supporting}


# ────────────────────────────────────────────────────────────────────────────
# Static frontend
# ────────────────────────────────────────────────────────────────────────────

frontend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/frontend", StaticFiles(directory=frontend_dir, html=True), name="frontend")


@app.get("/")
def root():
    index_path = os.path.join(frontend_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"status": "DispatchDesk Sales Inbox Router API", "candidate_id": CANDIDATE_ID_DEFAULT}
