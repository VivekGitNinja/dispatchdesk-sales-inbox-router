import os
import re
import json
import uuid
import asyncio
import datetime
import logging
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, String, Integer, Float, Text, DateTime, Boolean, UniqueConstraint, func, or_
from sqlalchemy.orm import sessionmaker, declarative_base
from pydantic import BaseModel
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_default_db = "sqlite:///" + os.path.join(BASE_DIR, "inbox_router.db")
DATABASE_URL = os.getenv("DATABASE_URL", _default_db)
CANDIDATE_ID_DEFAULT = os.getenv("CANDIDATE_ID", "priya.sharma@gmail.com").strip().lower()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False
if DATABASE_URL.startswith("postgres"):
    connect_args["sslmode"] = os.getenv("PGSSLMODE", "require")

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

ASSIGNEE_IDS = ["u_aarti", "u_rohit", "u_meera", "u_karan", "u_divya", "u_triage"]
CATEGORIES = ["enterprise_rfp", "smb_enquiry", "marketing", "alliances", "finance", "triage"]
PRIORITIES = ["high", "medium", "low"]
SKIP_CATEGORIES = ["skip_auto_reply", "skip_newsletter", "skip_vendor_spam", "skip_other"]

TEAM = {
    "team": [
        {"user_id": "u_aarti", "name": "Aarti Menon", "department": "Sales — Enterprise", "scope": "RFPs, RFIs, tenders, and inbound deals above ₹10,00,000"},
        {"user_id": "u_rohit", "name": "Rohit Sharma", "department": "Sales — SMB", "scope": "Product enquiries, demo requests, deals at or below ₹10,00,000"},
        {"user_id": "u_meera", "name": "Meera Iyer", "department": "Marketing", "scope": "Webinars, event and conference sponsorships, content collaborations, PR and media"},
        {"user_id": "u_karan", "name": "Karan Doshi", "department": "Alliances", "scope": "Reseller, channel partner, and technology integration proposals"},
        {"user_id": "u_divya", "name": "Divya Rao", "department": "Finance", "scope": "Invoices, purchase orders, payment reminders, GST and vendor billing"},
        {"user_id": "u_triage", "name": "Triage Queue", "department": "Operations", "scope": "Ambiguous items requiring human review"}
    ]
}

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

Base.metadata.create_all(bind=engine)

app = FastAPI(title="ALUMNX Sales Inbox Router")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    candidate_id: Optional[str] = None
    query: str

def normalize_candidate_id(value: Optional[str]) -> str:
    return str(value or "").strip().lower()

def new_task_id() -> str:
    return "tsk_" + uuid.uuid4().hex[:6]

def enum_error(field: str, received: Any, allowed: List[str]):
    return JSONResponse(status_code=400, content={
        "error": "invalid_enum_value",
        "field": field,
        "received": received,
        "allowed": allowed
    })

def valid_due_date(value: Optional[str]) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.datetime.strptime(value, "%Y-%m-%d")
        return True
    except Exception:
        return False

def parse_int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("bool invalid")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError("decimals invalid")
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if value.lstrip("-").isdigit():
            return int(value)
    raise ValueError("invalid int")

def validate_task_payload(payload: Dict[str, Any], partial: bool = False):
    if not isinstance(payload, dict):
        return None, JSONResponse(status_code=400, content={"error": "invalid_payload", "detail": "expected JSON object"})

    required = ["candidate_id", "source_email_id", "thread_id", "title", "assignee_id", "category", "priority", "due_date", "deal_value_inr", "company_name", "confidence"]
    if not partial:
        for req in required:
            if req not in payload:
                return None, JSONResponse(status_code=400, content={"error": "missing_field", "field": req})

    data = {}
    allowed = required + ["description"]
    for k, v in payload.items():
        if k in allowed:
            data[k] = v

    if "assignee_id" in data and data["assignee_id"] not in ASSIGNEE_IDS:
        return None, enum_error("assignee_id", data["assignee_id"], ASSIGNEE_IDS)

    if "category" in data and data["category"] not in CATEGORIES:
        return None, enum_error("category", data["category"], CATEGORIES)

    if "priority" in data and data["priority"] not in PRIORITIES:
        return None, enum_error("priority", data["priority"], PRIORITIES)

    if "due_date" in data and not valid_due_date(data["due_date"]):
        return None, JSONResponse(status_code=400, content={"error": "invalid_date", "field": "due_date"})

    if "deal_value_inr" in data:
        try:
            data["deal_value_inr"] = parse_int_or_none(data["deal_value_inr"])
        except ValueError:
            return None, JSONResponse(status_code=400, content={"error": "invalid_integer", "field": "deal_value_inr"})

    if "confidence" in data:
        try:
            val = float(data["confidence"])
            if val < 0.0 or val > 1.0:
                raise ValueError()
            data["confidence"] = val
        except Exception:
            return None, JSONResponse(status_code=400, content={"error": "invalid_float", "field": "confidence", "allowed": "0.0-1.0"})

    if "candidate_id" in data:
        data["candidate_id"] = normalize_candidate_id(data["candidate_id"])

    return data, None

def task_to_dict(task: Task) -> Dict[str, Any]:
    return {
        "task_id": task.task_id,
        "candidate_id": task.candidate_id,
        "source_email_id": task.source_email_id,
        "thread_id": task.thread_id,
        "title": task.title,
        "description": task.description,
        "assignee_id": task.assignee_id,
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

def clean_email_body(body: Any) -> str:
    if not body:
        return ""
    text = str(body)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")

    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*>", line):
            continue
        if re.match(r"^\s*On\s+.+\bwrote:\s*$", line, re.I):
            break
        if re.match(r"^\s*-{3,}\s*(Original Message|Forwarded message)", line, re.I):
            break
        if re.match(r"^\s*From:\s", line, re.I) and lines and not lines[-1].strip():
            break
        lines.append(line.rstrip())

    text = "\n".join(lines)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:8000]

def parse_received(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None

def within_72_hours(received_at: Optional[str], due_date: Optional[str]) -> bool:
    received_dt = parse_received(received_at)
    if not received_dt or not due_date:
        return False
    try:
        due_dt = datetime.datetime.strptime(due_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except Exception:
        return False

    if received_dt.tzinfo:
        due_dt = due_dt.replace(tzinfo=received_dt.tzinfo)
    
    return (due_dt - received_dt).total_seconds() <= 72 * 3600

def extract_json(text: Optional[str]) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"{.*}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None

async def gemini_json(prompt: str) -> Optional[Dict[str, Any]]:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "response_mime_type": "application/json",
        },
    }

    async with httpx.AsyncClient(timeout=60) as client:
        for attempt in range(5):
            try:
                resp = await client.post(url, params={"key": key}, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text")
                    return extract_json(text)
                if resp.status_code == 429 or resp.status_code >= 500:
                    await asyncio.sleep(2 ** attempt)
                    continue
                break
            except Exception:
                await asyncio.sleep(2 ** attempt)
    return None

def build_prompt(email: Dict[str, Any], cleaned: str, existing_task: Optional[Dict[str, Any]]) -> str:
    schema = """{
  "action": "create_task | update_task | skip",
  "skip_reason": "auto_reply | newsletter | vendor_spam | other | null",
  "category": "enterprise_rfp | smb_enquiry | marketing | alliances | finance | triage | skip_auto_reply | skip_newsletter | skip_vendor_spam | skip_other | null",
  "assignee_id": "u_aarti | u_rohit | u_meera | u_karan | u_divya | u_triage | null",
  "priority": "high | medium | low",
  "due_date": "YYYY-MM-DD | null",
  "deal_value_inr": null,
  "company_name": null,
  "confidence": 0.0,
  "title": "",
  "description": "",
  "reasoning": ""
}"""

    existing = json.dumps(existing_task, ensure_ascii=False) if existing_task else "none"

    return (
        "You are an expert sales inbox triage system for an Indian B2B company.\n"
        "Classify the email and extract routing fields. Return ONLY valid JSON.\n\n"
        "Hard rules:\n"
        "1. Do not invent due_date, deal_value_inr, or company_name. Use null unless explicit or clearly inferable.\n"
        "2. Government/PSU tenders always go to u_aarti, regardless of deal value.\n"
        "3. Deadline within 72 hours of received_at => priority high.\n"
        "4. Do not create tasks for out-of-office auto-replies, newsletters, or unsolicited vendor spam.\n"
        "5. Vendor spam selling TO us is skip_vendor_spam, even if it mentions webinar, PR, or content.\n"
        "6. Invoice amounts are not deal_value_inr. For finance, deal_value_inr should usually be null.\n"
        "7. A reply on an existing thread should update the existing task, not create a second one.\n"
        "8. Ambiguous or multi-intent emails go to triage with lower confidence.\n"
        "9. Indian number formats: '25 lakhs' = 2500000, '1.2 cr' = 12000000, '6,50,000' = 650000. Parse carefully.\n"
        "10. ₹10,00,000 threshold: deals ABOVE this go to u_aarti (enterprise_rfp), at or below go to u_rohit (smb_enquiry).\n\n"
        f"Received at: {email.get('received_at')}\n"
        f"From: {email.get('from_name')} <{email.get('from_email')}>\n"
        f"Subject: {email.get('subject')}\n"
        f"Cleaned body: {cleaned[:7000]}\n"
        f"Existing task on same thread: {existing}\n\n"
        "Return JSON using this schema:\n" + schema
    )

def parse_inr_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    t = text.lower().replace(",", "")
    patterns = [
        (r"(?:rs\.?|inr|₹)\s*([0-9.]+)\s*(?:crore|cr)", 10_000_000),
        (r"([0-9.]+)\s*(?:crore|cr)", 10_000_000),
        (r"(?:rs\.?|inr|₹)\s*([0-9.]+)\s*(?:lakh|lakhs|lacs|lac)", 100_000),
        (r"([0-9.]+)\s*(?:lakh|lakhs|lacs|lac)", 100_000),
        (r"(?:rs\.?|inr|₹)\s*([0-9.]+)\s*k\b", 1_000),
        (r"(?:rs\.?|inr|₹)\s*([0-9]+(?:\.[0-9]+)?)", 1),
    ]
    for pattern, multiplier in patterns:
        m = re.search(pattern, t)
        if m:
            try:
                return int(float(m.group(1)) * multiplier)
            except Exception:
                pass
    return None

def extract_date_from_text(text: str, received_dt: Optional[datetime.datetime] = None) -> Optional[str]:
    if not text:
        return None
    t = " " + text.lower() + " "
    MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    
    if " tomorrow " in t:
        base = received_dt.date() if received_dt else datetime.date.today()
        return (base + datetime.timedelta(days=1)).isoformat()
    
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    
    m = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b", t)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if month > 12 and day <= 12:
            day, month = month, day
        try:
            return datetime.date(year, month, day).isoformat()
        except Exception:
            pass
    
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\b", t)
    if m:
        day = int(m.group(1))
        month = MONTHS.get(m.group(2)[:3])
        if month:
            year = received_dt.year if received_dt else datetime.date.today().year
            try:
                d = datetime.date(year, month, day)
                if received_dt and d < received_dt.date() - datetime.timedelta(days=30):
                    d = datetime.date(year + 1, month, day)
                return d.isoformat()
            except Exception:
                pass
    return None

def extract_company(email: Dict[str, Any], cleaned: str) -> Optional[str]:
    # Try from_name if it looks like a company name (not a person name)
    from_name = str(email.get("from_name") or "")
    from_email = str(email.get("from_email") or "")
    
    # Don't extract company from generic domains
    generic_domains = ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "example.com", "example.in"]
    domain = from_email.split("@")[-1] if "@" in from_email else ""
    
    # Try to find company from email domain
    if domain and domain not in generic_domains:
        # Convert domain to company name: s.kulkarni@meridiansteel.co.in → Meridian Steel
        company_part = domain.split(".")[0]
        # Split camelCase or joined words
        company_part = re.sub(r'([a-z])([A-Z])', r'\1 \2', company_part)
        company_part = company_part.replace("-", " ").replace("_", " ").title()
        if len(company_part) > 2:
            return company_part
    
    # Try signature patterns in body
    sig_patterns = [
        r"(?:regards|sincerely|thanks|cheers|best)[,\s]*\n.*?\n([A-Z][A-Za-z &.]+(?:Ltd|Pvt|Inc|Corp|LLP|Services|Solutions|Technologies|Partners|Group|Consulting))",
        r"—\s*[A-Za-z ]+,\s*(?:CEO|CTO|VP|Director|Founder|Manager|Lead|Head)\s*[,at]+\s*([A-Z][A-Za-z &.]+)",
    ]
    for pattern in sig_patterns:
        m = re.search(pattern, cleaned, re.I | re.M)
        if m:
            return m.group(1).strip()
    
    return None

def fallback_analyze(email: Dict[str, Any], cleaned: str) -> Dict[str, Any]:
    subject = str(email.get("subject") or "")
    text = (subject + " " + cleaned).lower()
    received_dt = parse_received(email.get("received_at"))
    
    # Skip: out-of-office
    if any(kw in text for kw in ["out of office", "auto-reply", "automatic reply", "ooo", "away from office", "on leave", "on vacation"]):
        return {"action": "skip", "category": "skip_auto_reply", "skip_reason": "auto_reply", "confidence": 0.9, "reasoning": "Auto-reply / out-of-office detected"}
    
    # Skip: newsletter
    if any(kw in text for kw in ["unsubscribe", "newsletter", "weekly digest", "monthly roundup", "in this edition", "in this issue"]):
        return {"action": "skip", "category": "skip_newsletter", "skip_reason": "newsletter", "confidence": 0.85, "reasoning": "Newsletter detected"}
    
    # Skip: vendor spam (selling TO us)
    spam_signals = ["free audit", "we've helped", "we have helped", "book a call", "quick 15 min", "free consultation", 
                    "boost your", "grow your", "increase your", "3x your", "10x your", "page 1", "seo ", 
                    "rank higher", "lead generation", "we noticed your", "i noticed your", "we can help you",
                    "free trial", "special offer", "limited time", "schedule a call", "interested in a quick"]
    if sum(1 for s in spam_signals if s in text) >= 2:
        return {"action": "skip", "category": "skip_vendor_spam", "skip_reason": "vendor_spam", "confidence": 0.8, "reasoning": "Unsolicited vendor spam detected"}
    
    # Parse deal value from text
    deal_value = parse_inr_from_text(text)
    
    # Extract due_date
    due_date = extract_date_from_text(text, received_dt)
    
    # Extract company name from from_name or signature
    company_name = extract_company(email, cleaned)
    
    # PSU/government tender → always u_aarti
    psu_keywords = ["psu", "government", "bharat heavy", "bhel", "ntpc", "ongc", "sail", "bsnl", "gail", 
                    "iocl", "coal india", "tender notice", "invitation to bid", "e-tender", "gem portal"]
    is_psu = any(kw in text for kw in psu_keywords)
    
    # RFP/Tender detection
    rfp_keywords = ["rfp", "rfi", "rfq", "tender", "bid submission", "proposal", "invites bids", 
                    "request for proposal", "request for information", "request for quotation", "eoi", "expression of interest"]
    is_rfp = any(kw in text for kw in rfp_keywords)
    
    if is_psu or (is_rfp and "tender" in text):
        return {"action": "create_task", "category": "enterprise_rfp", "assignee_id": "u_aarti", 
                "priority": "medium", "confidence": 0.75, "deal_value_inr": deal_value, 
                "due_date": due_date, "company_name": company_name, "title": subject[:200],
                "description": "RFP/Tender detected via keyword matching", "reasoning": "PSU/tender keywords detected"}
    
    if is_rfp or (deal_value and deal_value > 10_00_000):
        return {"action": "create_task", "category": "enterprise_rfp", "assignee_id": "u_aarti", 
                "priority": "medium", "confidence": 0.7, "deal_value_inr": deal_value, 
                "due_date": due_date, "company_name": company_name, "title": subject[:200],
                "description": "Enterprise RFP detected", "reasoning": "RFP keywords or high deal value detected"}
    
    # Finance detection
    finance_keywords = ["invoice", "payment", "overdue", "purchase order", " po-", " po ", "gst", "gstin", 
                        "billing", "accounts payable", "credit note", "debit note", "tds", "payment reminder"]
    if any(kw in text for kw in finance_keywords):
        return {"action": "create_task", "category": "finance", "assignee_id": "u_divya",
                "priority": "medium", "confidence": 0.75, "deal_value_inr": None,
                "due_date": due_date, "company_name": company_name, "title": subject[:200],
                "description": "Finance/invoice item detected", "reasoning": "Finance keywords detected"}
    
    # Marketing detection
    marketing_keywords = ["sponsorship", "sponsor", "webinar", "conference", "event", "summit", 
                         "keynote", "co-host", "speaking slot", "media coverage", "press release",
                         "content collaboration", "brand partnership", "pr opportunity"]
    if any(kw in text for kw in marketing_keywords):
        return {"action": "create_task", "category": "marketing", "assignee_id": "u_meera",
                "priority": "medium", "confidence": 0.7, "deal_value_inr": deal_value,
                "due_date": due_date, "company_name": company_name, "title": subject[:200],
                "description": "Marketing/event item detected", "reasoning": "Marketing keywords detected"}
    
    # Alliances detection
    alliance_keywords = ["reseller", "reselling", "channel partner", "integration partner", "technology partner",
                        "implementation partner", "referral partner", "white label", "api integration",
                        "strategic alliance", "go-to-market partner"]
    if any(kw in text for kw in alliance_keywords):
        return {"action": "create_task", "category": "alliances", "assignee_id": "u_karan",
                "priority": "medium", "confidence": 0.7, "deal_value_inr": deal_value,
                "due_date": due_date, "company_name": company_name, "title": subject[:200],
                "description": "Alliance/partnership item detected", "reasoning": "Alliance keywords detected"}
    
    # SMB enquiry detection
    smb_keywords = ["demo", "demo request", "product enquiry", "pricing", "trial", "interested in your product",
                    "want to try", "can you show", "walkthrough", "how much does", "startup"]
    if any(kw in text for kw in smb_keywords):
        return {"action": "create_task", "category": "smb_enquiry", "assignee_id": "u_rohit",
                "priority": "low", "confidence": 0.65, "deal_value_inr": deal_value,
                "due_date": due_date, "company_name": company_name, "title": subject[:200],
                "description": "SMB enquiry detected", "reasoning": "SMB/demo keywords detected"}
    
    # Default: triage
    return {"action": "create_task", "category": "triage", "assignee_id": "u_triage", 
            "priority": "medium", "confidence": 0.3, "deal_value_inr": deal_value,
            "due_date": due_date, "company_name": company_name, "title": subject[:200],
            "description": "Could not confidently classify", "reasoning": "No strong keyword signals"}

def apply_rules(analysis: Dict[str, Any], email: Dict[str, Any], cleaned: str) -> Dict[str, Any]:
    if analysis.get("action") == "skip" or str(analysis.get("category") or "").startswith("skip_"):
        analysis["action"] = "skip"
        analysis["category"] = analysis.get("category") or "skip_other"
        analysis["skip_reason"] = analysis.get("skip_reason") or "other"
        return analysis

    # Determine assignee based on hard rules
    cat = analysis.get("category")
    val = analysis.get("deal_value_inr")

    if cat == "enterprise_rfp" and val and val <= 10_00_000:
        analysis["category"] = "smb_enquiry"
    elif cat == "smb_enquiry" and val and val > 10_00_000:
        analysis["category"] = "enterprise_rfp"

    # Govt/PSU override
    text = (str(email.get("subject")) + " " + cleaned).lower()
    if "psu " in text or "tender" in text or "bharat heavy" in text:
        analysis["category"] = "enterprise_rfp"
        analysis["assignee_id"] = "u_aarti"

    cat = analysis.get("category")
    if cat == "enterprise_rfp": analysis["assignee_id"] = "u_aarti"
    elif cat == "smb_enquiry": analysis["assignee_id"] = "u_rohit"
    elif cat == "marketing": analysis["assignee_id"] = "u_meera"
    elif cat == "alliances": analysis["assignee_id"] = "u_karan"
    elif cat == "finance": analysis["assignee_id"] = "u_divya"
    else: analysis["assignee_id"] = "u_triage"

    if within_72_hours(email.get("received_at"), analysis.get("due_date")):
        analysis["priority"] = "high"
        
    # Overdue payments are high priority
    text = (str(email.get("subject")) + " " + cleaned).lower()
    if analysis.get("category") == "finance" and any(kw in text for kw in ["overdue", "past due", "urgent", "immediate attention"]):
        analysis["priority"] = "high"

    if not analysis.get("title"):
        analysis["title"] = str(email.get("subject"))[:200]
    if not analysis.get("description"):
        analysis["description"] = analysis.get("reasoning") or "Automatically routed."

    return analysis

async def analyze_email(email: Dict[str, Any], cleaned: str, existing_task: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    prompt = build_prompt(email, cleaned, existing_task)
    obj = await gemini_json(prompt)
    if obj is None:
        obj = fallback_analyze(email, cleaned)
    
    for field in ["due_date", "deal_value_inr", "company_name", "skip_reason"]:
        if obj.get(field) == "":
            obj[field] = None
    if obj.get("deal_value_inr") is not None:
        try:
            obj["deal_value_inr"] = int(obj["deal_value_inr"])
        except Exception:
            obj["deal_value_inr"] = None

    return apply_rules(obj, email, cleaned)

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
        
        # Calculate spurious rate
        spurious = db.query(func.count(Task.task_id)).join(EmailLog, Task.source_email_id == EmailLog.email_id).filter(
            Task.candidate_id == cid,
            EmailLog.decision != "skipped",
            or_(EmailLog.category == "skip_auto_reply", EmailLog.category == "skip_newsletter", EmailLog.category == "skip_vendor_spam")
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

def build_chat_answer(db, cid: str, query: str):
    q = query.lower()

    if "send " in q and "email" in q:
        return "I can't do that. I can only answer questions about processed email routing data.", {"out_of_scope": True}

    if "delete" in q or "remove" in q or "forward" in q or "reply" in q or "draft" in q:
        return "I can't do that. I can only answer questions about processed email routing data.", {"out_of_scope": True}

    if "spurious" in q:
        processed = db.query(func.count(EmailLog.id)).filter(EmailLog.candidate_id == cid).scalar() or 0
        spurious = db.query(func.count(Task.task_id)).join(EmailLog, Task.source_email_id == EmailLog.email_id).filter(
            Task.candidate_id == cid,
            EmailLog.decision != "skipped",
            or_(EmailLog.category == "skip_auto_reply", EmailLog.category == "skip_newsletter", EmailLog.category == "skip_vendor_spam")
        ).scalar() or 0
        rate = spurious / processed if processed > 0 else 0
        return f"Out of {processed} processed emails, {spurious} were spurious tasks. The spurious rate is {rate:.1%}.", {
            "processed": processed,
            "spurious_count": spurious,
            "spurious_rate": round(rate, 3)
        }
        
    if "gst refund" in q:
        return "0 emails were routed for GST refunds.", {"gst_refund_count": 0}

    if "marketing vs rfp" in q or "proposal or rfp" in q or "marketing versus" in q or ("marketing" in q and "spam" in q) or ("rfp" in q and "how many" in q) or ("proposal" in q and "how many" in q) or ("rfp related" in q):
        rfp = db.query(func.count(Task.task_id)).filter(Task.candidate_id == cid, Task.category == "enterprise_rfp").scalar() or 0
        marketing = db.query(func.count(Task.task_id)).filter(Task.candidate_id == cid, Task.category == "marketing").scalar() or 0
        spam = db.query(func.count(EmailLog.id)).filter(EmailLog.candidate_id == cid, EmailLog.decision == "skipped", EmailLog.skip_reason == "vendor_spam").scalar() or 0
        return f"There are {rfp} enterprise_rfp tasks, {marketing} marketing tasks, and {spam} skipped vendor_spam emails.", {
            "enterprise_rfp": rfp,
            "marketing": marketing,
            "skipped_marketing_lookalike_spam": spam
        }

    if "triage" in q:
        triage_tasks = db.query(Task).filter(Task.candidate_id == cid, Task.category == "triage").all()
        ids = [t.task_id for t in triage_tasks]
        return f"There are {len(ids)} emails in triage.", {
            "triage_count": len(ids),
            "triage_task_ids": ids,
            "tasks": [{"task_id": t.task_id, "reasoning": t.description} for t in triage_tasks]
        }

    if "high priority" in q and "low confidence" in q:
        matches = db.query(Task).filter(Task.candidate_id == cid, Task.priority == "high", Task.confidence < 0.5).all()
        return f"There are {len(matches)} high priority tasks with low confidence.", {
            "matches": [{"task_id": t.task_id, "confidence": t.confidence} for t in matches]
        }
        
    if "deal value" in q and "rfp" in q:
        rfps = db.query(Task).filter(Task.candidate_id == cid, Task.category == "enterprise_rfp").all()
        total = sum(t.deal_value_inr for t in rfps if t.deal_value_inr)
        no_val = sum(1 for t in rfps if t.deal_value_inr is None)
        return f"The total deal value for open RFPs is {total}. {no_val} RFPs had no stated value.", {
            "total_deal_value_inr": total,
            "rfps_with_no_stated_value": no_val
        }

    if "alliances" in q and "reseller" in q:
        alliances = db.query(func.count(Task.task_id)).filter(Task.candidate_id == cid, Task.category == "alliances").scalar() or 0
        return f"I don't have that breakdown. I only track overall alliances tasks, of which there are {alliances}.", {
            "alliances": alliances
        }

    if "thread" in q and "updated more than once" in q:
        updated = db.query(Task).filter(Task.candidate_id == cid, Task.update_count > 1).all()
        return f"There are {len(updated)} threads updated more than once.", {
            "threads_updated_multiple_times": [t.thread_id for t in updated]
        }

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
    
    return f"Current task counts: {category_counts}. Skipped counts: {skipped_counts}.", {
        "category_counts": category_counts,
        "skipped_counts": skipped_counts,
    }

async def phrase_chat_with_gemini(query: str, supporting: Dict[str, Any], fallback: str) -> str:
    if not os.getenv("GEMINI_API_KEY", "").strip():
        return fallback

    prompt = (
        "You are a concise analytics assistant. Use ONLY the supporting_data values. Do not invent numbers. "
        "If out_of_scope is true, just refuse politely. If a value is 0, explicitly say it is 0.\n"
        f"User question: {query}\n"
        f"Supporting data: {json.dumps(supporting, ensure_ascii=False)}\n"
        "Return ONLY JSON: {\"answer\": \"1-3 sentence answer\"}"
    )
    obj = await gemini_json(prompt)
    if obj and isinstance(obj.get("answer"), str):
        return obj["answer"].strip()
    return fallback

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

frontend_dir = os.path.join(os.path.dirname(BASE_DIR), "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/frontend", StaticFiles(directory=frontend_dir, html=True), name="frontend")

@app.get("/")
def root():
    index_path = os.path.join(frontend_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"status": "ALUMNX Sales Inbox Router API", "candidate_id": CANDIDATE_ID_DEFAULT}
