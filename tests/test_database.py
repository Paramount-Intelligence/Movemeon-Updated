"""Unit tests for the MoveMeOn database.py Supabase data-access layer.

All tests are fully mocked — no live network / Supabase credentials required.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db


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
        # PostgREST chains .insert(...).select("*") — keep mutating op.
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

    def delete(self):
        self._op = "delete"
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
        if self._op == "delete":
            kept, deleted = [], []
            for row in rows:
                (deleted if self._match(row) else kept).append(row)
            self.store[self.name] = kept
            return FakeResponse(deleted)
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


class BoomTable(FakeTable):
    """A table whose .execute() always raises, to test _execute()'s error handling."""

    def __init__(self, store, name, message):
        super().__init__(store, name)
        self._message = message

    def execute(self):
        raise RuntimeError(self._message)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        db.reset_supabase_client()
        self._saved = {}
        for key in (
            "SUPABASE_URL",
            "SUPABASE_SECRET_KEY",
            "SUPABASE_SERVICE_ROLE_KEY",
            "SUPABASE_PUBLISHABLE_KEY",
            "SUPABASE_ANON_KEY",
        ):
            self._saved[key] = os.environ.pop(key, None)

    def tearDown(self):
        db.reset_supabase_client()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_preferred_secret_key(self):
        os.environ["SUPABASE_URL"] = "https://example.supabase.co"
        os.environ["SUPABASE_SECRET_KEY"] = "sb_secret_preferred"
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "legacy_should_not_win"
        url, key, source = db.get_supabase_credentials()
        self.assertEqual(url, "https://example.supabase.co")
        self.assertEqual(key, "sb_secret_preferred")
        self.assertEqual(source, "SUPABASE_SECRET_KEY")

    def test_legacy_service_role_fallback(self):
        os.environ["SUPABASE_URL"] = "https://example.supabase.co"
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "legacy_service_role"
        url, key, source = db.get_supabase_credentials()
        self.assertEqual(key, "legacy_service_role")
        self.assertEqual(source, "SUPABASE_SERVICE_ROLE_KEY")

    def test_missing_credentials_raise(self):
        os.environ["SUPABASE_URL"] = "https://example.supabase.co"
        with self.assertRaises(db.SupabaseConfigError):
            db.get_supabase_credentials()

    def test_missing_url_raises(self):
        with self.assertRaises(db.SupabaseConfigError):
            db.get_supabase_credentials()

    def test_reject_publishable_key_by_prefix(self):
        os.environ["SUPABASE_URL"] = "https://example.supabase.co"
        os.environ["SUPABASE_SECRET_KEY"] = "sb_publishable_not_allowed"
        with self.assertRaises(db.SupabaseConfigError):
            db.get_supabase_credentials()

    def test_reject_key_matching_publishable_env_value(self):
        os.environ["SUPABASE_URL"] = "https://example.supabase.co"
        os.environ["SUPABASE_PUBLISHABLE_KEY"] = "shared_value_123"
        os.environ["SUPABASE_SECRET_KEY"] = "shared_value_123"
        with self.assertRaises(db.SupabaseConfigError):
            db.get_supabase_credentials()


class DatabaseErrorPropagationTests(unittest.TestCase):
    """Database failures must never be silently treated as 'no data'."""

    def setUp(self):
        self.store = {"projects": []}
        self.client = FakeClient(self.store)
        db.reset_supabase_client()
        self.patcher = mock.patch.object(db, "get_supabase_client", return_value=self.client)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        db.reset_supabase_client()

    def test_should_process_project_raises_on_db_error(self):
        with mock.patch.object(
            db, "get_latest_project_occurrence",
            side_effect=db.SupabaseAPIError("simulated failure"),
        ):
            with self.assertRaises(db.SupabaseAPIError):
                db.should_process_project("movemeon", "p1")

    def test_get_latest_project_occurrence_network_error_propagates(self):
        with mock.patch.object(
            self.client, "table",
            return_value=BoomTable(self.store, "projects", "connection refused"),
        ):
            with self.assertRaises(db.SupabaseNetworkError):
                db.get_latest_project_occurrence("movemeon", "p1")

    def test_get_latest_project_occurrence_generic_error_is_api_error(self):
        with mock.patch.object(
            self.client, "table",
            return_value=BoomTable(self.store, "projects", "unexpected server error"),
        ):
            with self.assertRaises(db.SupabaseAPIError):
                db.get_latest_project_occurrence("movemeon", "p1")

    def test_platform_has_projects_raises_on_db_error(self):
        with mock.patch.object(
            self.client, "table",
            return_value=BoomTable(self.store, "projects", "boom"),
        ):
            with self.assertRaises(db.SupabaseAPIError):
                db.platform_has_projects("movemeon")

    def test_no_matching_rows_is_a_valid_empty_result_not_an_error(self):
        # Contrast case: genuinely no rows is None, never an exception.
        result = db.get_latest_project_occurrence("movemeon", "no-such-project")
        self.assertIsNone(result)


class NoMongoInProductionTests(unittest.TestCase):
    """Normal scraper runtime must use only Supabase — no MongoDB / pymongo."""

    @staticmethod
    def _read_source(filename):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, filename), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_database_module_source_has_no_pymongo(self):
        source = self._read_source("database.py")
        self.assertNotIn("pymongo", source.lower())
        self.assertNotIn("MongoClient", source)

    def test_script_clean_module_source_has_no_pymongo(self):
        source = self._read_source("script_clean.py")
        self.assertNotIn("pymongo", source.lower())
        self.assertNotIn("MongoClient", source)

    def test_database_module_does_not_import_pymongo_at_runtime(self):
        self.assertNotIn("pymongo", sys.modules)
        self.assertFalse(hasattr(db, "pymongo"))


class SchemaReadinessTests(unittest.TestCase):
    def test_list_required_tables_includes_core_tables(self):
        tables = db.list_required_tables()
        for expected in ("projects", "scraper_runs", "email_attempts", "scraper_sessions"):
            self.assertIn(expected, tables)

    def test_ensure_schema_ready_raises_config_error_when_table_missing(self):
        store = {}
        client = FakeClient(store)
        db.reset_supabase_client()

        def boom_table(name):
            return BoomTable(store, name, 'PGRST205: could not find the table "public.projects"')

        with mock.patch.object(db, "get_supabase_client", return_value=client):
            with mock.patch.object(client, "table", side_effect=boom_table):
                with self.assertRaises(db.SupabaseConfigError):
                    db.ensure_schema_ready()
        db.reset_supabase_client()


class PlatformConstantsTests(unittest.TestCase):
    def test_platform_movemeon_constant(self):
        self.assertEqual(db.PLATFORM_MOVEMEON, "movemeon")

    def test_three_day_window_constant(self):
        self.assertEqual(db.THREE_DAY_WINDOW, timedelta(days=3))

    def test_default_platform_is_movemeon(self):
        self.assertEqual(db.DEFAULT_PLATFORM, db.PLATFORM_MOVEMEON)


if __name__ == "__main__":
    unittest.main()
