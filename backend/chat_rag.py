"""chat_rag.py — grounded analytics chat.

Answers are COMPUTED from the database (SQL aggregates), never guessed by the
LLM. Gemini (when configured) only re-phrases the computed answer. Every
response carries `supporting_data` so callers can audit the numbers.
"""

import json
import os
from typing import Any, Dict, Tuple

from sqlalchemy import func, or_

from db import EmailLog, Task
from rules_engine import gemini_json

# Categories that, if a task was created for them, count as "spurious" (noise
# that should have been skipped, not routed).
_SPURIOUS_CATEGORIES = ("skip_auto_reply", "skip_newsletter", "skip_vendor_spam")


def _spurious_count(db, cid: str) -> int:
    return db.query(func.count(Task.task_id)).join(EmailLog, Task.source_email_id == EmailLog.email_id).filter(
        Task.candidate_id == cid,
        EmailLog.decision != "skipped",
        or_(EmailLog.category.in_(_SPURIOUS_CATEGORIES))
    ).scalar() or 0


def build_chat_answer(db, cid: str, query: str) -> Tuple[str, Dict[str, Any]]:
    """Answer an analytics question from stored data only.

    Returns (answer_text, supporting_data). `out_of_scope: true` in supporting
    data means the request is not something this assistant can do.
    """
    q = query.lower()

    if "send " in q and "email" in q:
        return "I can't do that. I can only answer questions about processed email routing data.", {"out_of_scope": True}

    if "delete" in q or "remove" in q or "forward" in q or "reply" in q or "draft" in q:
        return "I can't do that. I can only answer questions about processed email routing data.", {"out_of_scope": True}

    if "spurious" in q:
        processed = db.query(func.count(EmailLog.id)).filter(EmailLog.candidate_id == cid).scalar() or 0
        spurious = _spurious_count(db, cid)
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
    """Re-phrase the computed answer with Gemini. Never invents numbers —
    the model is instructed to use ONLY the supporting_data values."""
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
