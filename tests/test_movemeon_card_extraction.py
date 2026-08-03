"""Unit tests for MoveMeOn job-card normalization and card extraction status."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extraction


class TitlePreservationTests(unittest.TestCase):
    def test_real_title_preserved_when_no_company_suffix(self):
        raw = {
            "title": "Interim Finance Director",
            "company": "TechCorp Solutions",
            "url": "https://portal.movemeon.com/jobs/interim-finance-director-1",
        }
        project = extraction.normalize_movemeon_card_project(raw)
        self.assertEqual(project["title"], "Interim Finance Director")

    def test_legacy_company_suffix_is_stripped_not_appended(self):
        raw = {
            "title": "Interim Finance Director (TechCorp Solutions)",
            "company": "TechCorp Solutions",
            "url": "https://portal.movemeon.com/jobs/interim-finance-director-1",
        }
        project = extraction.normalize_movemeon_card_project(raw)
        self.assertEqual(project["title"], "Interim Finance Director")
        self.assertNotIn("TechCorp", project["title"])


class CompanyRawDataTests(unittest.TestCase):
    def test_company_stored_in_raw_data_not_title(self):
        raw = {
            "title": "Interim CFO",
            "company": "Acme Corp",
            "url": "https://portal.movemeon.com/jobs/interim-cfo-9",
        }
        project = extraction.normalize_movemeon_card_project(raw)
        self.assertEqual(project["raw_data"]["movemeon_company"], "Acme Corp")
        self.assertNotIn("Acme Corp", project["title"])

    def test_legacy_movemeon_company_key_supported(self):
        raw = {
            "title": "Interim CFO",
            "movemeon_company": "Legacy Co",
            "url": "https://portal.movemeon.com/jobs/interim-cfo-legacy",
        }
        project = extraction.normalize_movemeon_card_project(raw)
        self.assertEqual(project["raw_data"]["movemeon_company"], "Legacy Co")


class StableIdentityTests(unittest.TestCase):
    def test_stable_id_from_url_path(self):
        raw = {
            "title": "Interim CFO",
            "url": "https://portal.movemeon.com/jobs/interim-cfo-9?utm_source=email",
        }
        project = extraction.normalize_movemeon_card_project(raw)
        self.assertEqual(project["project_id"], "interim-cfo-9")
        self.assertNotIn("utm_source", project["source_url"])

    def test_same_url_gives_same_id_across_calls(self):
        raw = {"title": "T", "url": "https://portal.movemeon.com/jobs/stable-id-1"}
        p1 = extraction.normalize_movemeon_card_project(raw)
        p2 = extraction.normalize_movemeon_card_project(dict(raw))
        self.assertEqual(p1["project_id"], p2["project_id"])


class LocationCompensationEngagementTests(unittest.TestCase):
    def test_location_and_budget_preserved(self):
        raw = {
            "title": "Interim CFO",
            "url": "https://portal.movemeon.com/jobs/interim-cfo-loc",
            "location": "London, UK",
            "budget_text": "$800/day",
        }
        project = extraction.normalize_movemeon_card_project(raw)
        self.assertEqual(project["location"], "London, UK")
        self.assertIn("day", (project.get("budget_text") or "").lower())
        self.assertEqual(project.get("daily_rate"), 800.0)

    def test_permanent_sets_engagement_type_not_duration_text(self):
        raw = {
            "title": "Head of Strategy",
            "url": "https://portal.movemeon.com/jobs/head-of-strategy",
            "engagement_type": "Permanent",
        }
        project = extraction.normalize_movemeon_card_project(raw)
        self.assertEqual(project["engagement_type"], "Permanent")
        self.assertIsNone(project.get("duration_text"))

    def test_contract_sets_engagement_type_not_duration_text(self):
        raw = {
            "title": "Interim COO",
            "url": "https://portal.movemeon.com/jobs/interim-coo",
            "engagement_type": "Contract",
        }
        project = extraction.normalize_movemeon_card_project(raw)
        self.assertEqual(project["engagement_type"], "Contract")
        self.assertIsNone(project.get("duration_text"))

    def test_pure_duration_text_does_not_populate_engagement_type(self):
        mapped = extraction.classify_engagement_or_duration("3 months")
        self.assertEqual(mapped.get("duration_text"), "3 months")
        self.assertNotIn("engagement_type", mapped)


class ShortDescriptionTests(unittest.TestCase):
    def test_short_description_preserved(self):
        raw = {
            "title": "Interim CFO",
            "url": "https://portal.movemeon.com/jobs/interim-cfo-desc",
            "short_description": "Lead the finance function through a growth phase.",
        }
        project = extraction.normalize_movemeon_card_project(raw)
        self.assertEqual(
            project["short_description"],
            "Lead the finance function through a growth phase.",
        )


class InventedPlaceholderRejectionTests(unittest.TestCase):
    def test_recently_time_posted_not_used(self):
        raw = {
            "title": "Interim CFO",
            "url": "https://portal.movemeon.com/jobs/interim-cfo-recent",
            "time_posted_text": "Recently",
        }
        project = extraction.normalize_movemeon_card_project(raw)
        self.assertIsNone(project.get("time_posted_text"))

    def test_new_status_not_used(self):
        raw = {
            "title": "Interim CFO",
            "url": "https://portal.movemeon.com/jobs/interim-cfo-new",
            "status": "New",
        }
        project = extraction.normalize_movemeon_card_project(raw)
        self.assertIsNone(project.get("status"))

    def test_real_time_posted_value_kept(self):
        raw = {
            "title": "Interim CFO",
            "url": "https://portal.movemeon.com/jobs/interim-cfo-time",
            "time_posted_text": "3 days ago",
        }
        project = extraction.normalize_movemeon_card_project(raw)
        self.assertEqual(project.get("time_posted_text"), "3 days ago")


class CardExtractionStatusTests(unittest.TestCase):
    def test_failed_when_identity_missing(self):
        status = extraction.calculate_card_extraction_status(
            {"title": "T"}, platform="movemeon"
        )
        self.assertEqual(status, "FAILED")

    def test_complete_with_minimal_movemeon_identity(self):
        status = extraction.calculate_card_extraction_status(
            {
                "project_id": "p1",
                "title": "T",
                "source_url": "https://portal.movemeon.com/jobs/p1",
            },
            platform="movemeon",
        )
        self.assertEqual(status, "COMPLETE")

    def test_partial_when_richer_platform_missing_expected_field(self):
        # Catalant's expected-visible-field set is larger than movemeon's,
        # so identity alone yields PARTIAL there (demonstrates the PARTIAL path).
        status = extraction.calculate_card_extraction_status(
            {
                "project_id": "p1",
                "title": "T",
                "source_url": "https://example.com/p1",
            },
            platform="catalant",
        )
        self.assertEqual(status, "PARTIAL")

    def test_full_card_normalization_yields_complete_status(self):
        raw = {
            "title": "Interim CFO",
            "company": "Acme Corp",
            "url": "https://portal.movemeon.com/jobs/interim-cfo-complete",
            "location": "Remote",
            "budget_text": "$700/day",
            "engagement_type": "Contract",
            "short_description": "Great role.",
        }
        project = extraction.normalize_movemeon_card_project(raw)
        self.assertEqual(project["card_extraction_status"], "COMPLETE")


class CategoryNeverExposedOnCardTests(unittest.TestCase):
    def test_card_never_invents_a_category(self):
        raw = {
            "title": "Interim CFO",
            "url": "https://portal.movemeon.com/jobs/interim-cfo-cat",
        }
        project = extraction.normalize_movemeon_card_project(raw)
        self.assertIsNone(project.get("platform_category"))
        self.assertEqual(project.get("platform_category_extraction_status"), "NOT_EXPOSED")


if __name__ == "__main__":
    unittest.main()
