"""Unit tests for MoveMeOn project identity, URL normalization, and the
three-day repeated-project eligibility rule (database.should_process_project).
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
import extraction


class FakeResponse:
    def __init__(self, data=None):
        self.data = data


class FakeTable:
    def __init__(self, store, name):
        self.store = store
        self.name = name
        self._filters = []
        self._order = None
        self._limit = None
        self._payload = None
        self._op = "select"

    def select(self, *_a, **_k):
        if self._op not in ("insert", "update", "upsert", "delete"):
            self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, key, value):
        self._filters.append(("eq", key, value))
        return self

    def order(self, key, desc=False):
        self._order = (key, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _match(self, row):
        for op, key, value in self._filters:
            if op == "eq" and row.get(key) != value:
                return False
        return True

    def execute(self):
        rows = self.store.setdefault(self.name, [])
        if self._op == "insert":
            payload = self._payload
            items = payload if isinstance(payload, list) else [payload]
            created = []
            for item in items:
                row = dict(item)
                row.setdefault("id", f"{self.name}-{len(rows) + 1}")
                rows.append(row)
                created.append(row)
            return FakeResponse(created)
        matched = [r for r in rows if self._match(r)]
        if self._order:
            key, desc = self._order
            matched.sort(key=lambda r: r.get(key) or "", reverse=bool(desc))
        if self._limit is not None:
            matched = matched[: self._limit]
        return FakeResponse(matched)


class FakeClient:
    def __init__(self, store=None):
        self.store = store if store is not None else {}

    def table(self, name):
        return FakeTable(self.store, name)


class IdentityAndUrlNormalizationTests(unittest.TestCase):
    def test_id_from_canonical_url_path(self):
        pid, canonical, meta = extraction.resolve_movemeon_project_identity(
            url="https://portal.movemeon.com/jobs/interim-cfo-123?utm_source=x"
        )
        self.assertEqual(pid, "interim-cfo-123")
        self.assertEqual(meta["identity_source"], "url_path")
        self.assertEqual(meta["identity_confidence"], "HIGH")

    def test_canonicalize_strips_utm_and_tracking_params(self):
        url = (
            "https://portal.movemeon.com/jobs/interim-cfo-123"
            "?utm_source=newsletter&utm_medium=email&utm_campaign=x&ref=abc&gclid=y"
        )
        canonical = extraction.canonicalize_movemeon_url(url)
        self.assertNotIn("utm_source", canonical)
        self.assertNotIn("utm_medium", canonical)
        self.assertNotIn("utm_campaign", canonical)
        self.assertNotIn("ref=", canonical)
        self.assertNotIn("gclid", canonical)
        self.assertIn("/jobs/interim-cfo-123", canonical)

    def test_canonicalize_preserves_non_tracking_query_params(self):
        url = "https://portal.movemeon.com/jobs/abc?utm_source=x&locale=en"
        canonical = extraction.canonicalize_movemeon_url(url)
        self.assertIn("locale=en", canonical)
        self.assertNotIn("utm_source", canonical)

    def test_canonicalize_strips_trailing_slash(self):
        canonical = extraction.canonicalize_movemeon_url(
            "https://portal.movemeon.com/jobs/abc/"
        )
        self.assertFalse(canonical.endswith("/"))

    def test_url_tail_fallback_when_no_jobs_segment(self):
        pid, source = extraction.project_id_from_movemeon_url(
            "https://portal.movemeon.com/some-other-path/abc-999"
        )
        self.assertEqual(pid, "abc-999")
        self.assertEqual(source, "url_tail")

    def test_same_url_always_resolves_to_same_id(self):
        url = "https://portal.movemeon.com/jobs/stable-role-1"
        pid1, _, _ = extraction.resolve_movemeon_project_identity(url=url)
        pid2, _, _ = extraction.resolve_movemeon_project_identity(url=url)
        self.assertEqual(pid1, pid2)

    def test_missing_url_falls_back_to_hash(self):
        pid, canonical, meta = extraction.resolve_movemeon_project_identity(url="")
        self.assertTrue(pid)
        self.assertEqual(meta["identity_source"], "url_hash")
        self.assertEqual(meta["identity_confidence"], "LOW")


class ThreeDayWindowTests(unittest.TestCase):
    """Prefer testing should_process_project with a patched
    database.get_latest_project_occurrence, per project convention."""

    def setUp(self):
        db.reset_supabase_client()

    def tearDown(self):
        db.reset_supabase_client()

    def test_first_occurrence_eligible(self):
        with mock.patch.object(db, "get_latest_project_occurrence", return_value=None):
            ok, reason, latest = db.should_process_project("movemeon", "job-1")
        self.assertTrue(ok)
        self.assertEqual(reason, "first_occurrence")
        self.assertIsNone(latest)

    def test_same_id_within_three_days_skipped(self):
        now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
        latest_row = {"id": "row-1", "scraped_at": (now - timedelta(days=2)).isoformat()}
        with mock.patch.object(db, "get_latest_project_occurrence", return_value=latest_row):
            ok, reason, latest = db.should_process_project("movemeon", "job-1", now=now)
        self.assertFalse(ok)
        self.assertIn("skipped_within_3_days", reason)
        self.assertEqual(latest["id"], "row-1")

    def test_exactly_three_days_skipped(self):
        now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
        latest_row = {"id": "row-1", "scraped_at": (now - timedelta(days=3)).isoformat()}
        with mock.patch.object(db, "get_latest_project_occurrence", return_value=latest_row):
            ok, reason, _ = db.should_process_project("movemeon", "job-1", now=now)
        self.assertFalse(ok)
        self.assertIn("skipped_within_3_days", reason)

    def test_more_than_three_days_eligible(self):
        now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
        latest_row = {
            "id": "row-1",
            "scraped_at": (now - timedelta(days=3, seconds=1)).isoformat(),
        }
        with mock.patch.object(db, "get_latest_project_occurrence", return_value=latest_row):
            ok, reason, _ = db.should_process_project("movemeon", "job-1", now=now)
        self.assertTrue(ok)
        self.assertIn("eligible_after_", reason)

    def test_same_id_different_platforms_independent(self):
        def fake_latest(platform, project_id):
            if platform == "catalant":
                return {
                    "id": "cat-row",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                }
            return None

        with mock.patch.object(db, "get_latest_project_occurrence", side_effect=fake_latest):
            ok, reason, latest = db.should_process_project("movemeon", "shared-id")
        self.assertTrue(ok)
        self.assertEqual(reason, "first_occurrence")
        self.assertIsNone(latest)

    def test_db_failure_not_treated_as_empty(self):
        with mock.patch.object(
            db, "get_latest_project_occurrence", side_effect=db.SupabaseAPIError("boom")
        ):
            with self.assertRaises(db.SupabaseAPIError):
                db.should_process_project("movemeon", "job-1")


class MultipleOccurrenceInsertTests(unittest.TestCase):
    """MoveMeOn occurrences are always inserted (never upserted) — several
    rows for the same project_id are expected once eligible again."""

    def setUp(self):
        self.store = {"projects": []}
        self.client = FakeClient(self.store)
        db.reset_supabase_client()
        self.patcher = mock.patch.object(db, "get_supabase_client", return_value=self.client)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        db.reset_supabase_client()

    def test_several_movemeon_occurrences_allowed_after_three_days(self):
        now = datetime.now(timezone.utc)
        row1 = db.insert_project_occurrence(
            {
                "platform": "movemeon",
                "project_id": "job-42",
                "title": "Round 1",
                "source_url": "https://portal.movemeon.com/jobs/job-42",
            },
            email_status="SUPPRESSED",
            email_eligible=False,
            email_not_sent_reason="COLD_START_SEED",
        )
        for row in self.store["projects"]:
            if row["id"] == row1["id"]:
                row["scraped_at"] = (now - timedelta(days=4)).isoformat()

        ok, reason, latest = db.should_process_project("movemeon", "job-42", now=now)
        self.assertTrue(ok)
        self.assertIn("eligible_after_", reason)
        self.assertEqual(latest["id"], row1["id"])

        row2 = db.insert_project_occurrence(
            {
                "platform": "movemeon",
                "project_id": "job-42",
                "title": "Round 2",
                "source_url": "https://portal.movemeon.com/jobs/job-42",
            },
        )
        self.assertNotEqual(row1["id"], row2["id"])
        rows_for_id = [r for r in self.store["projects"] if r["project_id"] == "job-42"]
        self.assertEqual(len(rows_for_id), 2)

    def test_same_id_across_platforms_stored_independently(self):
        db.insert_project_occurrence(
            {
                "platform": "movemeon",
                "project_id": "shared-1",
                "title": "MoveMeOn Version",
                "source_url": "https://portal.movemeon.com/jobs/shared-1",
            }
        )
        db.insert_project_occurrence(
            {
                "platform": "btg",
                "project_id": "shared-1",
                "title": "BTG Version",
                "source_url": "https://talent.businesstalentgroup.com/projects/shared-1",
            }
        )
        movemeon_rows = [r for r in self.store["projects"] if r["platform"] == "movemeon"]
        btg_rows = [r for r in self.store["projects"] if r["platform"] == "btg"]
        self.assertEqual(len(movemeon_rows), 1)
        self.assertEqual(len(btg_rows), 1)


if __name__ == "__main__":
    unittest.main()
