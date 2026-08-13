"""Tests for fallback_analyze — the deterministic keyword router used when Gemini is unavailable.

These mirror the eval dataset templates so the fallback is proven to route every
category correctly (the known gap: multi-intent emails were being sent to marketing
instead of triage).
"""
from conftest import backend


def analyze(subject, body, from_name="Test Sender", from_email="sender@company.example"):
    email = {
        "subject": subject,
        "body": body,
        "from_name": from_name,
        "from_email": from_email,
        "received_at": "2026-08-05T10:00:00+05:30",
    }
    return backend.fallback_analyze(email, backend.clean_email_body(body))


def route(subject, body, **kwargs):
    result = analyze(subject, body, **kwargs)
    return backend.apply_rules(result, {"subject": subject, "received_at": "2026-08-05T10:00:00+05:30"}, backend.clean_email_body(body))


class TestSkips:
    def test_out_of_office(self):
        r = analyze("Out of Office", "I am out of office until 14th August.")
        assert r["action"] == "skip" and r["category"] == "skip_auto_reply"

    def test_newsletter(self):
        r = analyze("The B2B Growth Weekly", "In this edition: pricing experiments. [Unsubscribe]")
        assert r["action"] == "skip" and r["category"] == "skip_newsletter"

    def test_vendor_spam(self):
        r = analyze("Free SEO audit", "I noticed your website isn't ranking. We've helped 200+ SaaS 3x traffic. Interested in a quick 15 min call?")
        assert r["action"] == "skip" and r["category"] == "skip_vendor_spam"


class TestRouting:
    def test_enterprise_rfp(self):
        r = route("RFP - Enterprise DMS", "Meridian Steel invites proposals for an enterprise DMS. Budget Rs. 25 lakhs.")
        assert r["category"] == "enterprise_rfp" and r["assignee_id"] == "u_aarti"

    def test_psu_tender(self):
        r = route("Tender Notice No. BHEL/PROC/2026/0847", "Bharat Heavy Electricals invites bids. Estimated value: Rs. 6,50,000.")
        assert r["category"] == "enterprise_rfp" and r["assignee_id"] == "u_aarti"

    def test_smb_enquiry(self):
        r = route("Quick demo request", "Can we get a demo sometime next week? Nothing urgent.")
        assert r["category"] == "smb_enquiry" and r["assignee_id"] == "u_rohit"

    def test_marketing(self):
        r = route("Sponsorship confirmation needed", "Gold tier is ₹4,00,000 and includes a keynote slot.")
        assert r["category"] == "marketing" and r["assignee_id"] == "u_meera"

    def test_finance(self):
        r = route("Invoice INV-2026-0331 overdue", "Invoice for Rs. 1,18,000 against PO-88214 is 12 days overdue.")
        assert r["category"] == "finance" and r["assignee_id"] == "u_divya"

    def test_alliances(self):
        r = route("Partnership opportunity", "We'd like to explore reselling your platform, or a technical integration.")
        assert r["category"] == "alliances" and r["assignee_id"] == "u_karan"


class TestMultiIntent:
    """The eval dataset's 5 triage emails are all the 'Two asks' template. These must
    land in triage, not marketing (the bug that dragged accuracy to 0.8571)."""

    MULTI_BODY = "We want to evaluate your platform for our 800-person org and also co-host a webinar. Can you loop in the right people?"

    def test_multi_intent_goes_to_triage(self):
        r = route("Two asks", self.MULTI_BODY)
        assert r["category"] == "triage", f"expected triage, got {r['category']}"
        assert r["assignee_id"] == "u_triage"

    def test_multi_intent_low_confidence(self):
        r = route("Two asks", self.MULTI_BODY)
        assert r["confidence"] is not None and r["confidence"] < 0.5

    def test_single_intent_marketing_still_routes_to_marketing(self):
        # Regression guard: a plain sponsorship email must NOT trip multi-intent detection.
        r = route("Sponsorship confirmation needed", "We're finalising sponsors for the SaaS Summit. Gold tier is ₹4,00,000.")
        assert r["category"] == "marketing" and r["assignee_id"] == "u_meera"


class TestApplyRules:
    def test_low_value_rfp_downgraded_to_smb(self):
        analysis = {"action": "create_task", "category": "enterprise_rfp", "deal_value_inr": 500_000}
        out = backend.apply_rules(analysis, {"subject": "", "received_at": None}, "")
        assert out["category"] == "smb_enquiry" and out["assignee_id"] == "u_rohit"

    def test_high_value_smb_upgraded_to_rfp(self):
        analysis = {"action": "create_task", "category": "smb_enquiry", "deal_value_inr": 2_000_000}
        out = backend.apply_rules(analysis, {"subject": "", "received_at": None}, "")
        assert out["category"] == "enterprise_rfp" and out["assignee_id"] == "u_aarti"

    def test_psu_override(self):
        analysis = {"action": "create_task", "category": "marketing", "deal_value_inr": None}
        out = backend.apply_rules(analysis, {"subject": "Bharat Heavy tender", "received_at": None}, "")
        assert out["category"] == "enterprise_rfp" and out["assignee_id"] == "u_aarti"

    def test_unknown_category_defaults_to_triage(self):
        analysis = {"action": "create_task", "category": "triage", "deal_value_inr": None}
        out = backend.apply_rules(analysis, {"subject": "", "received_at": None}, "")
        assert out["assignee_id"] == "u_triage"
