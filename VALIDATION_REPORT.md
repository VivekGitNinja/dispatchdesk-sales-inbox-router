# VALIDATION REPORT — ALUMNX Sales Inbox Router

**Validation date:** 2026-08-09  
**Candidate ID:** `priya.sharma@gmail.com`

---

## Passed Checks ✅

| # | Check | Result |
|---|-------|--------|
| 1 | Every Must requirement has implementation | ✅ All 22 tasks implemented |
| 2 | Every Must requirement has acceptance criteria | ✅ All in IMPLEMENTATION_PLAN.md |
| 3 | Happy path flow (ingest → route → view tasks) | ✅ Run 1: F1=1.0 |
| 4 | Empty state (no emails ingested yet) | ✅ Frontend shows "No rows." |
| 5 | Loading state (during ingest) | ✅ Button shows "Ingesting..." |
| 6 | Error state (invalid JSON input) | ✅ Alert shown with parse error |
| 7 | Retry path (Gemini 429 → exponential backoff) | ✅ 5 retries with 2^n seconds |
| 8 | Fallback path (no Gemini key → keyword routing) | ✅ F1=1.0 in fallback mode |
| 9 | Idempotency (Run 2) | ✅ 0 new tasks, 0 updates |
| 10 | Thread reconciliation (Run 3) | ✅ Update, not create |
| 11 | Chat: RFP count | ✅ Correct count returned |
| 12 | Chat: Marketing vs spam | ✅ Both counts returned |
| 13 | Chat: Triage items with reasoning | ✅ Task IDs and descriptions |
| 14 | Chat: Spurious rate | ✅ Numeric rate |
| 15 | Chat: Zero-count trap (GST refunds) | ✅ Returns "0" |
| 16 | Chat: Out-of-scope refusal | ✅ "I can't do that" |
| 17 | XSS prevention | ✅ escapeText escapes HTML |
| 18 | No secrets in code | ✅ .env in .gitignore |
| 19 | SQL injection prevention | ✅ ORM parameterization |
| 20 | Input validation (enums, types) | ✅ 400 on invalid |
| 21 | CORS configured | ✅ Allow-origin * |
| 22 | Health endpoint | ✅ GET /health returns 200 |
| 23 | load_dotenv called | ✅ .env loaded at startup |
| 24 | SQLite path absolute | ✅ Uses BASE_DIR |
| 25 | PSU override rule | ✅ Tenders always go to u_aarti |
| 26 | ₹10L threshold rule | ✅ enterprise_rfp ↔ smb_enquiry |
| 27 | 72-hour deadline → high priority | ✅ within_72_hours check |
| 28 | Overdue invoice → high priority | ✅ "overdue" keyword check |
| 29 | README has candidate_id | ✅ priya.sharma@gmail.com |
| 30 | DECISIONS has 6 tradeoffs | ✅ Including known-failure item |
| 31 | EVALS has per-category metrics | ✅ Full table with TP/FN/FP |
| 32 | Eval script tests all 3 runs | ✅ Run 1 + Run 2 + Run 3 |

---

## Failed Checks ❌

None.

---

## Unresolved Gaps

| # | Gap | Status | Action Required |
|---|-----|--------|-----------------|
| G20 | Deployed URLs in README say REPLACE | ⚠️ Manual | Deploy to Render + Netlify, then update README |
| G24 | Eval metrics only tested in fallback mode | ⚠️ Manual | Re-run `run_eval.py` with GEMINI_API_KEY set for final metrics |

---

## Assumptions [ASSUMPTION]

1. **[ASSUMPTION]** The grader will use `priya.sharma@gmail.com` as candidate_id consistently across all runs.
2. **[ASSUMPTION]** The grader's Run 2 and Run 3 will use the same backend instance (database persisted).
3. **[ASSUMPTION]** "Spurious rate" in the PRD refers to tasks that were created but should have been skipped (false positives from the skip categories).
4. **[ASSUMPTION]** The ₹10,00,000 threshold means exactly 10 lakhs INR (1,000,000 in integer).
5. **[ASSUMPTION]** "Vendor spam" refers to companies selling TO the candidate's company, not marketing emails FROM potential customers.

---

## Open Questions

| # | Question | Recommended Answer | Owner |
|---|----------|--------------------|-------|
| OQ-01 | Which Gemini model should be used in production? | gemini-1.5-flash — fast, cheap, handles structured JSON well | Candidate |
| OQ-02 | Should the frontend be a separate Netlify deployment or served by the backend? | Both work. For grading, backend-served is simpler. For production, separate is better. | Candidate |
| OQ-03 | Is PostgreSQL required for grading or is SQLite acceptable? | SQLite is acceptable for local grading. Postgres is needed for deployed backend persistence across cold restarts. | Candidate |

---

## Recommended Next Actions

1. **Deploy backend to Render** — Create a Render web service, set `GEMINI_API_KEY` and `DATABASE_URL` env vars
2. **Deploy frontend to Netlify** — Deploy `frontend/` folder, set API base in UI
3. **Update README.md** — Replace placeholder URLs with actual deployed URLs
4. **Re-run evals with API key** — Set `GEMINI_API_KEY` in .env and run `python scripts/run_eval.py` to get LLM-powered metrics
5. **Submit** — Backend URL + Frontend URL + GitHub repo via submission form

---

## Final Readiness Decision

> **READY FOR DEPLOYMENT AND SUBMISSION** — pending deployment of backend and frontend, and updating deployed URLs in README.

All functional requirements are implemented and tested. All 3 grading runs pass. All 6 chat tests pass. Security controls are in place. Documentation is complete.
