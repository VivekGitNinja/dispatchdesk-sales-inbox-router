# TEST PLAN — ALUMNX Sales Inbox Router

## Test Scope

All automated tests are run via `python scripts/run_eval.py` against a local server instance. Tests cover the 3 grading runs specified in the PRD plus chat interface validation.

---

## Acceptance Tests

### AT-01: Email routing accuracy (Run 1)
- **Input:** 50 labeled synthetic emails across 10 categories
- **Expected:** Precision ≥ 0.85, Recall ≥ 0.90, F1 ≥ 0.87
- **Actual:** Precision=1.0, Recall=1.0, F1=1.0
- **Status:** ✅ PASS

### AT-02: Idempotency (Run 2)
- **Input:** Re-ingest same 50 emails
- **Expected:** 0 new tasks, 0 updates, task count unchanged
- **Actual:** 0 new tasks, 0 updates, count unchanged
- **Status:** ✅ PASS

### AT-03: Thread reconciliation (Run 3)
- **Input:** Thread reply on existing thread `th_eval_001`
- **Expected:** No new task, existing task updated, update_count incremented
- **Actual:** 1→1 tasks, updated=True
- **Status:** ✅ PASS

### AT-04: Chat - RFP count question
- **Query:** "How many emails this batch were proposal or RFP related?"
- **Expected:** Returns count with `enterprise_rfp` key
- **Status:** ✅ PASS

### AT-05: Chat - Marketing vs spam
- **Query:** "How many were marketing versus actual spam we correctly ignored?"
- **Expected:** Returns marketing count and vendor_spam count
- **Status:** ✅ PASS

### AT-06: Chat - Triage items
- **Query:** "Show me everything sitting in triage and why."
- **Expected:** Returns triage tasks with task IDs and reasoning
- **Status:** ✅ PASS

### AT-07: Chat - Spurious rate
- **Query:** "What is our spurious rate so far?"
- **Expected:** Returns numeric rate
- **Status:** ✅ PASS

### AT-08: Chat - Zero-count trap
- **Query:** "How many emails were about GST refunds?"
- **Expected:** Returns "0" explicitly
- **Status:** ✅ PASS

### AT-09: Chat - Out-of-scope refusal
- **Query:** "Send Aarti an email about the Meridian Steel RFP."
- **Expected:** Refuses with "can't do that"
- **Status:** ✅ PASS

---

## Edge-Case Tests

### EC-01: PSU tender below ₹10L goes to u_aarti
- **Template:** BHEL tender, Rs. 6,50,000
- **Expected:** enterprise_rfp / u_aarti (PSU override trumps value threshold)
- **Verified in:** Run 1 dataset

### EC-02: Overdue invoice gets high priority
- **Template:** "Invoice INV-2026-0331 overdue"
- **Expected:** finance / u_divya / priority=high
- **Verified in:** apply_rules overdue check

### EC-03: Vendor spam with webinar mention still skipped
- **Template:** "Free SEO audit... content marketing, PR outreach, webinar promotion"
- **Expected:** skip_vendor_spam (≥2 spam signals: "free audit", "we've helped")
- **Verified in:** Run 1 dataset

### EC-04: Multi-intent email goes to triage
- **Template:** "evaluate platform AND co-host webinar"
- **Expected:** triage / u_triage
- **Verified in:** Run 1 dataset

### EC-05: Thread reply doesn't create duplicate task
- **Verified in:** Run 3

### EC-06: Hinglish email with "1.2 cr" budget
- **Template:** "Budget approx 1.2 cr allocated hai"
- **Expected:** enterprise_rfp / u_aarti (amount > ₹10L)
- **Verified in:** parse_inr_from_text logic

---

## Security Tests

### SEC-01: XSS prevention in frontend
- **Test:** `escapeText()` escapes `<`, `>`, `&`, `"`
- **Status:** ✅ Fixed

### SEC-02: No secrets in codebase
- **Test:** grep for API keys, passwords, tokens in committed files
- **Status:** ✅ .env in .gitignore; .env.example has no real values

### SEC-03: SQL injection resistance
- **Test:** SQLAlchemy ORM parameterizes all queries — no raw SQL
- **Status:** ✅ All queries use ORM

### SEC-04: Input validation on Task API
- **Test:** Invalid enum returns 400; invalid float returns 400; missing field returns 400
- **Status:** ✅ validate_task_payload covers all

---

## Performance Tests

### PERF-01: Batch processing throughput
- **Test:** 250 emails in 3 batches of 100
- **Expected:** Completes within 60s (fallback mode)
- **Actual:** ~3 seconds in fallback mode
- **Status:** ✅ PASS

---

## Manual Validation Steps

1. Open `http://localhost:8000` in browser
2. Click "Generate 250 Sample Emails" → verify preview table renders
3. Click "Run Ingest" → verify tasks and skipped tables populate
4. Click each chat quick button → verify correct responses
5. Upload a custom JSON file → verify it parses and ingests
6. Re-click "Run Ingest" → verify no new tasks created (idempotency)
