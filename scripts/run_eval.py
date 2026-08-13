"""
run_eval.py — Evaluates the DispatchDesk Sales Inbox Router against 3 grading runs:
  Run 1: Ingest → route → verify categories/assignees
  Run 2: Re-ingest same batch → verify idempotency (0 new tasks, 0 new updates)
  Run 3: Ingest thread reply → verify thread reconciliation (update, not new task)
"""

import json
import os
import sys
import httpx

BASE = os.getenv("API_BASE", "http://localhost:8000").rstrip("/")
CANDIDATE = os.getenv("CANDIDATE_ID", "demo@dispatchdesk.ai")

def ingest(emails):
    resp = httpx.post(f"{BASE}/ingest", json={"candidate_id": CANDIDATE, "emails": emails}, timeout=600)
    resp.raise_for_status()
    return resp.json()

def get_tasks():
    resp = httpx.get(f"{BASE}/tasks", params={"candidate_id": CANDIDATE}, timeout=120)
    resp.raise_for_status()
    return resp.json()

def chat(query):
    resp = httpx.post(f"{BASE}/api/chat", json={"candidate_id": CANDIDATE, "query": query}, timeout=120)
    resp.raise_for_status()
    return resp.json()

# ────────────────────── Load dataset ──────────────────────
with open("evals/dataset.json", encoding="utf-8") as f:
    dataset = json.load(f)

emails = dataset["emails"]
labels = {label["email_id"]: label for label in dataset["labels"]}

results = {"candidate_id": CANDIDATE, "runs": {}}

# ═══════════════════ RUN 1: Initial ingest ═══════════════════
print("═══ RUN 1: Initial ingest ═══")
r1 = ingest(emails)
print(f"  processed={r1['processed']}  tasks_created={r1['tasks_created']}  "
      f"tasks_updated={r1['tasks_updated']}  skipped={r1['skipped']}")

tasks = get_tasks()
tasks_by_email = {t["source_email_id"]: t for t in tasks}

tp, fp, fn = 0, 0, 0
correct_assignee = 0
correct_category = 0
created_tasks = 0
expected_tasks = 0
per_category = {}

for email_id, label in labels.items():
    task = tasks_by_email.get(email_id)
    expected_task = label["expected_task"]
    expected_category = label["expected_category"]

    if expected_task:
        expected_tasks += 1
    if expected_category not in per_category:
        per_category[expected_category] = {"tp": 0, "fn": 0, "fp": 0, "correct_assignee": 0, "correct_category": 0}

    if expected_task and task:
        tp += 1
        created_tasks += 1
        per_category[expected_category]["tp"] += 1
        if task.get("assignee_id") == label.get("expected_assignee_id"):
            correct_assignee += 1
            per_category[expected_category]["correct_assignee"] += 1
        if task.get("category") == expected_category:
            correct_category += 1
            per_category[expected_category]["correct_category"] += 1
    elif expected_task and not task:
        fn += 1
        per_category[expected_category]["fn"] += 1
    elif not expected_task and task:
        fp += 1
        created_tasks += 1

precision = tp / (tp + fp) if (tp + fp) else 0.0
recall = tp / (tp + fn) if (tp + fn) else 0.0
f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
assignee_accuracy = correct_assignee / tp if tp else 0.0
category_accuracy = correct_category / tp if tp else 0.0

run1 = {
    "expected_tasks": expected_tasks,
    "created_tasks": created_tasks,
    "true_positive": tp,
    "false_positive": fp,
    "missed": fn,
    "precision": round(precision, 4),
    "recall": round(recall, 4),
    "f1": round(f1, 4),
    "assignee_accuracy": round(assignee_accuracy, 4),
    "category_accuracy": round(category_accuracy, 4),
    "per_category": per_category,
}
results["runs"]["run1_routing"] = run1
print(f"  precision={precision:.4f}  recall={recall:.4f}  f1={f1:.4f}")
print(f"  assignee_accuracy={assignee_accuracy:.4f}  category_accuracy={category_accuracy:.4f}")

# ═══════════════════ RUN 2: Idempotency test ═══════════════════
print("\n═══ RUN 2: Re-ingest same batch (idempotency) ═══")
r2 = ingest(emails)
tasks_after_r2 = get_tasks()

idempotent_new_tasks = r2["tasks_created"]
idempotent_updates = r2["tasks_updated"]
idempotent_ok = (idempotent_new_tasks == 0 and idempotent_updates == 0)
task_count_same = len(tasks_after_r2) == len(tasks)

run2 = {
    "new_tasks_created": idempotent_new_tasks,
    "tasks_updated": idempotent_updates,
    "idempotent": idempotent_ok,
    "task_count_before": len(tasks),
    "task_count_after": len(tasks_after_r2),
    "task_count_unchanged": task_count_same,
    "passed": idempotent_ok and task_count_same,
}
results["runs"]["run2_idempotency"] = run2
print(f"  new_tasks={idempotent_new_tasks}  updates={idempotent_updates}  "
      f"count_unchanged={task_count_same}  PASSED={run2['passed']}")

# ═══════════════════ RUN 3: Thread reconciliation ═══════════════════
print("\n═══ RUN 3: Thread reply reconciliation ═══")

# Find a thread that has a task (use the first enterprise_rfp template thread)
thread_reply_email = {
    "email_id": "em_eval_reply_001",
    "thread_id": "th_eval_001",  # Same thread as first email
    "message_index": 1,
    "from_name": "Suresh Kulkarni",
    "from_email": "s.kulkarni@meridiansteel.co.in",
    "to": "sales@company.com",
    "cc": [],
    "subject": "Re: RFP - Enterprise DMS",
    "body": "Correction: budget revised to Rs. 32 lakhs. Deadline advanced to 11th August.",
    "received_at": "2026-08-06T10:00:00+05:30",
    "attachments": [],
    "is_reply": True
}

# Get task count before
tasks_before_r3 = get_tasks()
task_for_thread = [t for t in tasks_before_r3 if t["thread_id"] == "th_eval_001"]

r3 = ingest([thread_reply_email])

tasks_after_r3 = get_tasks()
task_for_thread_after = [t for t in tasks_after_r3 if t["thread_id"] == "th_eval_001"]

thread_task_count_before = len(task_for_thread)
thread_task_count_after = len(task_for_thread_after)
no_new_task = (thread_task_count_after == thread_task_count_before)
was_updated = r3.get("tasks_updated", 0) > 0
total_unchanged = len(tasks_after_r3) == len(tasks_before_r3)

run3 = {
    "thread_tasks_before": thread_task_count_before,
    "thread_tasks_after": thread_task_count_after,
    "no_new_task_created": no_new_task,
    "task_was_updated": was_updated,
    "total_task_count_unchanged": total_unchanged,
    "passed": no_new_task and was_updated,
}
results["runs"]["run3_thread_reconciliation"] = run3
print(f"  thread_tasks_before={thread_task_count_before}  after={thread_task_count_after}  "
      f"updated={was_updated}  PASSED={run3['passed']}")

# ═══════════════════ CHAT TESTS ═══════════════════
print("\n═══ CHAT TESTS ═══")
chat_tests = [
    {"query": "How many emails this batch were proposal or RFP related?", "expect_key": "enterprise_rfp", "expect_nonzero": True},
    {"query": "How many were marketing versus actual spam we correctly ignored?", "expect_key": "marketing", "expect_nonzero": True},
    {"query": "Show me everything sitting in triage and why.", "expect_key": "triage_count", "expect_nonzero": True},
    {"query": "What is our spurious rate so far?", "expect_key": "spurious_rate", "expect_nonzero": False},
    {"query": "How many emails were about GST refunds?", "expect_key": "gst_refund_count", "expect_zero": True},
    {"query": "Send Aarti an email about the Meridian Steel RFP.", "expect_key": "out_of_scope", "expect_refusal": True},
]

chat_results = []
for ct in chat_tests:
    resp = chat(ct["query"])
    answer = resp.get("answer", "")
    supporting = resp.get("supporting_data", {})
    passed = True
    
    if ct.get("expect_refusal"):
        passed = "can't" in answer.lower() or "cannot" in answer.lower() or "don't" in answer.lower()
    elif ct.get("expect_zero"):
        passed = "0" in answer
    elif ct.get("expect_nonzero"):
        key = ct["expect_key"]
        val = supporting.get(key) if isinstance(supporting, dict) else None
        # Real assertion: the supporting value must exist AND be a positive number.
        passed = isinstance(val, (int, float)) and val > 0
    
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {ct['query'][:60]}...")
    chat_results.append({"query": ct["query"], "passed": passed, "answer": answer[:200]})

results["chat_tests"] = chat_results

# ═══════════════════ SUMMARY ═══════════════════
print("\n═══ SUMMARY ═══")
# Hard gates: F1 AND routing quality (category + assignee) must both be at or
# above 0.95. A task created with the wrong category/assignee is a real error,
# and F1 alone cannot see it (it only measures "was a task created").
ACCURACY_THRESHOLD = 0.95
routing_ok = run1["f1"] >= 0.95 and run1["category_accuracy"] >= ACCURACY_THRESHOLD and run1["assignee_accuracy"] >= ACCURACY_THRESHOLD
all_passed = (
    routing_ok and
    run2["passed"] and
    run3["passed"] and
    all(c["passed"] for c in chat_results)
)
results["overall_passed"] = all_passed
print(f"  Run 1 F1={run1['f1']:.4f}  category_acc={run1['category_accuracy']:.4f}  assignee_acc={run1['assignee_accuracy']:.4f}  (gate >= {ACCURACY_THRESHOLD})")
print(f"  Run 2 Idempotent={run2['passed']}  Run 3 Thread={run3['passed']}  Chat={sum(c['passed'] for c in chat_results)}/{len(chat_results)}")
print(f"  ROUTING GATE: {'PASS' if routing_ok else 'FAIL'}")
print(f"  OVERALL: {'PASS' if all_passed else 'FAIL'}")

with open("evals/results.json", "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)

print(f"\nFull results written to evals/results.json")
