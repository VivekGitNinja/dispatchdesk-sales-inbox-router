"""Tests for the new schema layer: RoutingDecision validation, role resolution,
and the URGENT priority / escalation rules introduced in the refactor.
"""
import pytest
from pydantic import ValidationError

from conftest import backend


class TestRoutingDecisionValidation:
    def test_valid_llm_output_passes(self):
        from schemas import RoutingDecision
        decision = RoutingDecision.model_validate({
            "action": "create_task",
            "category": "enterprise_rfp",
            "priority": "high",
            "confidence": 0.85,
            "deal_value_inr": 2500000,
            "due_date": "2026-08-12",
            "company_name": "Meridian Steel",
            "title": "RFP - Enterprise DMS",
            "reasoning": "RFP detected",
        })
        assert decision.category.value == "enterprise_rfp"
        assert decision.confidence == 0.85

    def test_hallucinated_category_rejected(self):
        from schemas import RoutingDecision
        with pytest.raises(ValidationError):
            RoutingDecision.model_validate({"category": "make_money_fast", "priority": "high"})

    def test_invalid_priority_rejected(self):
        from schemas import RoutingDecision
        with pytest.raises(ValidationError):
            RoutingDecision.model_validate({"category": "finance", "priority": "CRITICAL"})

    def test_out_of_range_confidence_rejected(self):
        from schemas import RoutingDecision
        with pytest.raises(ValidationError):
            RoutingDecision.model_validate({"category": "triage", "confidence": 1.5})

    def test_urgent_priority_is_valid(self):
        from schemas import RoutingDecision
        decision = RoutingDecision.model_validate({"category": "enterprise_rfp", "priority": "urgent"})
        assert decision.priority.value == "urgent"


class TestRoleResolution:
    def test_role_for_enterprise_is_founder_ops(self):
        from schemas import role_for_category
        assert role_for_category("enterprise_rfp").value == "FOUNDER_OPS"

    def test_role_for_finance_is_finance_team(self):
        from schemas import role_for_category
        assert role_for_category("finance").value == "FINANCE_TEAM"

    def test_skip_maps_to_none_role(self):
        from schemas import role_for_category
        assert role_for_category("skip_vendor_spam").value == "NONE"

    def test_default_assignee_preserves_legacy_mapping(self):
        from rules_engine import assignee_for_category
        assert assignee_for_category("enterprise_rfp") == "u_aarti"
        assert assignee_for_category("marketing") == "u_meera"
        assert assignee_for_category("alliances") == "u_karan"
        assert assignee_for_category("skip_newsletter") is None

    def test_category_env_override(self, monkeypatch):
        from rules_engine import assignee_for_category
        monkeypatch.setenv("ASSIGNEE_MARKETING", "u_campaigns")
        assert assignee_for_category("marketing") == "u_campaigns"

    def test_role_env_override(self, monkeypatch):
        from rules_engine import assignee_for_category
        monkeypatch.setenv("ROLE_FOUNDER_OPS", "ops@acme.in")
        assert assignee_for_category("enterprise_rfp") == "ops@acme.in"


class TestUrgentPriority:
    def test_psu_tender_is_urgent(self):
        analysis = {"action": "create_task", "category": "marketing", "deal_value_inr": None}
        out = backend.apply_rules(analysis, {"subject": "Bharat Heavy tender", "received_at": None}, "")
        assert out["priority"] == "urgent"
        assert out["target_role"] == "FOUNDER_OPS"
        assert out["category"] == "enterprise_rfp"

    def test_urgent_is_never_downgraded_by_72h_rule(self):
        # A PSU tender (urgent) with a due date beyond 72h must stay urgent.
        analysis = {"action": "create_task", "category": "enterprise_rfp", "priority": "urgent",
                    "due_date": "2026-08-20"}
        email = {"subject": "Tender Notice", "received_at": "2026-08-01T10:00:00+05:30"}
        out = backend.apply_rules(analysis, email, "")
        assert out["priority"] == "urgent"

    def test_72h_deadline_escalates_medium_to_high(self):
        analysis = {"action": "create_task", "category": "finance", "priority": "medium",
                    "due_date": "2026-08-02"}
        email = {"subject": "Invoice", "received_at": "2026-08-01T10:00:00+05:30"}
        out = backend.apply_rules(analysis, email, "")
        assert out["priority"] == "high"

    def test_high_is_not_downgraded_to_medium(self):
        analysis = {"action": "create_task", "category": "finance", "priority": "high",
                    "due_date": "2026-08-10"}
        email = {"subject": "Invoice", "received_at": "2026-08-01T10:00:00+05:30"}
        out = backend.apply_rules(analysis, email, "")
        assert out["priority"] == "high"


class TestAnalyzeNeverRaises:
    def test_analyze_with_bad_llm_shape_uses_fallback(self, monkeypatch):
        # Monkeypatch gemini_json to return a hallucinated dict; analyze_email
        # must fall back to the keyword router instead of raising/persisting garbage.
        import asyncio
        import rules_engine

        async def bad_llm(prompt):
            return {"category": "not_a_real_category", "priority": "FABULOUS", "confidence": 99}
        monkeypatch.setattr(rules_engine, "gemini_json", bad_llm)

        email = {
            "subject": "RFP - Enterprise DMS",
            "body": "Meridian Steel invites proposals. Budget Rs. 25 lakhs. Due 12th August 2026.",
            "from_name": "Suresh Kulkarni",
            "from_email": "s.kulkarni@meridiansteel.co.in",
            "received_at": "2026-08-01T10:00:00+05:30",
        }
        decision = asyncio.run(backend.analyze_email(email, backend.clean_email_body(email["body"]), None))
        assert decision["category"] == "enterprise_rfp"
        assert decision["assignee_id"] == "u_aarti"
        assert decision["priority"] in ("medium", "high", "urgent")

    def test_analyze_with_invalid_json_shape_uses_fallback(self, monkeypatch):
        # gemini_json returns None (e.g. invalid JSON, API failure) → fallback router.
        import asyncio
        import rules_engine

        async def bad_llm(prompt):
            return None
        monkeypatch.setattr(rules_engine, "gemini_json", bad_llm)

        email = {
            "subject": "Invoice INV-2026-0331 overdue",
            "body": "Invoice for Rs. 1,18,000 is 12 days overdue.",
            "from_name": "Vantage Cloud",
            "from_email": "billing@vantagecloud.example",
            "received_at": "2026-08-05T10:00:00+05:30",
        }
        decision = asyncio.run(backend.analyze_email(email, backend.clean_email_body(email["body"]), None))
        assert decision["category"] == "finance"
        assert decision["assignee_id"] == "u_divya"
