"""schemas.py — Pydantic v2 models, enums, taxonomy constants, and payload validation.

Single source of truth for every string enum in the system:
- Category (routing buckets + skip/noise categories)
- Priority (urgent/high/medium/low)
- TargetRole (configurable routing roles)
- RoutingDecision (the validated LLM output contract)

Nothing in this module imports from the rest of the backend, so it can be
imported anywhere without circular-dependency risk.
"""

import datetime
import os
import re
from typing import Any, Dict, List, Optional

from enum import Enum

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


# ────────────────────────────────────────────────────────────────────────────
# Enums
# ────────────────────────────────────────────────────────────────────────────

class Category(str, Enum):
    """Valid routing categories. Values are lowercase for DB / eval compatibility."""

    ENTERPRISE_RFP = "enterprise_rfp"
    SMB_ENQUIRY = "smb_enquiry"
    MARKETING = "marketing"
    ALLIANCES = "alliances"
    FINANCE = "finance"
    TRIAGE = "triage"
    SKIP_AUTO_REPLY = "skip_auto_reply"
    SKIP_NEWSLETTER = "skip_newsletter"
    SKIP_VENDOR_SPAM = "skip_vendor_spam"
    SKIP_OTHER = "skip_other"


class Priority(str, Enum):
    """Priority levels. `urgent` is the highest escalation tier."""

    URGENT = "urgent"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TargetRole(str, Enum):
    """Primary routing targets. Display assignees resolve from a role (configurable)."""

    FOUNDER_OPS = "FOUNDER_OPS"
    SALES_TEAM = "SALES_TEAM"
    SUPPORT_TEAM = "SUPPORT_TEAM"
    FINANCE_TEAM = "FINANCE_TEAM"
    NONE = "NONE"


# ────────────────────────────────────────────────────────────────────────────
# Taxonomy constants (derived from the enums — never duplicate strings)
# ────────────────────────────────────────────────────────────────────────────

CATEGORIES: List[str] = [c.value for c in Category if not c.value.startswith("skip_")]
SKIP_CATEGORIES: List[str] = [c.value for c in Category if c.value.startswith("skip_")]
PRIORITIES: List[str] = [p.value for p in Priority]
ASSIGNEE_IDS: List[str] = ["u_aarti", "u_rohit", "u_meera", "u_karan", "u_divya", "u_triage"]

# Category → primary routing role (configurable default mapping).
ROLE_BY_CATEGORY: Dict[str, TargetRole] = {
    Category.ENTERPRISE_RFP: TargetRole.FOUNDER_OPS,
    Category.SMB_ENQUIRY: TargetRole.SALES_TEAM,
    Category.MARKETING: TargetRole.SALES_TEAM,
    Category.ALLIANCES: TargetRole.SALES_TEAM,
    Category.FINANCE: TargetRole.FINANCE_TEAM,
    Category.TRIAGE: TargetRole.SUPPORT_TEAM,
    Category.SKIP_AUTO_REPLY: TargetRole.NONE,
    Category.SKIP_NEWSLETTER: TargetRole.NONE,
    Category.SKIP_VENDOR_SPAM: TargetRole.NONE,
    Category.SKIP_OTHER: TargetRole.NONE,
}


def role_for_category(category: Optional[str]) -> TargetRole:
    """Resolve the TargetRole for a category (unknown categories → support/triage)."""
    if not category:
        return TargetRole.SUPPORT_TEAM
    return ROLE_BY_CATEGORY.get(category, TargetRole.SUPPORT_TEAM)


def normalize_candidate_id(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


# ────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ────────────────────────────────────────────────────────────────────────────

class RoutingDecision(BaseModel):
    """Validated contract for an email-routing decision.

    This is what the LLM output is validated against AND what the deterministic
    pipeline produces. Any hallucinated enum, out-of-range confidence, or
    malformed type raises a pydantic ValidationError which the caller converts
    into a graceful fallback to the keyword router.
    """

    action: str = "create_task"  # create_task | update_task | skip
    skip_reason: Optional[str] = None
    category: Optional[Category] = None
    assignee_id: Optional[str] = None
    priority: Priority = Priority.MEDIUM
    due_date: Optional[str] = None
    deal_value_inr: Optional[int] = None
    company_name: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    title: str = ""
    description: str = ""
    reasoning: str = ""
    target_role: Optional[TargetRole] = None


class ChatRequest(BaseModel):
    candidate_id: Optional[str] = None
    query: str


# ────────────────────────────────────────────────────────────────────────────
# Payload validation helpers (manual task API)
# ────────────────────────────────────────────────────────────────────────────

def enum_error(field: str, received: Any, allowed: List[str]):
    return JSONResponse(status_code=400, content={
        "error": "invalid_enum_value",
        "field": field,
        "received": received,
        "allowed": allowed,
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
    """Validate a manual POST/PATCH /tasks payload.

    Returns (data, None) on success or (None, JSONResponse) on failure.
    Mirrors the historical behavior exactly (enum/type checks with 400s).
    """
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
