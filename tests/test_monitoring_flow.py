"""Unit tests for cold-start detection, CLI flags, and scrape-cycle flow control."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
import extraction
import script_clean as sc


class FakeResponse:
    def __init__(self, data=None):
        self.data = data


class FakeTable:
    def __init__(self, store, name):
        self.store = store
        self.name = name
        self._filters = []
        self._limit = None
        self._op = "select"

    def select(self, *_a, **_k):
        if self._op not in ("insert", "update", "delete"):
            self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
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
        matched = [r for r in rows if self._match(r)]
        if self._limit is not None:
            matched = matched[: self._limit]
        return FakeResponse(matched)


class FakeClient:
    def __init__(self, store=None):
        self.store = store if store is not None else {}

    def table(self, name):
        return FakeTable(self.store, name)


class ColdStartDetectionTests(unittest.TestCase):
    """platform_has_projects('movemeon') must be False even when other
    platforms already have rows — cold start is platform-specific."""

    def setUp(self):
        self.store = {
            "projects": [
                {"id": "c1", "platform": "catalant", "project_id": "x"},
                {"id": "b1", "platform": "btg", "project_id": "y"},
            ]
        }
        self.client = FakeClient(self.store)
        db.reset_supabase_client()
        self.patcher = mock.patch.object(db, "get_supabase_client", return_value=self.client)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        db.reset_supabase_client()

    def test_movemeon_cold_start_despite_other_platforms_having_rows(self):
        self.assertFalse(db.platform_has_projects("movemeon"))
        self.assertTrue(db.platform_has_projects("catalant"))
        self.assertTrue(db.platform_has_projects("btg"))


class RunScrapeCycleColdStartTests(unittest.TestCase):
    """Cold-start seeded rows must be SUPPRESSED / COLD_START_SEED and never emailed."""

    @staticmethod
    def _make_project():
        raw = {
            "title": "Interim CFO",
            "company": "Acme",
            "url": "https://portal.movemeon.com/jobs/interim-cfo-flow",
            "location": "London",
            "budget_text": "$700/day",
            "engagement_type": "Contract",
            "short_description": "Great interim role.",
        }
        return extraction.normalize_movemeon_card_project(raw)

    def test_cold_start_inserts_are_suppressed_with_no_email(self):
        project = self._make_project()

        with mock.patch.object(sc, "scan_for_projects", return_value=([project], 1)), \
             mock.patch.object(
                 sc, "fetch_project_details_with_retry",
                 return_value={"detail_extraction_status": "COMPLETE"},
             ), \
             mock.patch.object(sc, "_navigate_to_search"), \
             mock.patch.object(db, "platform_has_projects", return_value=False), \
             mock.patch.object(db, "create_scraper_run", return_value={"id": "run-1"}), \
             mock.patch.object(db, "complete_scraper_run") as mock_complete, \
             mock.patch.object(db, "insert_project_occurrence") as mock_insert, \
             mock.patch.object(sc, "process_project_email") as mock_email:
            mock_insert.return_value = {"id": "row-1"}
            driver = mock.MagicMock()
            driver.current_url = "https://portal.movemeon.com/dashboard/candidate/jobs"

            stats = sc.run_scrape_cycle(driver, dry_run=False)

        mock_insert.assert_called_once()
        _, kwargs = mock_insert.call_args
        self.assertEqual(kwargs.get("email_status"), "SUPPRESSED")
        self.assertFalse(kwargs.get("email_eligible"))
        self.assertEqual(kwargs.get("email_not_sent_reason"), "COLD_START_SEED")
        mock_email.assert_not_called()
        self.assertEqual(stats["projects_inserted"], 1)
        self.assertEqual(stats["emails_suppressed"], 1)
        self.assertEqual(stats["emails_sent"], 0)
        mock_complete.assert_called_once()

    def test_dry_run_never_creates_a_scraper_run(self):
        project = self._make_project()

        with mock.patch.object(sc, "scan_for_projects", return_value=([project], 1)), \
             mock.patch.object(
                 sc, "fetch_project_details_with_retry",
                 return_value={"detail_extraction_status": "COMPLETE"},
             ), \
             mock.patch.object(sc, "_navigate_to_search"), \
             mock.patch.object(db, "platform_has_projects", return_value=True), \
             mock.patch.object(db, "should_process_project", return_value=(True, "first_occurrence", None)), \
             mock.patch.object(db, "create_scraper_run") as mock_create_run, \
             mock.patch.object(db, "insert_project_occurrence") as mock_insert:
            driver = mock.MagicMock()
            driver.current_url = "https://portal.movemeon.com/dashboard/candidate/jobs"

            sc.run_scrape_cycle(driver, dry_run=True)

        mock_create_run.assert_not_called()
        mock_insert.assert_not_called()


class CLIArgsTests(unittest.TestCase):
    def test_run_once_flag(self):
        args = sc.parse_args(["--run-once"])
        self.assertTrue(args.run_once)

    def test_once_is_an_alias_for_run_once(self):
        args = sc.parse_args(["--once"])
        self.assertTrue(args.run_once)

    def test_run_once_defaults_to_false(self):
        args = sc.parse_args([])
        self.assertFalse(args.run_once)

    def test_dry_run_flag_default_false(self):
        args = sc.parse_args([])
        self.assertFalse(args.dry_run)

    def test_dry_run_flag_enabled(self):
        args = sc.parse_args(["--dry-run"])
        self.assertTrue(args.dry_run)


class BrowserCrashRecoveryTests(unittest.TestCase):
    def test_tab_crashed_is_dead_session(self):
        self.assertTrue(sc.is_browser_session_dead(Exception("Message: tab crashed")))

    def test_invalid_session_is_dead(self):
        self.assertTrue(sc.is_browser_session_dead(Exception("invalid session id")))

    def test_chrome_not_reachable_is_dead(self):
        self.assertTrue(sc.is_browser_session_dead(Exception("chrome not reachable")))

    def test_generic_scan_error_is_not_dead(self):
        self.assertFalse(sc.is_browser_session_dead(Exception("Timeout waiting for job cards")))

    def test_cmd_run_monitor_retries_same_day_then_sleeps_after_success(self):
        crash = Exception("Message: tab crashed\n  (Session info: chrome=151.0.7922.108)")
        calls = {"scan": 0}

        def fake_scan(*_a, **_k):
            calls["scan"] += 1
            if calls["scan"] == 1:
                raise crash

        fake_driver = mock.MagicMock()
        with mock.patch.object(sc, "start_health_server"), \
             mock.patch.object(sc.WorkerLockGuard, "acquire"), \
             mock.patch.object(sc.WorkerLockGuard, "maybe_renew", return_value=True), \
             mock.patch.object(sc.WorkerLockGuard, "release"), \
             mock.patch.object(sc, "initialize_driver", return_value=fake_driver), \
             mock.patch.object(sc, "setup_session", return_value=True), \
             mock.patch.object(sc, "run_scrape_cycle", side_effect=fake_scan), \
             mock.patch.object(sc, "recreate_browser_session", return_value=fake_driver) as mock_recreate, \
             mock.patch.object(sc, "sleep_until_next_run", side_effect=KeyboardInterrupt), \
             mock.patch.object(sc, "send_error_email", return_value=True), \
             mock.patch.object(sc.Config, "SCAN_CRASH_MAX_RETRIES", 3), \
             mock.patch.object(sc.Config, "SCAN_CRASH_RETRY_SECONDS", 0), \
             mock.patch.object(sc.time, "sleep"):
            with self.assertRaises(KeyboardInterrupt):
                sc._cmd_run_monitor(
                    run_once=False, test_details=False, dry_run=True, debug_extraction=False
                )

        self.assertEqual(calls["scan"], 2)
        self.assertTrue(mock_recreate.called)

    def test_cmd_run_monitor_run_once_does_not_sleep_after_crash_retry_success(self):
        crash = Exception("Message: tab crashed")
        calls = {"scan": 0}

        def fake_scan(*_a, **_k):
            calls["scan"] += 1
            if calls["scan"] == 1:
                raise crash

        fake_driver = mock.MagicMock()
        with mock.patch.object(sc, "start_health_server"), \
             mock.patch.object(sc.WorkerLockGuard, "acquire"), \
             mock.patch.object(sc.WorkerLockGuard, "maybe_renew", return_value=True), \
             mock.patch.object(sc.WorkerLockGuard, "release"), \
             mock.patch.object(sc, "initialize_driver", return_value=fake_driver), \
             mock.patch.object(sc, "setup_session", return_value=True), \
             mock.patch.object(sc, "run_scrape_cycle", side_effect=fake_scan), \
             mock.patch.object(sc, "recreate_browser_session", return_value=fake_driver), \
             mock.patch.object(sc, "sleep_until_next_run") as mock_sleep, \
             mock.patch.object(sc, "send_error_email", return_value=True), \
             mock.patch.object(sc.Config, "SCAN_CRASH_MAX_RETRIES", 3), \
             mock.patch.object(sc.Config, "SCAN_CRASH_RETRY_SECONDS", 0), \
             mock.patch.object(sc.time, "sleep"):
            rc = sc._cmd_run_monitor(
                run_once=True, test_details=False, dry_run=True, debug_extraction=False
            )

        self.assertEqual(rc, 0)
        self.assertEqual(calls["scan"], 2)
        mock_sleep.assert_not_called()

    def test_cmd_run_monitor_exits_nonzero_when_retries_exhausted(self):
        crash = Exception("Message: tab crashed")
        fake_driver = mock.MagicMock()
        with mock.patch.object(sc, "start_health_server"), \
             mock.patch.object(sc.WorkerLockGuard, "acquire"), \
             mock.patch.object(sc.WorkerLockGuard, "maybe_renew", return_value=True), \
             mock.patch.object(sc.WorkerLockGuard, "release"), \
             mock.patch.object(sc, "initialize_driver", return_value=fake_driver), \
             mock.patch.object(sc, "setup_session", return_value=True), \
             mock.patch.object(sc, "run_scrape_cycle", side_effect=crash), \
             mock.patch.object(sc, "recreate_browser_session", return_value=fake_driver), \
             mock.patch.object(sc, "sleep_until_next_run") as mock_sleep, \
             mock.patch.object(sc, "send_error_email", return_value=True), \
             mock.patch.object(sc.Config, "SCAN_CRASH_MAX_RETRIES", 2), \
             mock.patch.object(sc.Config, "SCAN_CRASH_RETRY_SECONDS", 0), \
             mock.patch.object(sc.time, "sleep"):
            rc = sc._cmd_run_monitor(
                run_once=True, test_details=False, dry_run=True, debug_extraction=False
            )

        self.assertEqual(rc, 1)
        mock_sleep.assert_not_called()


class CategoryNotExposedNoPartialTests(unittest.TestCase):
    """Category NOT_EXPOSED must never, by itself, count as a real failure / PARTIAL."""

    def test_category_only_missing_does_not_force_partial(self):
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

    def test_apply_not_exposed_category_removes_it_from_missing_but_visible(self):
        details = {}
        metadata = {"fields_missing_but_visible": ["platform_category", "location"]}
        extraction.apply_not_exposed_category(details, platform="movemeon", metadata=metadata)
        self.assertEqual(details["platform_category_extraction_status"], "NOT_EXPOSED")
        self.assertNotIn("platform_category", metadata["fields_missing_but_visible"])
        self.assertIn("location", metadata["fields_missing_but_visible"])


if __name__ == "__main__":
    unittest.main()
