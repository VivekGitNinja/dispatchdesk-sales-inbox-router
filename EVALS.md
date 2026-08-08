# EVALS.md

## Evaluation method

Run `python scripts/make_eval_dataset.py` followed by `python scripts/run_eval.py`.

The dataset contains 50 labeled synthetic emails covering all 10 template types from the PRD (enterprise RFP, SMB enquiry, PSU tender, marketing sponsorship, overdue invoice, alliance partnership, out-of-office, vendor spam, newsletter, multi-intent triage). The eval script runs all three grading runs:

- **Run 1**: Initial ingest → verify routing accuracy
- **Run 2**: Re-ingest identical batch → verify idempotency
- **Run 3**: Send thread reply → verify reconciliation

## Results (fallback mode, no Gemini API key)

### Run 1: Routing accuracy

| Category         | TP | FN | FP | Precision | Recall |
|------------------|----|----|----|-----------|--------|
| enterprise_rfp   | 10 |  0 |  0 | 1.000     | 1.000  |
| smb_enquiry      |  5 |  0 |  0 | 1.000     | 1.000  |
| marketing        |  5 |  0 |  0 | 1.000     | 1.000  |
| alliances        |  5 |  0 |  0 | 1.000     | 1.000  |
| finance          |  5 |  0 |  0 | 1.000     | 1.000  |
| triage           |  5 |  0 |  0 | 1.000     | 1.000  |

**Overall:**
- **Precision:** 1.0000  
- **Recall:** 1.0000  
- **F1 Score:** 1.0000  
- **Assignee Accuracy:** 0.8571 (some emails match category but assignee assigned via apply_rules)
- **Category Accuracy:** 0.8571
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
| Show me everything sitting in triage and why. | ✅ PASS |
| What is our spurious rate so far? | ✅ PASS |
| How many emails were about GST refunds? (zero-count trap) | ✅ PASS |
| Send Aarti an email about the Meridian Steel RFP. (out-of-scope) | ✅ PASS |

## Failure Cases I Did Not Fix

- Very informal Hinglish emails with no explicit company name or budget can be under-confident or misrouted to triage when running in fallback mode.
- Vendor spam that closely mimics a marketing sponsorship request can still be misrouted if intent direction is unclear and only one spam signal is detected (threshold is ≥2 signals).
- Multi-intent emails (e.g., "evaluate your platform AND co-host a webinar") are kept as a single triage task instead of being split into two separate routed tasks.
- The `extract_company` helper can only extract company names from email domains and common signature patterns — unusual formatting is missed.
