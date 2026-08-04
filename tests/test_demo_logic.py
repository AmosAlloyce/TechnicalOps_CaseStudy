import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from agents.triage_agent import rule_based_routing
from lambdas.escalation.handler import is_outside_business_hours, parse_ticket_datetime


def ticket(**overrides):
    base = {
        "ticket_id": "TEST-001",
        "category": "Billing - Invoice Question",
        "priority": "medium",
        "internal_notes": "Customer needs help understanding an invoice.",
        "is_enterprise": False,
        "account_name": "General Account",
    }
    base.update(overrides)
    return base


class DemoRoutingTests(unittest.TestCase):
    def test_transient_import_is_auto_resolved(self):
        result = rule_based_routing(
            ticket(
                category="Magic Import - Error",
                priority="low",
                internal_notes="Processing failed after a transient timeout. Retry usually works.",
            ),
            fallback_used=False,
        )
        self.assertEqual(result["routing"], "auto_resolve")
        self.assertTrue(result["is_retry_pattern"])
        self.assertFalse(result["fallback_used"])

    def test_file_size_failure_is_not_auto_resolved(self):
        result = rule_based_routing(
            ticket(
                category="Magic Import - Error",
                priority="medium",
                internal_notes="Upload failed because the file size is over 50MB.",
            ),
            fallback_used=False,
        )
        self.assertEqual(result["routing"], "standard_queue")
        self.assertFalse(result["is_retry_pattern"])
        self.assertIn("50MB", result["draft_response"])

    def test_enterprise_retry_requires_human_review(self):
        result = rule_based_routing(
            ticket(
                category="Magic Import - Error",
                priority="low",
                internal_notes="Transient timeout; retry resolved the prior attempt.",
                is_enterprise=True,
                account_name="DataForge",
            ),
            fallback_used=False,
        )
        self.assertEqual(result["routing"], "enterprise_queue")

    def test_high_priority_enterprise_ticket_escalates(self):
        result = rule_based_routing(
            ticket(
                category="Account - Access",
                priority="high",
                internal_notes="The full customer organisation is locked out.",
                is_enterprise=True,
                account_name="CoreVista",
            ),
            fallback_used=False,
        )
        self.assertEqual(result["routing"], "escalate")


class BusinessHoursTests(unittest.TestCase):
    def test_csv_timestamp_is_parsed_in_business_timezone(self):
        parsed = parse_ticket_datetime("2026-01-02 19:45")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, ZoneInfo("America/New_York"))

    def test_evening_ticket_is_after_hours(self):
        parsed = parse_ticket_datetime("2026-01-02 19:45")
        self.assertTrue(is_outside_business_hours(parsed))

    def test_daytime_ticket_is_not_after_hours(self):
        parsed = datetime(2026, 1, 2, 11, 3, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(is_outside_business_hours(parsed))


if __name__ == "__main__":
    unittest.main()
