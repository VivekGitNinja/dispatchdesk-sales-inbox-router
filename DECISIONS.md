# DECISIONS.md

1. **LLM extraction plus deterministic rules engine**: Gemini classifies and extracts fields, but Python applies the final routing rules. This prevents the LLM from blindly applying the wrong rule in conflict cases such as PSU tenders below ₹10,00,000. Tradeoff: more code and extra post-processing, but much safer routing.

2. **SQLite default, Postgres in production**: SQLite makes local setup easy. Production uses DATABASE_URL for Postgres/Supabase because in-memory state or ephemeral SQLite can fail Run 2 and Run 3 after cold restart. Tradeoff: requires setting DATABASE_URL for real deployment.

3. **Thread reconciliation by thread_id**: If a task already exists for a thread, non-skipped emails on that thread update the existing task instead of creating a new task. update_count is incremented to support the chat question about threads updated more than once. Tradeoff: if a thread genuinely contains two separate asks, this system may still keep one task and route ambiguous cases to triage.

4. **Chat grounded in SQL, not LLM**: The chat endpoint computes numbers from the database first via targeted SQL queries. Gemini only phrases the answer — it never invents numbers. If Gemini is unavailable, deterministic text is returned. The zero-count trap ("How many GST refunds?") returns 0 explicitly from SQL, and out-of-scope requests ("Send Aarti an email") are refused before hitting the LLM. Tradeoff: less conversational without Gemini, but the numbers cannot hallucinate.

5. **Heuristic fallback when Gemini fails**: If Gemini rate limits or fails, a keyword-based fallback routes emails across all 6 categories using domain-specific patterns (RFP/tender keywords, INR amount parsing, finance terms, marketing signals, alliance indicators). Vendor spam is detected via signal scoring (≥2 spam phrases). This avoids dropped emails. Tradeoff: fallback is less accurate for nuanced Hinglish or vendor spam disguised as marketing.

6. **Multi-intent emails → one triage task**: Multi-intent emails (e.g., "evaluate your platform AND co-host a webinar") are sent to triage as a single task instead of being split into two tasks routed to different owners. Splitting requires understanding task boundaries reliably, which the LLM does inconsistently; the safer default is one triage task a human can split manually. The cost is that Farhan-style emails take longer to reach the right people. Enforced in both paths: the Gemini prompt (hard rule 8) and, since v2, the deterministic fallback router (multi-intent detection) so the rule holds even without an API key.
