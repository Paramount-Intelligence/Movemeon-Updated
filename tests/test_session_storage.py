"""Unit tests for MoveMeOn scraper_sessions cookie save/load/clear contract.

Verifies save_scraper_session / delete_scraper_session never send a null
saved_at and never overwrite worker-lock columns during a cookie clear.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db


class FakeResponse:
    def __init__(self, data=None):
        self.data = data


class FakeTable:
    def __init__(self, store, name, client=None):
        self.store = store
        self.name = name
        self.client = client
        self._filters = []
        self._payload = None
        self._op = "select"
        self._limit = None

    def select(self, *_a, **_k):
        if self._op not in ("insert", "update", "upsert", "delete"):
            self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        if self.client is not None:
            self.client.last_writes.append((self.name, "insert", dict(payload)))
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        if self.client is not None:
            self.client.last_writes.append((self.name, "update", dict(payload)))
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, key, value):
        self._filters.append((key, value))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _match(self, row):
        return all(row.get(k) == v for k, v in self._filters)

    def execute(self):
        rows = self.store.setdefault(self.name, [])
        if self._op == "insert":
            payload = dict(self._payload)
            payload.setdefault("id", f"{self.name}-{len(rows) + 1}")
            rows.append(payload)
            return FakeResponse([payload])
        if self._op == "update":
            updated = []
            for row in rows:
                if self._match(row):
                    row.update(self._payload)
                    updated.append(row)
            return FakeResponse(updated)
        if self._op == "delete":
            kept, deleted = [], []
            for row in rows:
                (deleted if self._match(row) else kept).append(row)
            self.store[self.name] = kept
            return FakeResponse(deleted)
        matched = [r for r in rows if self._match(r)]
        if self._limit is not None:
            matched = matched[: self._limit]
        return FakeResponse(matched)


class FakeClient:
    def __init__(self):
        self.store = {}
        self.last_writes = []

    def table(self, name):
        return FakeTable(self.store, name, self)


class SaveLoadContractTests(unittest.TestCase):
    def setUp(self):
        db.reset_supabase_client()
        self.client = FakeClient()
        db._supabase_client = self.client

    def tearDown(self):
        db.reset_supabase_client()

    def test_save_produces_non_null_saved_at(self):
        saved = db.save_scraper_session("movemeon", [{"name": "sid", "value": "abc"}])
        self.assertIsNotNone(saved.get("saved_at"))

    def test_load_roundtrip_returns_saved_cookies(self):
        db.save_scraper_session("movemeon", [{"name": "sid", "value": "abc"}])
        loaded = db.load_scraper_session("movemeon")
        self.assertEqual(loaded["session_data"]["cookies"][0]["name"], "sid")

    def test_save_strips_password_like_keys_from_cookies(self):
        db.save_scraper_session(
            "movemeon",
            [{"name": "sid", "value": "abc", "password": "should-not-be-stored"}],
        )
        loaded = db.load_scraper_session("movemeon")
        cookie = loaded["session_data"]["cookies"][0]
        self.assertNotIn("password", cookie)

    def test_second_save_updates_existing_row_instead_of_inserting(self):
        first = db.save_scraper_session("movemeon", [{"name": "a", "value": "1"}])
        second = db.save_scraper_session("movemeon", [{"name": "b", "value": "2"}])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.client.store["scraper_sessions"]), 1)

    def test_load_missing_platform_returns_none(self):
        self.assertIsNone(db.load_scraper_session("nonexistent-platform"))


class SessionWriteContractAssertionTests(unittest.TestCase):
    """_assert_session_write_contract is the application-level safety net."""

    def test_rejects_null_saved_at(self):
        with self.assertRaises(db.SupabaseAPIError):
            db._assert_session_write_contract({"saved_at": None}, operation="unit_test")

    def test_accepts_non_null_saved_at(self):
        db._assert_session_write_contract(
            {"saved_at": "2026-08-03T00:00:00+00:00"}, operation="unit_test"
        )

    def test_rejects_worker_lock_owner_in_payload(self):
        with self.assertRaises(db.SupabaseAPIError):
            db._assert_session_write_contract(
                {"saved_at": "2026-08-03T00:00:00+00:00", "worker_lock_owner": "x"},
                operation="unit_test",
            )

    def test_rejects_worker_lock_expires_at_in_payload(self):
        with self.assertRaises(db.SupabaseAPIError):
            db._assert_session_write_contract(
                {"saved_at": "2026-08-03T00:00:00+00:00", "worker_lock_expires_at": "x"},
                operation="unit_test",
            )

    def test_rejects_worker_lock_heartbeat_at_in_payload(self):
        with self.assertRaises(db.SupabaseAPIError):
            db._assert_session_write_contract(
                {"saved_at": "2026-08-03T00:00:00+00:00", "worker_lock_heartbeat_at": "x"},
                operation="unit_test",
            )


class CookieClearPreservesWorkerLockTests(unittest.TestCase):
    def setUp(self):
        db.reset_supabase_client()
        self.client = FakeClient()
        db._supabase_client = self.client
        self.client.store["scraper_sessions"] = [
            {
                "id": "sess-1",
                "platform": "movemeon",
                "session_data": {"cookies": [{"name": "sid", "value": "abc"}]},
                "saved_at": "2026-08-01T00:00:00+00:00",
                "expires_at": "2026-08-08T00:00:00+00:00",
                "worker_lock_owner": "worker-a",
                "worker_lock_expires_at": "2026-08-03T01:00:00+00:00",
                "worker_lock_heartbeat_at": "2026-08-03T00:55:00+00:00",
            }
        ]

    def tearDown(self):
        db.reset_supabase_client()

    def test_delete_scraper_session_omits_lock_columns_from_update_payload(self):
        self.client.last_writes.clear()
        db.delete_scraper_session("movemeon")
        updates = [
            payload
            for (table, op, payload) in self.client.last_writes
            if table == "scraper_sessions" and op == "update"
        ]
        self.assertTrue(updates)
        payload = updates[-1]
        self.assertNotIn("worker_lock_owner", payload)
        self.assertNotIn("worker_lock_expires_at", payload)
        self.assertNotIn("worker_lock_heartbeat_at", payload)

    def test_delete_scraper_session_preserves_lock_values_in_store(self):
        db.delete_scraper_session("movemeon")
        row = self.client.store["scraper_sessions"][0]
        self.assertEqual(row["worker_lock_owner"], "worker-a")
        self.assertEqual(row["worker_lock_expires_at"], "2026-08-03T01:00:00+00:00")

    def test_delete_scraper_session_clears_cookies_and_marks_metadata(self):
        db.delete_scraper_session("movemeon")
        row = self.client.store["scraper_sessions"][0]
        self.assertEqual(row["session_data"]["cookies"], [])
        self.assertEqual(row["metadata"]["session_state"], "cleared")

    def test_delete_scraper_session_never_sends_saved_at_none(self):
        db.delete_scraper_session("movemeon")
        row = self.client.store["scraper_sessions"][0]
        self.assertIsNotNone(row["saved_at"])

    def test_delete_scraper_session_idempotent_when_row_missing(self):
        self.client.store["scraper_sessions"] = []
        self.assertTrue(db.delete_scraper_session("movemeon"))


class CookieSavePreservesWorkerLockTests(unittest.TestCase):
    def setUp(self):
        db.reset_supabase_client()
        self.client = FakeClient()
        db._supabase_client = self.client
        self.client.store["scraper_sessions"] = [
            {
                "id": "sess-1",
                "platform": "movemeon",
                "session_data": {"cookies": []},
                "saved_at": "2026-08-01T00:00:00+00:00",
                "worker_lock_owner": "worker-a",
                "worker_lock_expires_at": "2026-08-03T01:00:00+00:00",
                "worker_lock_heartbeat_at": "2026-08-03T00:55:00+00:00",
            }
        ]

    def tearDown(self):
        db.reset_supabase_client()

    def test_save_scraper_session_omits_lock_columns_from_payload(self):
        self.client.last_writes.clear()
        db.save_scraper_session("movemeon", [{"name": "sid", "value": "new-cookie"}])
        writes = [
            payload
            for (table, op, payload) in self.client.last_writes
            if table == "scraper_sessions" and op in ("update", "insert")
        ]
        self.assertTrue(writes)
        payload = writes[-1]
        self.assertNotIn("worker_lock_owner", payload)
        self.assertNotIn("worker_lock_expires_at", payload)
        self.assertNotIn("worker_lock_heartbeat_at", payload)

    def test_save_scraper_session_preserves_lock_in_store(self):
        db.save_scraper_session("movemeon", [{"name": "sid", "value": "new-cookie"}])
        row = self.client.store["scraper_sessions"][0]
        self.assertEqual(row["worker_lock_owner"], "worker-a")


if __name__ == "__main__":
    unittest.main()
