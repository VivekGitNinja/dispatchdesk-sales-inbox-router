"""
seed_demo.py — Load a realistic demo inbox into any running instance.

A fresh deployment starts with an empty register, which makes a boring first
impression. Run this once (locally or against a deployed backend) and the UI
immediately shows routed tasks, skipped noise, stats, and chat answers.

Usage:
    # Against a local server (default):
    python scripts/seed_demo.py

    # Against a deployed backend:
    API_BASE=https://your-app.onrender.com python scripts/seed_demo.py

    # Under a different tenant/workspace:
    CANDIDATE_ID=ops@acme.in python scripts/seed_demo.py

The dataset is evals/dataset.json — 50 labeled synthetic emails covering all
10 real-world shapes (RFPs, PSU tenders, SMB demos, sponsorships, invoices,
partnerships, OOO, spam, newsletters, multi-intent triage).
"""

import json
import os
import pathlib
import sys

import httpx

BASE = os.getenv("API_BASE", "http://localhost:8000").rstrip("/")
CANDIDATE = os.getenv("CANDIDATE_ID", "demo@dispatchdesk.ai")
DATASET = pathlib.Path(__file__).resolve().parent.parent / "evals" / "dataset.json"


def main() -> int:
    if not DATASET.exists():
        print(f"Dataset not found at {DATASET}", file=sys.stderr)
        return 1

    with open(DATASET, encoding="utf-8") as f:
        dataset = json.load(f)
    emails = dataset["emails"]

    total = {"processed": 0, "tasks_created": 0, "tasks_updated": 0, "skipped": 0}
    with httpx.Client(timeout=600) as client:
        for i in range(0, len(emails), 100):
            batch = emails[i : i + 100]
            resp = client.post(f"{BASE}/ingest", json={"candidate_id": CANDIDATE, "emails": batch})
            resp.raise_for_status()
            result = resp.json()
            for key in total:
                total[key] += result.get(key, 0)

    print(f"Seeded {total['processed']} demo emails for candidate '{CANDIDATE}' at {BASE}")
    print(f"  tasks created: {total['tasks_created']}  |  skipped as noise: {total['skipped']}")
    print("Open the UI — the register should now be populated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
