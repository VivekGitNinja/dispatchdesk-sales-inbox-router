"""Tests for the extraction helpers: parse_inr_from_text, extract_date_from_text, extract_company."""
import datetime

from conftest import backend


class TestParseINR:
    def test_lakhs(self):
        assert backend.parse_inr_from_text("Indicative budget is Rs. 25 lakhs.") == 2_500_000

    def test_crores(self):
        assert backend.parse_inr_from_text("Budget approx 1.2 cr allocated") == 12_000_000

    def test_inr_symbol(self):
        assert backend.parse_inr_from_text("Gold tier is ₹4,00,000 and includes a keynote.") == 400_000

    def test_plain_amount(self):
        assert backend.parse_inr_from_text("Rs. 1,18,000 (incl. 18% GST)") == 118_000

    def test_none_when_absent(self):
        assert backend.parse_inr_from_text("No amounts mentioned here.") is None

    def test_k_suffix(self):
        assert backend.parse_inr_from_text("Rs. 50k budget") == 50_000


class TestExtractDate:
    def test_iso(self):
        assert backend.extract_date_from_text("Deadline: 2026-08-12") == "2026-08-12"

    def test_dd_mm_yyyy(self):
        assert backend.extract_date_from_text("Last date for bid submission: 03-08-2026") == "2026-08-03"

    def test_ordinal_month(self):
        received = datetime.datetime(2026, 8, 5, 10, 0)
        assert backend.extract_date_from_text("Proposals must reach us by 12th August 2026.", received) == "2026-08-12"

    def test_tomorrow(self):
        received = datetime.datetime(2026, 8, 5, 10, 0)
        assert backend.extract_date_from_text("We need confirmation by tomorrow EOD.", received) == "2026-08-06"

    def test_none_when_absent(self):
        assert backend.extract_date_from_text("No deadline here.") is None


class TestExtractCompany:
    def test_from_domain(self):
        email = {"from_name": "Suresh Kulkarni", "from_email": "s.kulkarni@meridiansteel.co.in"}
        assert backend.extract_company(email, "") == "Meridiansteel"

    def test_generic_domain_falls_through(self):
        email = {"from_name": "Ankit Bose", "from_email": "ankit@gmail.com"}
        assert backend.extract_company(email, "") is None
