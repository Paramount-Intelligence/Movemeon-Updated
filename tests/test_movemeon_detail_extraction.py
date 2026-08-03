"""Unit tests for MoveMeOn detail-page field extraction and merge behavior."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extraction

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _load_fixture(name: str) -> str:
    with open(os.path.join(FIXTURES_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


class FixtureBodyExtractionTests(unittest.TestCase):
    """extract_movemeon_detail_fields_from_body against Label: value fixture."""

    def setUp(self):
        self.body = _load_fixture("detail_movemeon_sample.txt")
        self.details = extraction.extract_movemeon_detail_fields_from_body(
            self.body, title="Interim Finance Director"
        )

    def test_description_extracted(self):
        description = (self.details.get("description") or "").lower()
        self.assertIn("finance director", description)
        self.assertGreater(len(description), 30)

    def test_location_extracted(self):
        self.assertEqual(self.details.get("location"), "London, United Kingdom")

    def test_compensation_extracted(self):
        self.assertIsNotNone(self.details.get("budget_text"))
        self.assertEqual(self.details.get("billing_type"), "daily")
        self.assertEqual(self.details.get("daily_rate"), 700.0)

    def test_engagement_type_extracted(self):
        self.assertEqual(self.details.get("engagement_type"), "Contract")

    def test_skills_extracted(self):
        skills = self.details.get("skills") or []
        self.assertIn("Financial Modelling", skills)
        self.assertIn("FP&A", skills)

    def test_application_deadline_extracted(self):
        self.assertEqual(self.details.get("application_deadline"), "30/09/2026")

    def test_category_not_exposed(self):
        self.assertEqual(
            self.details.get("platform_category_extraction_status"), "NOT_EXPOSED"
        )
        self.assertIsNone(self.details.get("platform_category"))
        missing = self.details.get("missing_fields") or []
        self.assertNotIn("platform_category", missing)

    def test_overall_status_is_complete_for_fully_visible_fixture(self):
        self.assertEqual(self.details.get("detail_extraction_status"), "COMPLETE")


class LiveStyleConsecutiveLabelTests(unittest.TestCase):
    """MoveMeOn Details panel uses label then value on consecutive lines."""

    def setUp(self):
        self.body = _load_fixture("detail_movemeon_live_style.txt")
        self.details = extraction.extract_movemeon_detail_fields_from_body(
            self.body, title="Leadership Advisor"
        )

    def test_salary_range_parsed(self):
        self.assertEqual(self.details.get("budget_text"), "£60K - £90K")
        self.assertEqual(self.details.get("budget_min"), 60000.0)
        self.assertEqual(self.details.get("budget_max"), 90000.0)
        self.assertEqual(self.details.get("budget_currency"), "GBP")
        self.assertEqual(self.details.get("billing_type"), "fixed_range")

    def test_permanent_engagement(self):
        self.assertEqual(self.details.get("engagement_type"), "Permanent")

    def test_company_in_raw_data(self):
        self.assertEqual(
            (self.details.get("raw_data") or {}).get("movemeon_company"),
            "Exceptional Leadership Technology",
        )

    def test_industry_and_function(self):
        self.assertIn("Boutique consultancy", self.details.get("industry") or "")
        self.assertEqual(
            self.details.get("workstream"), "Organisation and People, Strategy"
        )

    def test_remote_flexibility(self):
        self.assertEqual(self.details.get("remote_or_onsite"), "Remote")

    def test_description_from_job_description_not_why_apply(self):
        desc = self.details.get("description") or ""
        self.assertIn("Leadership Advisor", desc)
        self.assertNotIn("rare opportunity for an exceptional consultant", desc)

    def test_must_have_stored_in_raw_data(self):
        reqs = (self.details.get("raw_data") or {}).get("movemeon_must_have_requirements")
        self.assertTrue(reqs)
        self.assertTrue(any("strategy consulting" in str(r).lower() for r in reqs))

    def test_complete_status(self):
        self.assertEqual(self.details.get("detail_extraction_status"), "COMPLETE")
        self.assertEqual(self.details.get("missing_fields") or [], [])


class GbpBudgetParseTests(unittest.TestCase):
    def test_gbp_k_range(self):
        parsed = extraction.parse_budget("£60K - £90K")
        self.assertEqual(parsed["budget_min"], 60000.0)
        self.assertEqual(parsed["budget_max"], 90000.0)
        self.assertEqual(parsed["budget_currency"], "GBP")
        self.assertEqual(parsed["billing_type"], "fixed_range")


class MissingButNotVisibleTests(unittest.TestCase):
    """Fields never mentioned on the page must not be treated as failures."""

    def test_no_partial_when_optional_fields_are_absent(self):
        body = (
            "Job Description\n"
            "We need a hands-on interim controller to run month-end close and "
            "manage the transition to a new ERP system for our finance team.\n\n"
            "Location: Manchester, UK\n"
            "Compensation: 500 GBP per day\n"
        )
        details = extraction.extract_movemeon_detail_fields_from_body(
            body, title="Interim Controller"
        )
        missing_visible = (details.get("extraction_metadata") or {}).get(
            "fields_missing_but_visible"
        ) or []
        self.assertNotIn("platform_category", missing_visible)
        self.assertNotEqual(details.get("detail_extraction_status"), "FAILED")


class VisibleButMissedFieldsCausePartialTests(unittest.TestCase):
    def test_visible_location_heading_without_value_causes_partial(self):
        body = (
            "Job Description\n"
            "This role requires a strong finance background across multiple "
            "business units and reporting lines within a fast growing group.\n\n"
            "Location: \n"
            "Skills: Python\n"
        )
        details = extraction.extract_movemeon_detail_fields_from_body(
            body, title="Finance Role"
        )
        missing_visible = (details.get("extraction_metadata") or {}).get(
            "fields_missing_but_visible"
        ) or []
        self.assertIn("location", missing_visible)
        self.assertEqual(details.get("detail_extraction_status"), "PARTIAL")


class DetailStatusHelperTests(unittest.TestCase):
    def test_timeout_status(self):
        self.assertEqual(
            extraction.calculate_detail_extraction_status(
                attempted=True, page_ok=False, timeout=True, meaningful=False
            ),
            "TIMEOUT",
        )

    def test_failed_when_page_not_ok_and_not_timeout(self):
        self.assertEqual(
            extraction.calculate_detail_extraction_status(
                attempted=True, page_ok=False, timeout=False, meaningful=False
            ),
            "FAILED",
        )

    def test_not_attempted_status(self):
        self.assertEqual(
            extraction.calculate_detail_extraction_status(
                attempted=False, page_ok=False, meaningful=False
            ),
            "NOT_ATTEMPTED",
        )

    def test_category_only_missing_stays_complete_for_movemeon(self):
        status = extraction.calculate_detail_extraction_status(
            attempted=True,
            page_ok=True,
            fields_visible=["description", "platform_category"],
            fields_extracted=["description"],
            fields_missing_but_visible=["platform_category"],
            meaningful=True,
            platform="movemeon",
        )
        self.assertEqual(status, "COMPLETE")


class MergeDoesNotOverwriteTests(unittest.TestCase):
    def test_empty_detail_does_not_overwrite_card(self):
        card = {
            "title": "Interim CFO",
            "budget_text": "$500/day",
            "location": "London",
            "skills": ["Budgeting"],
        }
        detail = {"budget_text": "", "location": "Unknown", "skills": []}
        merged = extraction.merge_project_data(card, detail)
        self.assertEqual(merged["budget_text"], "$500/day")
        self.assertEqual(merged["location"], "London")
        self.assertEqual(merged["skills"], ["Budgeting"])

    def test_populated_detail_fields_are_applied(self):
        card = {"title": "Interim CFO", "location": "London"}
        detail = {"description": "Full role description here with enough characters.", "budget_text": "£700/day"}
        merged = extraction.merge_project_data(card, detail)
        self.assertIn("Full role description", merged.get("description") or "")
        self.assertTrue(merged.get("budget_text"))

    def test_unclassified_category_from_detail_does_not_overwrite_card(self):
        card = {"title": "Role", "platform_category": "Strategy"}
        detail = {"platform_category": "Unclassified"}
        merged = extraction.merge_project_data(card, detail)
        self.assertEqual(merged.get("platform_category"), "Strategy")


if __name__ == "__main__":
    unittest.main()
