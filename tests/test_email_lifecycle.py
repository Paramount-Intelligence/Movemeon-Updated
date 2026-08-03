"""Unit tests for the MoveMeOn email attempt / retry lifecycle.

Covers script_clean.process_project_email (insert-before-send ordering,
success/failure/max-retries transitions) and the retryable-email filters in
database.py. All Supabase and SMTP calls are mocked — no live network.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
import script_clean as sc


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

    def lt(self, key, value):
        self._filters.append(("lt", key, value))
        return self

    def lte(self, key, value):
        self._filters.append(("lte", key, value))
        return self

    def order(self, key, desc=False):
        self._order = (key, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _match(self, row):
        for op, key, value in self._filters:
            actual = row.get(key)
            if op == "eq" and actual != value:
                return False
            if op == "lt" and not (actual is not None and str(actual) < str(value)):
                return False
            if op == "lte" and not (actual is not None and str(actual) <= str(value)):
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
        if self._op == "update":
            updated = []
            for i, row in enumerate(rows):
                if self._match(row):
                    rows[i] = {**row, **(self._payload or {})}
                    updated.append(rows[i])
            return FakeResponse(updated)
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


class ProcessProjectEmailTests(unittest.TestCase):
    def setUp(self):
        self.patchers = [
            mock.patch.object(db, "create_email_attempt"),
            mock.patch.object(db, "complete_email_attempt_success"),
            mock.patch.object(db, "complete_email_attempt_failure"),
            mock.patch.object(db, "update_project_email_status"),
            mock.patch.object(sc, "send_notification"),
        ]
        (
            self.mock_create,
            self.mock_success,
            self.mock_failure,
            self.mock_update,
            self.mock_send,
        ) = (p.start() for p in self.patchers)
        self.mock_create.return_value = {"id": "attempt-uuid-1"}

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def test_missing_row_id_raises_value_error(self):
        with self.assertRaises(ValueError):
            sc.process_project_email({})

    def test_success_updates_same_project_uuid(self):
        self.mock_send.return_value = {
            "success": True, "ok": True, "message_id": "<m1>",
            "failure_code": None, "error": None,
        }
        row = {"id": "row-uuid-1", "email_attempt_count": 0}
        result = sc.process_project_email(row)

        self.assertTrue(result["success"])
        self.mock_success.assert_called_once()
        success_args = self.mock_success.call_args.args
        self.assertEqual(success_args[0], "attempt-uuid-1")

        self.mock_update.assert_called_once()
        update_args = self.mock_update.call_args.args
        update_kwargs = self.mock_update.call_args.kwargs
        self.assertEqual(update_args[0], "row-uuid-1")
        self.assertEqual(update_kwargs["email_status"], "SENT")
        self.assertTrue(update_kwargs["email_sent"])

    def test_failure_sets_retry_pending_with_backoff(self):
        self.mock_send.return_value = {
            "success": False, "ok": False, "message_id": None,
            "failure_code": "SMTP_SEND_ERROR", "error": "boom",
        }
        row = {"id": "row-uuid-2", "email_attempt_count": 0}
        with mock.patch.object(sc.Config, "EMAIL_MAX_RETRIES", 3):
            result = sc.process_project_email(row)

        self.assertFalse(result["success"])
        self.mock_failure.assert_called_once()
        update_kwargs = self.mock_update.call_args.kwargs
        self.assertEqual(update_kwargs["email_status"], "RETRY_PENDING")
        self.assertIsNotNone(update_kwargs["email_next_retry_at"])

    def test_max_retries_reached_produces_failed_with_no_retry(self):
        self.mock_send.return_value = {
            "success": False, "ok": False, "message_id": None,
            "failure_code": "SMTP_AUTH_ERROR", "error": "bad creds",
        }
        row = {"id": "row-uuid-3", "email_attempt_count": 2}  # next attempt == 3
        with mock.patch.object(sc.Config, "EMAIL_MAX_RETRIES", 3):
            result = sc.process_project_email(row)

        self.assertFalse(result["success"])
        update_kwargs = self.mock_update.call_args.kwargs
        self.assertEqual(update_kwargs["email_status"], "FAILED")
        self.assertIsNone(update_kwargs["email_next_retry_at"])

    def test_project_insert_occurs_before_email_send(self):
        order = []

        def fake_insert(*_a, **_k):
            order.append("insert")
            return {"id": "row-uuid-9", "email_attempt_count": 0, "project_id": "p9"}

        def fake_create_attempt(_row_id, _attempt_number, **_k):
            order.append("create_attempt")
            return {"id": "attempt-9"}

        def fake_send(_row):
            order.append("send")
            return {
                "success": True, "ok": True, "message_id": "<m9>",
                "failure_code": None, "error": None,
            }

        with mock.patch.object(db, "insert_project_occurrence", side_effect=fake_insert):
            with mock.patch.object(db, "create_email_attempt", side_effect=fake_create_attempt):
                with mock.patch.object(sc, "send_notification", side_effect=fake_send):
                    row = db.insert_project_occurrence(
                        {
                            "platform": "movemeon",
                            "project_id": "p9",
                            "title": "T",
                            "source_url": "https://portal.movemeon.com/jobs/p9",
                        }
                    )
                    sc.process_project_email(row)

        self.assertEqual(order, ["insert", "create_attempt", "send"])


class RetryBackoffTests(unittest.TestCase):
    def test_next_retry_increases_with_attempt_count(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t1 = db.compute_email_next_retry_at(1, base_minutes=15, now=now)
        t2 = db.compute_email_next_retry_at(2, base_minutes=15, now=now)
        t3 = db.compute_email_next_retry_at(3, base_minutes=15, now=now)
        self.assertLess(t1, t2)
        self.assertLess(t2, t3)

    def test_backoff_is_capped_at_24_hours(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t = db.compute_email_next_retry_at(20, base_minutes=15, now=now)
        self.assertLessEqual(t - now, timedelta(hours=24))


class RetryableFilterTests(unittest.TestCase):
    """Suppressed / not-required emails must never surface as retryable."""

    def setUp(self):
        self.store = {"projects": []}
        self.client = FakeClient(self.store)
        db.reset_supabase_client()
        self.patcher = mock.patch.object(db, "get_supabase_client", return_value=self.client)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        db.reset_supabase_client()

    def test_suppressed_and_not_required_excluded_from_retryable(self):
        now = datetime.now(timezone.utc)
        due = (now - timedelta(minutes=1)).isoformat()
        self.store["projects"] = [
            {
                "id": "r1", "platform": "movemeon", "email_status": "RETRY_PENDING",
                "email_eligible": True, "email_attempt_count": 1, "email_next_retry_at": due,
            },
            {
                "id": "r2", "platform": "movemeon", "email_status": "SUPPRESSED",
                "email_eligible": False, "email_attempt_count": 0, "email_next_retry_at": due,
            },
            {
                "id": "r3", "platform": "movemeon", "email_status": "NOT_REQUIRED",
                "email_eligible": False, "email_attempt_count": 0, "email_next_retry_at": due,
            },
        ]
        rows = db.get_retryable_email_projects(max_attempts=3, platform="movemeon")
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, ["r1"])

    def test_cold_start_seed_never_retryable(self):
        now = datetime.now(timezone.utc)
        self.store["projects"] = [
            {
                "id": "seed1", "platform": "movemeon", "email_status": "SUPPRESSED",
                "email_eligible": False, "email_attempt_count": 0,
                "email_next_retry_at": (now - timedelta(minutes=1)).isoformat(),
            }
        ]
        rows = db.get_retryable_email_projects(max_attempts=3, platform="movemeon")
        self.assertEqual(rows, [])


class RetryPendingEmailsFlowTests(unittest.TestCase):
    def test_dry_run_never_calls_process_project_email(self):
        with mock.patch.object(
            db, "get_retryable_email_projects",
            return_value=[{"id": "r1", "title": "T", "email_attempt_count": 0}],
        ):
            with mock.patch.object(sc, "process_project_email") as mock_process:
                result = sc.retry_pending_emails(dry_run=True)
        mock_process.assert_not_called()
        self.assertEqual(result, {"sent": 0, "failed": 0, "total": 1})

    def test_no_due_rows_returns_zero_counts(self):
        with mock.patch.object(db, "get_retryable_email_projects", return_value=[]):
            result = sc.retry_pending_emails(dry_run=False)
        self.assertEqual(result, {"sent": 0, "failed": 0, "total": 0})


if __name__ == "__main__":
    unittest.main()
