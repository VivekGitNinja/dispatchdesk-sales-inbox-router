# EVALS.md

## Evaluation method

Run `python scripts/make_eval_dataset.py` followed by `python scripts/run_eval.py`.

The dataset contains 50 labeled synthetic emails covering all 10 template types from the PRD (enterprise RFP, SMB enquiry, PSU tender, marketing sponsorship, overdue invoice, alliance partnership, out-of-office, vendor spam, newsletter, multi-intent triage). The eval script runs all three grading runs:

- **Run 1**: Initial ingest → verify routing accuracy
- **Run 2**: Re-ingest identical batch → verify idempotency
- **Run 3**: Send thread reply → verify reconciliation

The overall gate requires **all** of the following (this is a real gate, not a smoke test):

- Run 1 **F1 ≥ 0.95** — every expected email becomes a task and no noise becomes one
- Run 1 **category accuracy ≥ 0.95** — each task lands in the right category
- Run 1 **assignee accuracy ≥ 0.95** — each task reaches the right person
- Run 2 and Run 3 pass
- All 6 chat tests pass, including a real non-zero assertion on triage count

F1 alone cannot see routing errors (a task created with the wrong category/assignee still counts as a true positive), which is why category and assignee accuracy are gated separately. `evals/results.json` records every run, and CI fails if `overall_passed` is false.

## Latest results (fallback mode, no Gemini API key, fresh DB)

### Run 1: Routing accuracy

| Category         | TP | FN | FP | Correct assignee | Correct category |
|------------------|----|----|----|------------------|------------------|
| enterprise_rfp   | 10 |  0 |  0 | 10               | 10               |
| smb_enquiry      |  5 |  0 |  0 | 5                | 5                |
| marketing        |  5 |  0 |  0 | 5                | 5                |
| alliances        |  5 |  0 |  0 | 5                | 5                |
| finance          |  5 |  0 |  0 | 5                | 5                |
| triage           |  5 |  0 |  0 | 5                | 5                |

**Overall:**
- **Precision:** 1.0000
- **Recall:** 1.0000
- **F1 Score:** 1.0000
- **Assignee Accuracy:** 1.0000
- **Category Accuracy:** 1.0000
- **Spurious rate:** 0.0 (0 spurious tasks out of 35 created)

### Run 2: Idempotency

- New tasks created on re-ingest: **0**
- Tasks updated on re-ingest: **0**
- Task count unchanged: **True**
- **PASSED** ✅

### Run 3: Thread reconciliation

- Thread tasks before reply: **1**
- Thread tasks after reply: **1**
- Task was updated: **True**
- **PASSED** ✅

### Chat tests

| Question | Result |
|----------|--------|
| How many emails this batch were proposal or RFP related? | ✅ PASS |
| How many were marketing versus actual spam we correctly ignored? | ✅ PASS |
| Show me everything sitting in triage and why. (asserts non-zero triage) | ✅ PASS |
| What is our spurious rate so far? | ✅ PASS |
| How many emails were about GST refunds? (zero-count trap) | ✅ PASS |
| Send Aarti an email about the Meridian Steel RFP. (out-of-scope) | ✅ PASS |

## Change history

- **v2 (current):** Multi-intent emails (e.g. "evaluate your platform AND co-host a webinar") are detected by the fallback router and routed to **triage** instead of marketing. This fixed the 0.8571 category/assignee accuracy gap (5 of 35 tasks were previously misrouted). Unit tests in `tests/test_fallback_routing.py` lock this behavior in. Chat tests now assert real values (non-zero triage) instead of key presence.

- **v1 (original):** Category/assignee accuracy were 0.8571 because all 5 multi-intent emails were routed to marketing by the `webinar`/`co-host` keyword branch before reaching the triage default. The F1=1.0 headline masked this because F1 only measures "was a task created".

## Known limitations (honest)

- Fallback mode is keyword-based; nuanced Hinglish or vendor spam disguised as marketing can still slip through. Gemini mode (with `GEMINI_API_KEY`) is more robust but was not part of this fallback run.
- The eval dataset is synthetic and templated (each base template repeats 10×). Real-world email variety will be messier.
- Multi-intent emails are kept as a **single** triage task rather than split into two tasks — a deliberate product decision (see DECISIONS.md #6).
