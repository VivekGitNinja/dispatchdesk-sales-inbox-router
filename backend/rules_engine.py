"""rules_engine.py — the email-analysis pipeline.

Owns:
- Email cleaning / field extraction (INR, dates, companies)
- The deterministic fallback keyword router (no-API-key mode)
- The post-LLM deterministic rules engine (thresholds, overrides, escalations)
- The configurable role → assignee resolution layer
- Gemini integration + prompt building

The LLM proposes, the rules decide. Every decision passes through
`RoutingDecision` (schemas.py) so hallucinated enums never reach the DB.
"""

import asyncio
import datetime
import json
import logging
import os
import re
from typing import Any, Dict, Optional

import httpx

from schemas import Category, Priority, RoutingDecision, TargetRole, role_for_category

logger = logging.getLogger(__name__)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# Priority escalation order — rules only ever escalate, never downgrade.
_PRIORITY_ORDER = {Priority.URGENT.value: 4, Priority.HIGH.value: 3, Priority.MEDIUM.value: 2, Priority.LOW.value: 1}


# ────────────────────────────────────────────────────────────────────────────
# Configurable role → assignee layer
# ────────────────────────────────────────────────────────────────────────────

# Legacy per-category specialist assignees. These preserve the exact historical
# routing behavior (and the eval ground truth). Each category can be overridden
# at deploy time with an env var, e.g. ASSIGNEE_MARKETING=u_new_person.
DEFAULT_ASSIGNEE_BY_CATEGORY: Dict[str, Optional[str]] = {
    Category.ENTERPRISE_RFP.value: "u_aarti",
    Category.SMB_ENQUIRY.value: "u_rohit",
    Category.MARKETING.value: "u_meera",
    Category.ALLIANCES.value: "u_karan",
    Category.FINANCE.value: "u_divya",
    Category.TRIAGE.value: "u_triage",
}

# Role-level overrides take precedence when set, e.g. ROLE_FOUNDER_OPS=ops@acme.in.
_ROLE_ENV = {
    TargetRole.FOUNDER_OPS.value: "ROLE_FOUNDER_OPS",
    TargetRole.SALES_TEAM.value: "ROLE_SALES_TEAM",
    TargetRole.SUPPORT_TEAM.value: "ROLE_SUPPORT_TEAM",
    TargetRole.FINANCE_TEAM.value: "ROLE_FINANCE_TEAM",
}


def assignee_for_category(category: Optional[str]) -> Optional[str]:
    """Resolve the display assignee for a category.

    Priority: per-category env override (ASSIGNEE_<CATEGORY>) →
              role-level env override (ROLE_<TARGETROLE>) →
              legacy default mapping.
    """
    if not category:
        return None
    if category.startswith("skip_"):
        return None

    per_category = os.getenv("ASSIGNEE_" + category.upper(), "").strip()
    if per_category:
        return per_category

    role = role_for_category(category)
    per_role = os.getenv(_ROLE_ENV.get(role.value, ""), "").strip()
    if per_role:
        return per_role

    return DEFAULT_ASSIGNEE_BY_CATEGORY.get(category)


def _escalate(current: Optional[str], target: str) -> str:
    cur = _PRIORITY_ORDER.get(str(current or "").lower(), 0)
    tgt = _PRIORITY_ORDER.get(target, 0)
    return target if tgt >= cur else (current or Priority.MEDIUM.value)


# ────────────────────────────────────────────────────────────────────────────
# Email cleaning / parsing
# ────────────────────────────────────────────────────────────────────────────

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


# ────────────────────────────────────────────────────────────────────────────
# Deterministic fallback router (no-API-key mode)
# ────────────────────────────────────────────────────────────────────────────

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

    # PSU/government tender → always enterprise (u_aarti by default)
    psu_keywords = ["psu", "government", "bharat heavy", "bhel", "ntpc", "ongc", "sail", "bsnl", "gail",
                    "iocl", "coal india", "tender notice", "invitation to bid", "e-tender", "gem portal"]
    is_psu = any(kw in text for kw in psu_keywords)

    # RFP/Tender detection
    rfp_keywords = ["rfp", "rfi", "rfq", "tender", "bid submission", "proposal", "invites bids",
                    "request for proposal", "request for information", "request for quotation", "eoi", "expression of interest"]
    is_rfp = any(kw in text for kw in rfp_keywords)

    # Finance detection
    finance_keywords = ["invoice", "payment", "overdue", "purchase order", " po-", " po ", "gst", "gstin",
                        "billing", "accounts payable", "credit note", "debit note", "tds", "payment reminder"]

    # Marketing detection
    marketing_keywords = ["sponsorship", "sponsor", "webinar", "conference", "event", "summit",
                          "keynote", "co-host", "speaking slot", "media coverage", "press release",
                          "content collaboration", "brand partnership", "pr opportunity"]

    # Alliances detection
    alliance_keywords = ["reseller", "reselling", "channel partner", "integration partner", "technology partner",
                         "implementation partner", "referral partner", "white label", "api integration",
                         "strategic alliance", "go-to-market partner"]

    # SMB enquiry detection
    smb_keywords = ["demo", "demo request", "product enquiry", "pricing", "trial", "interested in your product",
                    "want to try", "can you show", "walkthrough", "how much does", "startup",
                    "evaluate your platform"]

    # ── Multi-intent detection ──────────────────────────────────
    # Emails with two or more distinct asks (e.g. "evaluate your platform AND
    # co-host a webinar") must go to triage as a single task, not be routed
    # to whichever keyword list fires first. Mirrors hard rule 8 in the prompt.
    matched_groups = 0
    if is_psu or is_rfp: matched_groups += 1
    if any(kw in text for kw in finance_keywords): matched_groups += 1
    if any(kw in text for kw in marketing_keywords): matched_groups += 1
    if any(kw in text for kw in alliance_keywords): matched_groups += 1
    if any(kw in text for kw in smb_keywords): matched_groups += 1

    multi_intent_markers = ["and also", "as well as", "two things", "two asks", "two requests",
                            "loop in the right people", "can you loop", "(1)", "(2)", "in addition"]
    is_multi_intent = matched_groups >= 2 or any(m in text for m in multi_intent_markers)

    if is_multi_intent:
        return {"action": "create_task", "category": "triage", "assignee_id": "u_triage",
                "priority": "medium", "confidence": 0.3, "deal_value_inr": deal_value,
                "due_date": due_date, "company_name": company_name, "title": subject[:200],
                "description": "Multi-intent or ambiguous email requiring human review",
                "reasoning": "Multiple distinct asks detected; routed to triage"}

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

    if any(kw in text for kw in finance_keywords):
        return {"action": "create_task", "category": "finance", "assignee_id": "u_divya",
                "priority": "medium", "confidence": 0.75, "deal_value_inr": None,
                "due_date": due_date, "company_name": company_name, "title": subject[:200],
                "description": "Finance/invoice item detected", "reasoning": "Finance keywords detected"}

    if any(kw in text for kw in marketing_keywords):
        return {"action": "create_task", "category": "marketing", "assignee_id": "u_meera",
                "priority": "medium", "confidence": 0.7, "deal_value_inr": deal_value,
                "due_date": due_date, "company_name": company_name, "title": subject[:200],
                "description": "Marketing/event item detected", "reasoning": "Marketing keywords detected"}

    if any(kw in text for kw in alliance_keywords):
        return {"action": "create_task", "category": "alliances", "assignee_id": "u_karan",
                "priority": "medium", "confidence": 0.7, "deal_value_inr": deal_value,
                "due_date": due_date, "company_name": company_name, "title": subject[:200],
                "description": "Alliance/partnership item detected", "reasoning": "Alliance keywords detected"}

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


# ────────────────────────────────────────────────────────────────────────────
# Deterministic post-LLM rules engine
# ────────────────────────────────────────────────────────────────────────────

def apply_rules(analysis: Dict[str, Any], email: Dict[str, Any], cleaned: str) -> Dict[str, Any]:
    """Enforce hard business rules AFTER any extraction (LLM or fallback).

    Order of operations:
    1. Noise passthrough (skip_* stays zero-task idempotent, assignee = NONE)
    2. ₹10,00,000 threshold reclassification
    3. Govt/PSU tender override → enterprise + URGENT
    4. Role resolution → display assignee (configurable)
    5. Priority escalation (72h deadline, overdue finance) — never downgrades
    """
    if analysis.get("action") == "skip" or str(analysis.get("category") or "").startswith("skip_"):
        analysis["action"] = "skip"
        analysis["category"] = analysis.get("category") or "skip_other"
        analysis["skip_reason"] = analysis.get("skip_reason") or "other"
        analysis["assignee_id"] = None
        analysis["target_role"] = role_for_category(analysis["category"]).value
        return analysis

    cat = analysis.get("category")
    val = analysis.get("deal_value_inr")

    # ₹10L threshold: enterprise ↔ smb reclassification
    if cat == Category.ENTERPRISE_RFP.value and val and val <= 10_00_000:
        analysis["category"] = Category.SMB_ENQUIRY.value
    elif cat == Category.SMB_ENQUIRY.value and val and val > 10_00_000:
        analysis["category"] = Category.ENTERPRISE_RFP.value

    # Govt/PSU tender override → enterprise + URGENT
    text = (str(email.get("subject")) + " " + cleaned).lower()
    if "psu " in text or "tender" in text or "bharat heavy" in text:
        analysis["category"] = Category.ENTERPRISE_RFP.value
        analysis["priority"] = Priority.URGENT.value

    cat = analysis.get("category")
    # Assign the role first, then resolve the display assignee from it.
    analysis["target_role"] = role_for_category(cat).value
    analysis["assignee_id"] = assignee_for_category(cat)

    # Priority escalation — only ever escalates, never downgrades.
    if within_72_hours(email.get("received_at"), analysis.get("due_date")):
        analysis["priority"] = _escalate(analysis.get("priority"), Priority.HIGH.value)

    # Overdue payments are high priority
    text = (str(email.get("subject")) + " " + cleaned).lower()
    if analysis.get("category") == Category.FINANCE.value and any(kw in text for kw in ["overdue", "past due", "urgent", "immediate attention"]):
        analysis["priority"] = _escalate(analysis.get("priority"), Priority.HIGH.value)

    if not analysis.get("title"):
        analysis["title"] = str(email.get("subject"))[:200]
    if not analysis.get("description"):
        analysis["description"] = analysis.get("reasoning") or "Automatically routed."

    return analysis


# ────────────────────────────────────────────────────────────────────────────
# LLM integration (Gemini) + prompt
# ────────────────────────────────────────────────────────────────────────────

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
  "priority": "urgent | high | medium | low",
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
        "2. Government/PSU tenders always go to enterprise_rfp (u_aarti), regardless of deal value.\n"
        "3. Deadline within 72 hours of received_at => priority high; government/PSU tenders => priority urgent.\n"
        "4. Do not create tasks for out-of-office auto-replies, newsletters, or unsolicited vendor spam.\n"
        "5. Vendor spam selling TO us is skip_vendor_spam, even if it mentions webinar, PR, or content.\n"
        "6. Invoice amounts are not deal_value_inr. For finance, deal_value_inr should usually be null.\n"
        "7. A reply on an existing thread should update the existing task, not create a second one.\n"
        "8. Ambiguous or multi-intent emails go to triage with lower confidence.\n"
        "9. Indian number formats: '25 lakhs' = 2500000, '1.2 cr' = 12000000, '6,50,000' = 650000. Parse carefully.\n"
        "10. ₹10,00,000 threshold: deals ABOVE this go to enterprise_rfp (u_aarti), at or below go to smb_enquiry (u_rohit).\n\n"
        f"Received at: {email.get('received_at')}\n"
        f"From: {email.get('from_name')} <{email.get('from_email')}>\n"
        f"Subject: {email.get('subject')}\n"
        f"Cleaned body: {cleaned[:7000]}\n"
        f"Existing task on same thread: {existing}\n\n"
        "Return JSON using this schema:\n" + schema
    )


# ────────────────────────────────────────────────────────────────────────────
# Orchestration: LLM proposes → schema-validate → rules decide → re-validate
# ────────────────────────────────────────────────────────────────────────────

async def analyze_email(email: Dict[str, Any], cleaned: str, existing_task: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze one email end-to-end. Never raises on bad LLM output.

    - Gemini (when available) returns a dict → validated through
      RoutingDecision; any ValidationError drops the LLM result and falls back
      to the deterministic keyword router (zero hallucinated records).
    - `apply_rules` enforces business rules.
    - The final decision is re-validated before being returned; if it still
      fails the schema (should never happen), the fallback runs once more.
    """
    prompt = build_prompt(email, cleaned, existing_task)
    obj = await gemini_json(prompt)
    if obj is not None:
        try:
            obj = RoutingDecision.model_validate(obj).model_dump()
        except Exception as exc:  # pydantic.ValidationError
            logger.warning("LLM output failed schema validation; using deterministic fallback router: %s", exc)
            obj = None
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

    obj = apply_rules(obj, email, cleaned)

    # Safety net: the post-rules decision must still conform to the schema.
    try:
        obj = RoutingDecision.model_validate(obj).model_dump()
    except Exception as exc:  # pydantic.ValidationError
        logger.warning("Post-rules decision invalid; re-running fallback router: %s", exc)
        try:
            obj = apply_rules(fallback_analyze(email, cleaned), email, cleaned)
            obj = RoutingDecision.model_validate(obj).model_dump()
        except Exception as exc2:  # absolute last resort — never raise, never persist garbage
            logger.error("Fallback decision also invalid; returning safe triage decision: %s", exc2)
            obj = {
                "action": "create_task", "category": Category.TRIAGE.value,
                "assignee_id": "u_triage", "target_role": TargetRole.SUPPORT_TEAM.value,
                "priority": Priority.MEDIUM.value, "confidence": 0.1,
                "due_date": None, "deal_value_inr": None, "company_name": None,
                "title": str(email.get("subject"))[:200],
                "description": "Safe fallback: unable to produce a valid routing decision",
                "reasoning": "Both LLM and fallback produced invalid decisions; routed to triage",
                "skip_reason": None,
            }

    return obj
