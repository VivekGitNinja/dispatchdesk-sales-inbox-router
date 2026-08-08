import json
import pathlib

TEMPLATES = [
    {
        "subject": "RFP - Enterprise DMS",
        "body": "Meridian Steel invites proposals for enterprise DMS. Budget Rs. 25 lakhs. Proposals due 12th August 2026.",
        "from_name": "Suresh Kulkarni",
        "from_email": "s.kulkarni@meridiansteel.co.in",
        "task_expected": True,
        "expected_assignee_id": "u_aarti",
        "expected_category": "enterprise_rfp"
    },
    {
        "subject": "Demo request",
        "body": "Hi, we are a 30-person logistics startup in Pune. Can we get a demo sometime next week? Nothing urgent. Ankit Bose, Founder, Railyard Logistics",
        "from_name": "Ankit Bose",
        "from_email": "ankit@railyardlogistics.in",
        "task_expected": True,
        "expected_assignee_id": "u_rohit",
        "expected_category": "smb_enquiry"
    },
    {
        "subject": "PSU tender",
        "body": "Tender Notice BHEL/PROC/2026/0847. Bharat Heavy Electricals Limited invites bids. Estimated value Rs. 6,50,000. Last date 03-08-2026.",
        "from_name": "BHEL Procurement",
        "from_email": "procurement@bhel.example.in",
        "task_expected": True,
        "expected_assignee_id": "u_aarti",
        "expected_category": "enterprise_rfp"
    },
    {
        "subject": "Sponsorship",
        "body": "We are finalising sponsors for India SaaS Summit. Gold tier is Rs. 4,00,000. Need confirmation by tomorrow EOD.",
        "from_name": "Nandita Reddy",
        "from_email": "nandita@saassummit.in",
        "task_expected": True,
        "expected_assignee_id": "u_meera",
        "expected_category": "marketing"
    },
    {
        "subject": "Invoice overdue",
        "body": "Please find attached invoice INV-2026-0331 for Rs. 1,18,000 against PO-88214. Payment is 12 days overdue.",
        "from_name": "Vantage Cloud Services",
        "from_email": "billing@vantagecloud.example",
        "task_expected": True,
        "expected_assignee_id": "u_divya",
        "expected_category": "finance"
    },
    {
        "subject": "Reseller partnership",
        "body": "We are a Salesforce implementation partner. We would like to explore reselling your platform or technical integration.",
        "from_name": "Zenith Cloud Partners",
        "from_email": "partnerships@zenithcloud.example",
        "task_expected": True,
        "expected_assignee_id": "u_karan",
        "expected_category": "alliances"
    },
    {
        "subject": "Out of Office",
        "body": "I am out of office until 14th August with limited access to email.",
        "from_name": "Auto Reply",
        "from_email": "auto@example.com",
        "task_expected": False,
        "expected_assignee_id": None,
        "expected_category": "skip_auto_reply"
    },
    {
        "subject": "SEO audit",
        "body": "We noticed your website is not ranking. We do content marketing, PR outreach, webinar promotion. Interested in a quick call?",
        "from_name": "Growth Agency",
        "from_email": "hello@growthagency.example",
        "task_expected": False,
        "expected_assignee_id": None,
        "expected_category": "skip_vendor_spam"
    },
    {
        "subject": "Newsletter",
        "body": "The B2B Growth Weekly Issue #212. In this edition: pricing experiments. Unsubscribe.",
        "from_name": "Newsletter",
        "from_email": "news@example.com",
        "task_expected": False,
        "expected_assignee_id": None,
        "expected_category": "skip_newsletter"
    },
    {
        "subject": "Two asks",
        "body": "We want to evaluate your platform for our 800-person org and also co-host a webinar. Can you loop in the right people?",
        "from_name": "Farhan Qureshi",
        "from_email": "farhan@halcyonretail.example",
        "task_expected": True,
        "expected_assignee_id": "u_triage",
        "expected_category": "triage"
    }
]

emails = []
labels = []

for i in range(50):
    t = TEMPLATES[i % len(TEMPLATES)]
    email_id = f"em_eval_{i + 1:03d}"
    thread_id = f"th_eval_{i + 1:03d}"
    received_at = f"2026-08-{(i % 5) + 1:02d}T10:00:00+05:30"
    
    email = {
        "email_id": email_id,
        "thread_id": thread_id,
        "message_index": 0,
        "from_name": t["from_name"],
        "from_email": t["from_email"],
        "to": "sales@company.com",
        "cc": [],
        "subject": t["subject"],
        "body": t["body"],
        "received_at": received_at,
        "attachments": [],
        "is_reply": False
    }
    emails.append(email)
    
    labels.append({
        "email_id": email_id,
        "thread_id": thread_id,
        "expected_task": t["task_expected"],
        "expected_assignee_id": t["expected_assignee_id"],
        "expected_category": t["expected_category"]
    })

out = {"emails": emails, "labels": labels}
pathlib.Path("evals").mkdir(exist_ok=True)
with open("evals/dataset.json", "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)

print("Wrote evals/dataset.json with 50 labeled synthetic emails.")
