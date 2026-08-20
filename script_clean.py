"""
MoveMeOn Job Monitor — Supabase edition.

Selenium-based scraper for the MoveMeOn "Discover Jobs" portal, migrated from a
standalone MongoDB collection to the shared Supabase platform also used by the
Catalant and BTG scrapers (public.projects / scraper_runs / email_attempts /
scraper_sessions, platform='movemeon'). All database I/O goes through
``database.py`` and all field extraction/normalization goes through
``extraction.py`` — this module only drives the browser, orchestrates the
scrape lifecycle, and wires the two together.

Entry points:
    cli_main(argv=None) -> int   CLI dispatcher (see --help)
    main(argv=None) -> int       Alias for cli_main (direct execution)

``monitor.py`` is expected to be a thin wrapper:

    from script_clean import cli_main
    raise SystemExit(cli_main())
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import socket
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional

# Ensure UTF-8 output on Windows consoles (avoids UnicodeEncodeError crashes).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import database as db
import extraction

# encoding="utf-8-sig" tolerates a UTF-8 BOM, which some Windows editors write
# into .env files and which would otherwise silently break every os.getenv() call.
load_dotenv(encoding="utf-8-sig")

PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time (UTC+5)
PLATFORM = db.PLATFORM_MOVEMEON


class ConfigError(RuntimeError):
    """Missing or invalid configuration; raised at startup before any scraping."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


class Config:
    """Runtime configuration loaded from environment variables."""

    # --- MoveMeOn auth / navigation ---
    EMAIL = os.getenv("MOVEMEON_EMAIL")
    PASSWORD = os.getenv("MOVEMEON_PASSWORD")
    DASHBOARD_URL = os.getenv(
        "MOVEMEON_DASHBOARD_URL", "https://portal.movemeon.com/dashboard/candidate/jobs"
    )
    JOBS_URL = os.getenv(
        "MOVEMEON_JOBS_URL", "https://portal.movemeon.com/dashboard/candidate/jobs"
    )

    # --- Email / SMTP ---
    SMTP_SERVER = os.getenv("SMTP_SERVER")
    SMTP_PORT = _env_int("SMTP_PORT", 587)
    SENDER_EMAIL = os.getenv("SENDER_EMAIL")
    SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
    RECIPIENT_EMAILS = [
        e.strip().strip('"').strip("'")
        for e in os.getenv("RECIPIENT_EMAILS", "").split(",")
        if e.strip()
    ]
    # Prefer ERROR_RECIPIENT; keep typo/plural fallbacks for existing deployments.
    ERROR_RECIPIENT = (
        os.getenv("ERROR_RECIPIENT")
        or os.getenv("error_recipent")
        or os.getenv("ERROR_RECIPENT")
        or os.getenv("ERROR_RECIPIENTS")
        or ""
    ).strip().strip('"').strip("'")
    ERROR_RECIPIENTS = [
        e.strip().strip('"').strip("'")
        for e in ERROR_RECIPIENT.split(",")
        if e.strip()
    ]
    ERROR_EMAIL_COOLDOWN_MINUTES = _env_int("ERROR_EMAIL_COOLDOWN_MINUTES", 30)
    SUPPRESS_PROJECT_EMAILS_ON_FIRST_SCAN = _env_bool("SUPPRESS_PROJECT_EMAILS_ON_FIRST_SCAN", "false")

    # Immediate SMTP send retries (fresh connection each attempt; not DB lifecycle retries)
    SMTP_SEND_MAX_ATTEMPTS = _env_int("SMTP_SEND_MAX_ATTEMPTS", 3)
    SMTP_RETRY_DELAYS_SECONDS = (2, 5, 10)

    # --- Schedule ---
    DAILY_RUN_HOUR = _env_int("DAILY_RUN_HOUR", 23)
    DAILY_RUN_MINUTE = _env_int("DAILY_RUN_MINUTE", 0)
    MAX_AGE_MINUTES = _env_int("MAX_AGE_MINUTES", 60)

    # --- Browser ---
    HEADLESS = _env_bool("HEADLESS", "True" if os.name != "nt" else "False")
    SELENIUM_REMOTE_URL = os.getenv("SELENIUM_REMOTE_URL", "").strip()
    CHROME_BIN = os.getenv("CHROME_BIN", "").strip()
    CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH", "").strip()

    # --- Detail fetch ---
    DETAIL_FETCH_DELAY_SECONDS = _env_int("DETAIL_FETCH_DELAY_SECONDS", 2)
    DETAIL_FETCH_MAX_ATTEMPTS = _env_int("DETAIL_FETCH_MAX_ATTEMPTS", 2)

    # --- Email retry policy ---
    EMAIL_MAX_RETRIES = _env_int("EMAIL_MAX_RETRIES", 3)
    EMAIL_RETRY_BASE_MINUTES = _env_int("EMAIL_RETRY_BASE_MINUTES", 15)

    # --- Detail enrichment / backfill policy ---
    DETAIL_MAX_AUTOMATIC_ATTEMPTS = _env_int("DETAIL_MAX_AUTOMATIC_ATTEMPTS", 3)
    DETAIL_RETRY_COOLDOWN_MINUTES = _env_int("DETAIL_RETRY_COOLDOWN_MINUTES", 360)
    DETAIL_AUTO_ENRICHMENT_ENABLED = _env_bool("DETAIL_AUTO_ENRICHMENT_ENABLED", "true")

    # --- Worker lock (single-writer guarantee across replicas) ---
    WORKER_LOCK_ENABLED = _env_bool("WORKER_LOCK_ENABLED", "true")
    WORKER_LOCK_TTL_SECONDS = _env_int("WORKER_LOCK_TTL_SECONDS", 180)

    # --- Same-day recovery after Chrome/tab crashes ---
    SCAN_CRASH_MAX_RETRIES = _env_int("SCAN_CRASH_MAX_RETRIES", 3)
    SCAN_CRASH_RETRY_SECONDS = _env_int("SCAN_CRASH_RETRY_SECONDS", 45)

    # Delay before exiting on session/login failure so Railway ON_FAILURE
    # does not crash-loop every ~1 minute and flood ERROR_RECIPIENT.
    SESSION_FAIL_EXIT_DELAY_SECONDS = _env_int("SESSION_FAIL_EXIT_DELAY_SECONDS", 600)

    # --- Session cache ---
    COOKIES_FILE = os.getenv("COOKIE_FILE", "movemeon_cookies.json")

    # --- Optional Railway health check ---
    PORT = os.getenv("PORT", "").strip()


_error_email_last_sent: dict[str, float] = {}
_first_scan_done = False
_sending_error_email = False
_suppress_error_emails = False

JOB_SESSION_SELECTOR = "div.rounded-xl.border.bg-card, a[href*='/jobs/']"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

DETAIL_PAGE_FAILURE_CODES = (
    "LOGIN_REDIRECT",
    "ONBOARDING_REDIRECT",
    "ACCESS_DENIED",
    "JOB_NOT_FOUND",
    "EMPTY_APP_SHELL",
    "DETAIL_TIMEOUT",
    "UNEXPECTED_PAGE",
)


def _redact(value: Any) -> str:
    """Redact secrets (passwords, Supabase keys, etc.) from any loggable value."""
    return db.redact_db_error(value)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    value = dt or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def get_worker_owner() -> str:
    """Identity used for the platform worker lock — stable per running process/host."""
    owner = (os.getenv("RAILWAY_DEPLOYMENT_ID") or os.getenv("RAILWAY_REPLICA_ID") or "").strip()
    if owner:
        return owner
    try:
        host = socket.gethostname()
        if host:
            return host
    except Exception:
        pass
    return f"pid-{os.getpid()}"


# ---------------------------------------------------------------------------
# Environment validation
# ---------------------------------------------------------------------------

def validate_environment(*, dry_run: bool = False) -> None:
    """Validate required configuration at startup. Raises ConfigError with a clear message."""
    errors: list[str] = []

    try:
        db.get_supabase_credentials()
    except db.SupabaseConfigError as exc:
        errors.append(str(exc))

    if not Config.EMAIL:
        errors.append("MOVEMEON_EMAIL is required")
    if not Config.PASSWORD:
        errors.append("MOVEMEON_PASSWORD is required")

    if not dry_run:
        if not Config.SMTP_SERVER:
            errors.append("SMTP_SERVER is required (unless --dry-run)")
        if not Config.SENDER_EMAIL:
            errors.append("SENDER_EMAIL is required (unless --dry-run)")
        if not Config.SENDER_PASSWORD:
            errors.append("SENDER_PASSWORD is required (unless --dry-run)")
        if not Config.RECIPIENT_EMAILS:
            errors.append("RECIPIENT_EMAILS is required (unless --dry-run)")

    if errors:
        raise ConfigError("; ".join(errors))


# ---------------------------------------------------------------------------
# Worker lock
# ---------------------------------------------------------------------------

class WorkerLockGuard:
    """
    Atomic single-writer lock for this platform, backed by
    ``scraper_sessions.worker_lock_*`` columns via database.py RPCs.

    Fails closed: if the lock cannot be acquired (held by another owner), an
    exception propagates and the caller must not proceed with scraping. The
    only exception is when ``allow_missing_schema`` is set (dry-run only) and
    the worker-lock migration has not been applied yet — the lock is skipped
    with a warning instead of hard-failing a diagnostic run.
    """

    def __init__(
        self,
        platform: str,
        owner: str,
        ttl_seconds: int,
        *,
        enabled: bool = True,
        allow_missing_schema: bool = False,
    ) -> None:
        self.platform = platform
        self.owner = owner
        self.ttl_seconds = max(int(ttl_seconds or 180), 1)
        self.enabled = enabled
        self.allow_missing_schema = allow_missing_schema
        self.acquired = False
        self._last_heartbeat = 0.0

    def acquire(self, *, wait: bool = True, max_wait_seconds: Optional[int] = None) -> bool:
        """
        Acquire the platform worker lock.

        When ``wait=True`` (default), if another owner holds the lock we sleep
        until shortly after ``expires_at`` and retry, instead of exiting and
        crash-looping under Railway ON_FAILURE.
        """
        if not self.enabled:
            print("  Worker lock disabled (WORKER_LOCK_ENABLED=false).")
            return False

        # Wait at least one full TTL (+ cushion) for a dead holder to expire.
        wait_budget = max_wait_seconds
        if wait_budget is None:
            wait_budget = max(int(self.ttl_seconds) + 60, 240)
        deadline = time.time() + max(int(wait_budget), 0)

        while True:
            try:
                result = db.acquire_worker_lock(self.platform, self.owner, self.ttl_seconds)
            except db.WorkerLockError as exc:
                if self.allow_missing_schema and "migration" in str(exc).lower():
                    print(f"  WARNING: worker lock schema missing; continuing without a lock. {exc}")
                    return False
                raise

            if result.get("acquired"):
                self.acquired = True
                self._last_heartbeat = time.monotonic()
                print(f"  Worker lock acquired (owner={self.owner}, ttl={self.ttl_seconds}s).")
                return True

            holder = result.get("owner")
            expires_at = result.get("expires_at")
            msg = (
                f"Worker lock for platform={self.platform!r} is held by "
                f"owner={holder!r} until {expires_at}."
            )
            if not wait or time.time() >= deadline:
                raise db.WorkerLockError(
                    f"{msg} Refusing to start another worker (fail closed)."
                )

            sleep_s = 15.0
            try:
                if expires_at:
                    text = str(expires_at)
                    if text.endswith("Z"):
                        text = text[:-1] + "+00:00"
                    exp_dt = datetime.fromisoformat(text)
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    sleep_s = max((exp_dt - datetime.now(timezone.utc)).total_seconds() + 3.0, 5.0)
            except Exception:
                sleep_s = 15.0

            remaining = deadline - time.time()
            if remaining <= 0:
                raise db.WorkerLockError(
                    f"{msg} Refusing to start another worker (fail closed)."
                )
            sleep_s = min(sleep_s, remaining, 60.0)
            print(
                f"  {msg} Waiting {sleep_s:.0f}s for it to expire "
                f"(stale lock from a previous deploy/crash)..."
            )
            time.sleep(sleep_s)

    def maybe_renew(self) -> None:
        if not self.acquired:
            return
        if time.monotonic() - self._last_heartbeat < max(self.ttl_seconds / 2.0, 20.0):
            return
        try:
            db.renew_worker_lock(self.platform, self.owner, self.ttl_seconds)
            self._last_heartbeat = time.monotonic()
        except Exception as exc:
            print(f"  WARNING: failed to renew worker lock: {_redact(exc)}")

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            db.release_worker_lock(self.platform, self.owner)
            print("  Worker lock released.")
        except Exception as exc:
            print(f"  WARNING: failed to release worker lock: {_redact(exc)}")
        finally:
            self.acquired = False


# ---------------------------------------------------------------------------
# Session management (Supabase + local file fallback)
# ---------------------------------------------------------------------------

def _collect_safe_local_storage(driver) -> dict:
    """Capture LocalStorage keys needed for session recovery; never store credentials."""
    storage: dict[str, str] = {}
    try:
        keys = driver.execute_script("return Object.keys(window.localStorage || {});") or []
    except Exception:
        return storage
    deny_tokens = ("password", "passwd", "smtp", "secret_key", "service_role")
    allow_tokens = (
        "sb-",
        "supabase",
        "auth",
        "session",
        "token",
        "clerk",
        "__clerk",
        "__session",
        "__client",
        "nextauth",
        "movemeon",
        "user",
        "access",
        "refresh",
        "oidc",
        "pkce",
    )
    for key in keys:
        if not isinstance(key, str):
            continue
        lowered = key.lower()
        if any(tok in lowered for tok in deny_tokens):
            continue
        # Clerk / portal apps often use underscore-prefixed session keys.
        allowed = any(tok in lowered for tok in allow_tokens) or lowered.startswith("__")
        if not allowed:
            continue
        try:
            value = driver.execute_script(
                "return window.localStorage.getItem(arguments[0]);", key
            )
        except Exception:
            continue
        if value is not None:
            storage[key] = value
    return storage


def save_cookies(driver) -> bool:
    """Persist session cookies to Supabase (primary) and a local file (fallback)."""
    try:
        cookies = driver.get_cookies()
    except Exception as exc:
        print(f"  WARNING: could not read cookies from driver: {_redact(exc)}")
        return False

    local_storage = _collect_safe_local_storage(driver)

    metadata = {
        "cookie_count": len(cookies),
        "source": "selenium",
        "session_state": "active" if cookies else "cleared",
        "local_storage_keys": sorted(local_storage.keys()),
        "saved_by": get_worker_owner(),
    }
    try:
        db.save_scraper_session(
            PLATFORM, cookies, metadata=metadata, local_storage=local_storage
        )
        print(
            f"  Session saved to Supabase ({len(cookies)} cookie(s)"
            f", {len(local_storage)} localStorage key(s))."
        )
    except Exception as exc:
        print(f"  WARNING: could not save session to Supabase: {_redact(exc)}")

    try:
        with open(Config.COOKIES_FILE, "w", encoding="utf-8") as fh:
            json.dump({"cookies": cookies, "local_storage": local_storage}, fh)
    except Exception as exc:
        print(f"  WARNING: could not write local cookie file: {_redact(exc)}")

    return True


def _load_cookies_from_supabase() -> tuple[Optional[list], Optional[dict]]:
    try:
        session = db.load_scraper_session(PLATFORM)
    except Exception as exc:
        print(f"  WARNING: could not load session from Supabase: {_redact(exc)}")
        return None, None
    if not session:
        return None, None
    data = session.get("session_data") or {}
    cookies = data.get("cookies")
    local_storage = data.get("local_storage") if isinstance(data.get("local_storage"), dict) else None
    if isinstance(cookies, list) and cookies:
        print(f"  Loaded {len(cookies)} cookie(s) from Supabase session.")
        return cookies, local_storage
    return None, None


def _load_cookies_from_file() -> tuple[Optional[list], Optional[dict]]:
    if not os.path.exists(Config.COOKIES_FILE):
        return None, None
    try:
        with open(Config.COOKIES_FILE, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return None, None
    if isinstance(payload, list):
        cookies = payload
        local_storage = None
    elif isinstance(payload, dict):
        cookies = payload.get("cookies")
        local_storage = payload.get("local_storage")
    else:
        return None, None
    if not cookies:
        return None, None
    print(f"  Loaded {len(cookies)} cookie(s) from local file ({Config.COOKIES_FILE}).")
    return cookies, local_storage


def load_cookies(driver) -> bool:
    """Load cookies from Supabase first, then the local file. Never prints raw cookie values."""
    cookies, local_storage = _load_cookies_from_supabase()
    if not cookies:
        cookies, local_storage = _load_cookies_from_file()
    if not cookies:
        return False

    try:
        driver.get("https://portal.movemeon.com/")
        time.sleep(3)
        driver.delete_all_cookies()

        restored = 0
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            domain = cookie.get("domain") or ""
            if "movemeon.com" not in domain:
                continue
            clean_cookie = dict(cookie)
            same_site = clean_cookie.get("sameSite")
            if same_site not in ("Strict", "Lax", "None"):
                clean_cookie.pop("sameSite", None)
            try:
                driver.add_cookie(clean_cookie)
                restored += 1
            except Exception:
                continue
        print(f"  Restored {restored} cookie(s) for movemeon.com.")

        if local_storage:
            try:
                for key, value in local_storage.items():
                    driver.execute_script(
                        "window.localStorage.setItem(arguments[0], arguments[1]);", key, value
                    )
                print(f"  Restored {len(local_storage)} localStorage key(s).")
            except Exception as exc:
                print(f"  WARNING: failed to restore localStorage: {_redact(exc)}")

        # Clerk/auth apps often need a navigation after cookie+storage injection.
        try:
            driver.get(Config.JOBS_URL)
            time.sleep(3)
        except Exception:
            pass
        return restored > 0
    except Exception as exc:
        print(f"  WARNING: error restoring session: {_redact(exc)}")
        return False


def _save_page_debug(driver, basename: str) -> None:
    """Save screenshot + HTML dump for post-mortem debugging. Never dumps cookies."""
    try:
        driver.save_screenshot(f"{basename}.png")
        print(f"  Saved screenshot: {basename}.png")
    except Exception as exc:
        print(f"  Could not save screenshot: {type(exc).__name__}")
    try:
        with open(f"{basename}.html", "w", encoding="utf-8") as fh:
            fh.write(driver.page_source)
        print(f"  Saved HTML: {basename}.html")
    except Exception as exc:
        print(f"  Could not save HTML: {type(exc).__name__}")


def _log_page_state(driver, label: str = "page") -> None:
    try:
        print(f"  [{label}] URL: {driver.current_url}")
        print(f"  [{label}] Title: {driver.title}")
        body_text = driver.find_element(By.TAG_NAME, "body").text[:2000]
        print(f"  [{label}] Body (first 2000 chars):\n{body_text}")
    except Exception as exc:
        print(f"  [{label}] Could not read page state: {type(exc).__name__}")


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def _find_visible_input(driver, selectors: list, timeout: int = 20):
    def _locator(d):
        for selector in selectors:
            for elem in d.find_elements(By.CSS_SELECTOR, selector):
                if elem.is_displayed():
                    return elem
        return False

    return WebDriverWait(driver, timeout).until(_locator)


def _click_visible_by_texts(
    driver,
    texts: list[str],
    *,
    timeout: int = 8,
    exclude_texts: Optional[list[str]] = None,
    exact: bool = False,
) -> bool:
    """Click the first visible element whose text matches any of ``texts``."""
    lowered = [t.strip().lower() for t in texts if t and t.strip()]
    excluded = [t.strip().lower() for t in (exclude_texts or []) if t and t.strip()]
    if not lowered:
        return False
    end = time.time() + max(timeout, 1)
    while time.time() < end:
        # Prefer a broad DOM scan — MoveMeOn nests labels in spans/divs.
        try:
            candidates = driver.find_elements(
                By.XPATH,
                "//button|//a|//*[@role='button']|//span|//div|//p|//label",
            )
        except Exception:
            candidates = []
        for elem in candidates:
            try:
                if not elem.is_displayed():
                    continue
                label = (elem.text or "").strip().lower()
                if not label:
                    label = (elem.get_attribute("aria-label") or "").strip().lower()
                if not label:
                    continue
                if any(ex in label for ex in excluded):
                    continue
                matched = False
                for text in lowered:
                    if exact and label == text:
                        matched = True
                        break
                    if not exact and text in label:
                        matched = True
                        break
                if not matched:
                    continue
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'}); arguments[0].click();",
                    elem,
                )
                return True
            except Exception:
                continue
        time.sleep(0.4)
    return False


def _set_input_value(driver, elem, value: str) -> None:
    """Set an input value reliably for React/Clerk controlled fields."""
    try:
        elem.click()
    except Exception:
        pass
    try:
        elem.send_keys(Keys.CONTROL, "a")
        elem.send_keys(Keys.BACK_SPACE)
    except Exception:
        try:
            elem.clear()
        except Exception:
            pass
    elem.send_keys(value)
    # Nudge frameworks that listen for input events. Do not overwrite .value via JS —
    # that often desyncs React/Clerk controlled state.
    try:
        driver.execute_script(
            """
            const el = arguments[0];
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            """,
            elem,
        )
    except Exception:
        pass


def _password_field_visible(driver) -> bool:
    try:
        for sel in (
            "input[type='password']",
            "input[name*='password' i]",
            "input[placeholder*='password' i]",
            "input[autocomplete='current-password']",
            "#password",
        ):
            for elem in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if elem.is_displayed():
                        return True
                except Exception:
                    continue
    except Exception:
        return False
    return False


def _click_login_submit(driver) -> bool:
    """Click the real password-form submit control (not Google / magic-link)."""
    try:
        for elem in driver.find_elements(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']"):
            try:
                if not elem.is_displayed() or not elem.is_enabled():
                    continue
                label = ((elem.text or "") + " " + (elem.get_attribute("aria-label") or "")).strip().lower()
                if any(x in label for x in ("google", "magic", "forgot", "sign up", "signup")):
                    continue
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'}); arguments[0].click();",
                    elem,
                )
                print("  Clicked submit button[type=submit].")
                return True
            except Exception:
                continue
    except Exception:
        pass

    # On MoveMeOn the password-form CTA is often literally "Log in with password".
    if _click_visible_by_texts(
        driver,
        [
            "log in with password",
            "login with password",
            "continue",
            "log in",
            "sign in",
            "submit",
        ],
        timeout=4,
        exclude_texts=["google", "magic", "forgot", "sign up", "signup", "instead"],
    ):
        print("  Clicked login CTA.")
        return True
    return False


def _log_auth_controls(driver, label: str = "auth") -> None:
    """Print visible auth control labels to help diagnose login UI changes."""
    try:
        labels = []
        for elem in driver.find_elements(
            By.XPATH, "//button|//a|//*[@role='button']|//input"
        ):
            try:
                if not elem.is_displayed():
                    continue
                text = (elem.text or "").strip()
                aria = (elem.get_attribute("aria-label") or "").strip()
                typ = (elem.get_attribute("type") or "").strip()
                name = (elem.get_attribute("name") or "").strip()
                placeholder = (elem.get_attribute("placeholder") or "").strip()
                bit = text or aria or " ".join(x for x in (typ, name, placeholder) if x)
                if bit:
                    labels.append(bit[:80])
            except Exception:
                continue
        if labels:
            print(f"  [{label}] Visible controls: {labels[:20]}")
    except Exception as exc:
        print(f"  [{label}] Could not list controls: {type(exc).__name__}")


def _clear_browser_auth_state(driver) -> None:
    """Drop cookies/localStorage so a stale half-session cannot block password login."""
    try:
        driver.delete_all_cookies()
    except Exception:
        pass
    try:
        driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
    except Exception:
        pass


def _skip_onboarding_if_present(driver, label: str = "onboarding") -> None:
    print(f"  [{label}] Redirected to onboarding page. Attempting to click 'Skip'...")
    try:
        time.sleep(5)
        skip_btn = None
        for selector in (
            "//button[contains(text(), 'Skip')]",
            "//a[contains(text(), 'Skip')]",
            "//*[contains(text(), 'Skip')]",
            "button[class*='skip']",
            "a[class*='skip']",
        ):
            try:
                if selector.startswith("//"):
                    candidate = driver.find_element(By.XPATH, selector)
                else:
                    candidate = driver.find_element(By.CSS_SELECTOR, selector)
                if candidate and candidate.is_displayed():
                    skip_btn = candidate
                    break
            except Exception:
                continue
        if skip_btn:
            driver.execute_script("arguments[0].click();", skip_btn)
            print(f"  [{label}] Clicked 'Skip' on onboarding page.")
            time.sleep(5)
        else:
            print(f"  [{label}] Could not find 'Skip' button.")
    except Exception as exc:
        print(f"  [{label}] Failed to skip onboarding: {_redact(exc)}")


def _navigate_to_search(driver) -> None:
    """Navigate to the Curated/Discover Jobs page, skipping onboarding if redirected."""
    driver.get(Config.JOBS_URL)
    time.sleep(8)
    if "onboarding" in (driver.current_url or "").lower():
        _skip_onboarding_if_present(driver, label="navigation")


def perform_login(driver) -> bool:
    """MoveMeOn login: ensure password form, fill email+password, submit → dashboard."""
    try:
        # Stale cookies often leave the sign-in page in a half-auth state
        # ("Already have an account? Log out").
        _clear_browser_auth_state(driver)

        print("  Navigating to signin page...")
        driver.get("https://portal.movemeon.com/auth/signin")
        try:
            driver.maximize_window()
        except Exception:
            pass
        time.sleep(4)

        if "dashboard" in (driver.current_url or ""):
            print("  Already logged in.")
            return True

        _log_auth_controls(driver, "signin")

        # Clear residual "Log out" / half-session UI before filling credentials.
        if _click_visible_by_texts(driver, ["log out", "logout", "sign out"], timeout=3):
            print("  Clicked Log out to clear stale session state.")
            time.sleep(2)
            _clear_browser_auth_state(driver)
            driver.get("https://portal.movemeon.com/auth/signin")
            time.sleep(4)
            _log_auth_controls(driver, "signin-after-logout")

        # Only click the method chooser when the password field is not already shown.
        # When the password form is open, "Log in with password" is the SUBMIT CTA.
        if not _password_field_visible(driver):
            if _click_visible_by_texts(
                driver,
                ["log in with password", "login with password", "use password"],
                timeout=10,
                exclude_texts=["google", "magic", "instead"],
            ):
                print("  Selected password login method.")
                time.sleep(2)

        print(f"  Entering email: {Config.EMAIL}...")
        email_field = _find_visible_input(
            driver,
            [
                "input[type='email']",
                "input[name*='email' i]",
                "input[placeholder*='email' i]",
                "input[autocomplete='username']",
                "input[autocomplete='email']",
                "#email",
            ],
            timeout=25,
        )
        _set_input_value(driver, email_field, Config.EMAIL)

        if not _password_field_visible(driver):
            if not _click_visible_by_texts(
                driver,
                ["continue", "next"],
                timeout=3,
                exclude_texts=["google", "magic", "password"],
            ):
                try:
                    email_field.send_keys(Keys.ENTER)
                except Exception:
                    pass
            time.sleep(2)
            if not _password_field_visible(driver) and _click_visible_by_texts(
                driver,
                ["log in with password", "login with password", "use password"],
                timeout=8,
                exclude_texts=["google", "magic", "instead"],
            ):
                print("  Selected password login method (after email).")
                time.sleep(2)

        print("  Waiting for password field...")
        password_field = _find_visible_input(
            driver,
            [
                "input[type='password']",
                "input[name*='password' i]",
                "input[placeholder*='password' i]",
                "input[autocomplete='current-password']",
                "#password",
            ],
            timeout=25,
        )
        print("  Entering password...")
        _set_input_value(driver, password_field, Config.PASSWORD)

        if not _click_login_submit(driver):
            print("  No submit CTA found; pressing ENTER on password field.")
            password_field.send_keys(Keys.ENTER)

        print("  Waiting for post-login redirect...")
        WebDriverWait(driver, 45).until(
            lambda d: "dashboard" in (d.current_url or "")
            or "onboarding" in (d.current_url or "").lower()
            or bool(d.find_elements(By.CSS_SELECTOR, "[href*='dashboard'], a[href*='/jobs/']"))
        )
        print(f"  Login successful. Current URL: {driver.current_url}")

        if "onboarding" in (driver.current_url or "").lower():
            _skip_onboarding_if_present(driver)

        save_cookies(driver)
        _navigate_to_search(driver)

        print("  Verifying job cards are visible...")
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, JOB_SESSION_SELECTOR))
        )
        print("  Session established -> Discover Jobs")
        return True

    except Exception as exc:
        print(f"  Login failed: {type(exc).__name__}: {_redact(exc)}")
        _log_page_state(driver, "login")
        _log_auth_controls(driver, "login-failed")
        _save_page_debug(driver, "movemeon_login_failed")
        send_error_email(
            "Login Failed",
            exc,
            details="Full login attempt failed",
            operation="MoveMeOn authentication",
            traceback_text=traceback.format_exc(),
        )
        return False


def setup_session(driver) -> bool:
    """Try cookie-restored session first, fall back to full interactive login."""
    if load_cookies(driver):
        _navigate_to_search(driver)
        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, JOB_SESSION_SELECTOR))
            )
            print("  Logged in via cookies -> Discover Jobs")
            return True
        except Exception:
            _log_page_state(driver, "cookie session")
            _save_page_debug(driver, "cookie_session_failed")
            print("  Cookies may be invalid or expired; clearing and falling back to full login.")
            _clear_browser_auth_state(driver)
            try:
                db.delete_scraper_session(PLATFORM)
                print("  Cleared stale Supabase scraper session for movemeon.")
            except Exception as exc:
                print(f"  WARNING: could not clear Supabase session: {_redact(exc)}")
    return perform_login(driver)


# ---------------------------------------------------------------------------
# Card extraction
# ---------------------------------------------------------------------------

def extract_project_data(card) -> Optional[dict]:
    """
    Extract one MoveMeOn job card into a shared-schema project dict via
    extraction.normalize_movemeon_card_project. The visible title is kept as
    scraped (no company suffix appended); company goes to raw_data.
    """
    try:
        title_elem = None
        for selector in ("h3 a", "a[href*='/jobs/']", "a.title", ".job-title a"):
            try:
                candidate = card.find_element(By.CSS_SELECTOR, selector)
                if candidate:
                    title_elem = candidate
                    break
            except Exception:
                continue
        if not title_elem:
            return None

        title = (title_elem.text or "").strip()
        url = title_elem.get_attribute("href")
        if not title or not url:
            return None

        location = ""
        budget_text = ""
        engagement_or_duration = ""
        company = ""
        language = ""
        remote_or_onsite = ""
        metadata_items_raw: list[str] = []

        language_tokens = {
            "english", "french", "german", "spanish", "dutch", "italian",
            "portuguese", "mandarin", "arabic", "japanese", "korean", "hindi",
        }
        engagement_tokens = (
            "permanent", "contract", "full-time", "full time", "part-time",
            "part time", "temporary", "interim", "freelance", "fractional",
        )
        location_hints = (
            "united kingdom", "uk", "london", "singapore", "germany", "france",
            "netherlands", "remote", "hybrid", "on-site", "onsite", "europe",
            "united states", "usa", "uae", "dubai", "zurich", "geneva",
            "hong kong", "australia", "ireland", "belgium", "switzerland",
        )

        def _looks_like_budget(text: str) -> bool:
            if re.search(r"[£$€]", text):
                return True
            if re.search(r"(?i)\b(salary|budget|day rate|daily rate|hourly)\b", text):
                return True
            if re.search(r"(?i)\b\d+\s*[-–—to]+\s*\d+\s*[kK]\b", text):
                return True
            if re.search(r"(?i)\b\d+[kK]\b", text) and re.search(r"(?i)(/|per)\s*(day|hour|annum|year)", text):
                return True
            return False

        try:
            metadata_items = card.find_elements(
                By.CSS_SELECTOR,
                "div.flex.items-center.gap-1, .metadata-item, [class*='text-slate-500']",
            )
            for item in metadata_items:
                text = (item.text or "").strip()
                if not text:
                    continue
                # Skip nested duplicates / very long blobs
                if len(text) > 120:
                    continue
                metadata_items_raw.append(text)
                lowered = text.lower().strip()

                if lowered in language_tokens:
                    language = text
                    continue
                if _looks_like_budget(text):
                    budget_text = text
                    continue
                if any(tok in lowered for tok in engagement_tokens):
                    engagement_or_duration = text
                    continue
                if any(tok in lowered for tok in ("remote", "hybrid", "on-site", "onsite", "in office", "in-office")):
                    if not remote_or_onsite:
                        remote_or_onsite = text
                    # Hybrid/Remote often co-located with a city — keep as location too
                    if not location and any(
                        h in lowered
                        for h in location_hints
                        if h not in ("remote", "hybrid", "on-site", "onsite")
                    ):
                        location = text
                    continue
                if any(h in lowered for h in location_hints) or "," in text:
                    if not location:
                        location = text
                    continue
                # Remaining short tokens may be company names (avoid geo/work-mode leftovers)
                if (
                    not company
                    and not any(h in lowered for h in location_hints)
                    and lowered not in ("in office", "in-office", "office")
                ):
                    company = text
        except Exception:
            pass

        # If we never found a company but first non-language/non-budget chip looks corporate, keep empty.
        # Prefer geo in location over company when only one geo-like chip existed.
        if company and any(h in company.lower() for h in location_hints):
            if not location:
                location = company
            company = ""

        description = ""
        try:
            desc_elem = card.find_element(
                By.CSS_SELECTOR, "p.text-slate-600, .description, .summary"
            )
            description = (desc_elem.text or "").strip()
        except Exception:
            pass

        data_job_id = None
        try:
            data_job_id = card.get_attribute("data-job-id") or card.get_attribute("data-id")
        except Exception:
            pass

        raw = {
            "title": title,
            "company": company,
            "url": url,
            "source_url": url,
            "location": location,
            "budget_text": budget_text,
            "engagement_type": engagement_or_duration,
            "remote_or_onsite": remote_or_onsite,
            "description": description,
            "short_description": description,
            "id": data_job_id,
            "metadata_items": metadata_items_raw,
            "raw_data": {"movemeon_language": language} if language else {},
        }
        project = extraction.normalize_movemeon_card_project(raw)
        project["detected_at"] = datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S")
        return project
    except Exception:
        return None


def scan_for_projects(driver) -> tuple[list, int]:
    """Scan the Discover Jobs page. Returns (valid_projects, cards_found_count)."""
    try:
        time.sleep(5)
        WebDriverWait(driver, 25).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, JOB_SESSION_SELECTOR))
        )

        cards = driver.find_elements(By.CSS_SELECTOR, "div.rounded-xl.border.bg-card")
        if not cards:
            cards = driver.find_elements(
                By.XPATH, "//a[contains(@href, '/jobs/')]/ancestor::div[contains(@class, 'border')]"
            )

        projects = []
        for card in cards:
            project = extract_project_data(card)
            if project and project.get("title") and project.get("project_id"):
                projects.append(project)

        print(f"  Extracted {len(projects)} valid job(s) from {len(cards)} card(s).")
        return projects, len(cards)
    except TimeoutException:
        print("  Timeout waiting for job cards.")
        return [], 0
    except Exception as exc:
        print(f"  Error scanning jobs: {_redact(exc)}")
        return [], 0


# ---------------------------------------------------------------------------
# Detail page fetch
# ---------------------------------------------------------------------------

def classify_detail_page_state(driver) -> str:
    """Classify the current detail page without assuming success or failure blindly."""
    url = (driver.current_url or "").lower()
    if "signin" in url or "/login" in url or "/auth" in url:
        return "LOGIN_REDIRECT"
    if "onboarding" in url:
        return "ONBOARDING_REDIRECT"

    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        body_text = ""
    lowered = body_text.lower()

    if any(tok in lowered for tok in ("access denied", "not authorized", "unauthorized", "forbidden")):
        return "ACCESS_DENIED"
    if any(
        tok in lowered
        for tok in ("job not found", "no longer available", "job no longer exists", "page not found", "404")
    ):
        return "JOB_NOT_FOUND"
    if len(body_text.strip()) < 40:
        return "EMPTY_APP_SHELL"
    return "OK"


def wait_for_movemeon_job_detail_page(driver, timeout: int = 25) -> tuple[bool, str]:
    """Wait for a recognizable job-detail signal, then classify the resulting page."""
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: (
                d.find_elements(By.TAG_NAME, "h1")
                or d.find_elements(
                    By.CSS_SELECTOR,
                    "[class*='description' i], [class*='job-detail' i], [data-testid*='job-details' i]",
                )
                or d.find_elements(
                    By.XPATH,
                    "//*[contains(translate(text(),"
                    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'apply')]",
                )
                or "signin" in (d.current_url or "").lower()
                or "onboarding" in (d.current_url or "").lower()
                or "/login" in (d.current_url or "").lower()
            )
        )
    except TimeoutException:
        return False, "DETAIL_TIMEOUT"
    except Exception:
        return False, "DETAIL_TIMEOUT"

    state = classify_detail_page_state(driver)
    return (state == "OK"), state


def _expand_read_more(driver) -> bool:
    """Click a single 'Read more' / 'Show more' style control if present."""
    for btn_xpath in (
        "//button[contains(translate(text(),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'read more')]",
        "//button[contains(translate(text(),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'show more')]",
        "//a[contains(translate(text(),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'read more')]",
        "//a[contains(translate(text(),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'show more')]",
        "//*[contains(translate(text(),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'see more')]",
        "//*[contains(translate(text(),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'view more')]",
    ):
        try:
            btn = driver.find_element(By.XPATH, btn_xpath)
            if btn.is_displayed():
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(2)
                return True
        except Exception:
            continue
    return False


def _extract_description_via_dom(driver) -> str:
    """MoveMeOn-specific section-card DOM strategies (kept from the legacy scraper)."""
    description = ""
    try:
        section_cards = driver.find_elements(
            By.CSS_SELECTOR, "div.border.bg-white.p-6, div[class*='border'][class*='bg-white'][class*='p-6']"
        )
        desc_parts = []
        for card in section_cards:
            card_text = (card.text or "").strip()
            if not card_text:
                continue
            first_line = card_text.split("\n")[0].strip()
            if first_line in ("Job Description", "Why Apply", "The Role", "About The Role", "Overview"):
                try:
                    content_div = card.find_element(
                        By.CSS_SELECTOR, "div.text-gray-700, div[class*='text-gray']"
                    )
                    text = (content_div.text or "").strip()
                except Exception:
                    lines = card_text.split("\n")
                    text = "\n".join(lines[1:]).strip()
                if text and len(text) > 30:
                    desc_parts.append(text)
        if desc_parts:
            description = "\n\n".join(desc_parts)
    except Exception:
        pass

    if not description:
        for selector in (
            "div.text-gray-700.mb-6",
            "[class*='description']",
            "[class*='job-description']",
            "[class*='job-detail']",
            "div.prose",
            "[class*='overview']",
            "article",
        ):
            try:
                for elem in driver.find_elements(By.CSS_SELECTOR, selector):
                    text = (elem.text or "").strip()
                    if len(text) > 100:
                        description = text
                        break
                if description:
                    break
            except Exception:
                continue

    return description


def _expand_show_more_controls(driver) -> None:
    """Expand '+N More' / Show more controls so Industry lists are fully visible."""
    _expand_read_more(driver)
    for xp in (
        "//button[contains(translate(normalize-space(.),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'more')]",
        "//*[self::button or self::a or self::span][contains(translate(normalize-space(.),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'+') and "
        "contains(translate(normalize-space(.),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'more')]",
    ):
        try:
            for el in driver.find_elements(By.XPATH, xp):
                text = (el.text or "").strip().lower()
                if not el.is_displayed():
                    continue
                if "more" not in text:
                    continue
                if "show less" in text:
                    continue
                try:
                    driver.execute_script("arguments[0].click();", el)
                    time.sleep(1)
                except Exception:
                    continue
        except Exception:
            continue


def fetch_job_details_once(driver, project: dict) -> dict:
    """Single detail-page fetch attempt. Returns an extraction-shaped details dict."""
    url = project.get("source_url") or project.get("url")
    if not url:
        return {
            "detail_extraction_status": "FAILED",
            "detail_failure_code": "UNEXPECTED_PAGE",
            "detail_last_error": "Project has no source_url",
            "_page_ok": False,
        }

    driver.get(url)
    time.sleep(Config.DETAIL_FETCH_DELAY_SECONDS)

    ok, state = wait_for_movemeon_job_detail_page(driver)
    if not ok:
        failure_status = "TIMEOUT" if state == "DETAIL_TIMEOUT" else "FAILED"
        return {
            "detail_extraction_status": failure_status,
            "detail_failure_code": state,
            "detail_last_error": f"Detail page state: {state} (url={url})",
            "_page_ok": False,
        }

    _expand_show_more_controls(driver)

    body_text = ""
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        pass

    details = extraction.extract_movemeon_detail_fields_from_body(
        body_text, title=project.get("title") or "", project=project
    )

    # Prefer dedicated Job Description card text when longer/cleaner
    dom_description = _extract_description_via_dom(driver)
    if dom_description and len(dom_description) > len(details.get("description") or ""):
        details["description"] = dom_description
        meta = details.setdefault("extraction_metadata", {})
        extracted_fields = meta.setdefault("fields_extracted", [])
        if "description" not in extracted_fields:
            extracted_fields.append("description")
        visible = meta.setdefault("fields_visible_on_page", [])
        if "description" not in visible:
            visible.append("description")
        meta["fields_missing_but_visible"] = [
            f for f in meta.get("fields_missing_but_visible", []) if f != "description"
        ]
        details["missing_fields"] = [
            f for f in details.get("missing_fields", []) if f != "description"
        ]
        details["detail_extraction_status"] = extraction.calculate_detail_extraction_status(
            attempted=True,
            page_ok=True,
            fields_visible=meta.get("fields_visible_on_page", []),
            fields_extracted=meta.get("fields_extracted", []),
            fields_missing_but_visible=meta.get("fields_missing_but_visible", []),
            meaningful=True,
            platform=PLATFORM,
        )

    details["_page_ok"] = True
    return details


def fetch_project_details_with_retry(driver, project: dict, max_attempts: Optional[int] = None) -> dict:
    """
    Fetch job details with retry (up to DETAIL_FETCH_MAX_ATTEMPTS), then always
    return to the jobs list page. LOGIN_REDIRECT / ACCESS_DENIED stop early
    since retrying will not help without re-authenticating.
    """
    attempts_allowed = max(int(max_attempts or Config.DETAIL_FETCH_MAX_ATTEMPTS), 1)
    result: dict = {}
    attempts_made = 0

    try:
        for attempt in range(1, attempts_allowed + 1):
            attempts_made = attempt
            try:
                result = fetch_job_details_once(driver, project)
            except Exception as exc:
                result = {
                    "detail_extraction_status": "FAILED",
                    "detail_failure_code": "UNEXPECTED_PAGE",
                    "detail_last_error": _redact(f"{type(exc).__name__}: {exc}"),
                    "_page_ok": False,
                }

            if result.get("_page_ok"):
                break

            failure_code = result.get("detail_failure_code")
            print(f"    Detail fetch attempt {attempt}/{attempts_allowed} failed: {failure_code}")
            if failure_code in ("LOGIN_REDIRECT", "ACCESS_DENIED"):
                break
            if attempt < attempts_allowed:
                time.sleep(Config.DETAIL_FETCH_DELAY_SECONDS)
    finally:
        try:
            driver.get(Config.JOBS_URL)
            time.sleep(3)
        except Exception:
            pass

    result.pop("_page_ok", None)
    result["detail_attempt_count"] = attempts_made
    result["detail_last_attempt_at"] = _utc_iso()
    if result.get("detail_extraction_status") not in ("FAILED", "TIMEOUT"):
        result["detail_completed_at"] = _utc_iso()
    return result


def compute_age_minutes(project: dict) -> Optional[float]:
    """Age of the posting in minutes if known; None when unknown (never treated as stale)."""
    posted = project.get("source_posted_at")
    if not posted:
        return None
    try:
        if isinstance(posted, datetime):
            dt = posted
        else:
            text = str(posted)
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max((now - dt).total_seconds() / 60.0, 0.0)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Email notifications
# ---------------------------------------------------------------------------

def _esc(text: Any) -> str:
    return (str(text) if text is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section_header(icon: str, title: str, color: str) -> str:
    return (
        f'<tr><td colspan="2" style="padding:14px 16px 6px;background:{color};'
        f'color:#fff;font-size:12px;font-weight:bold;'
        f'text-transform:uppercase;letter-spacing:1px;">'
        f"{icon}&nbsp; {title}</td></tr>"
    )


def _row(label: str, value: Any, alt: bool = False, bold_value: bool = False) -> str:
    if not value:
        return ""
    bg = "background:#f8f9fa;" if alt else "background:#fff;"
    bold = "font-weight:bold;" if bold_value else ""
    return (
        "<tr>"
        f"<td style='padding:9px 16px;color:#555;width:200px;{bg}border-bottom:1px solid #eee;'>"
        f"<strong>{_esc(label)}</strong></td>"
        f"<td style='padding:9px 16px;{bg}{bold}border-bottom:1px solid #eee;'>{_esc(value)}</td>"
        "</tr>"
    )


def _format_pkt_display(value: Any) -> str:
    if not value:
        return ""
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(PKT).strftime("%Y-%m-%d %H:%M:%S PKT")
    except Exception:
        return str(value)


def create_email_html(row: dict) -> str:
    """
    MoveMeOn-branded HTML email. Reads directly from a projects-table row shape
    (or an equivalent in-memory project dict) so this same function works both
    right after insert and when retrying RETRY_PENDING emails later.
    """
    title = row.get("title") or "Untitled Job"
    url = row.get("source_url") or row.get("url") or Config.DASHBOARD_URL
    description = row.get("description") or row.get("short_description") or ""
    location = row.get("location") or row.get("location_preference") or ""
    budget_text = row.get("budget_text") or "Not provided"
    engagement_type = row.get("engagement_type") or row.get("duration_text") or ""
    raw_data = row.get("raw_data") or {}
    company = raw_data.get("movemeon_company") or ""
    project_id = row.get("project_id") or row.get("id") or ""
    detected_at = _format_pkt_display(row.get("scraped_at") or row.get("first_detected_at"))
    time_posted = row.get("time_posted_text") or ""

    hdr_grad = "linear-gradient(135deg,#0056b3,#007bff)"
    sec_desc = "#0056b3"
    sec_logist = "#004085"
    sec_budget = "#28a745"
    btn_color = "#007bff"

    desc_html = ""
    if description:
        paragraphs = _esc(description).replace("\n\n", "|||").replace("\n", " ")
        desc_html = "".join(f"<p style='margin:0 0 10px;'>{p}</p>" for p in paragraphs.split("|||"))

    desc_section = ""
    if desc_html:
        desc_section = (
            _section_header("📋", "Description", sec_desc)
            + "<tr><td colspan='2' style='padding:14px 16px;background:#f9fafb;"
            "font-size:14px;line-height:1.75;color:#333;border-bottom:2px solid #e5e7eb;'>"
            f"{desc_html}</td></tr>"
        )

    logistics_rows = (
        _row("Company", company, alt=False)
        + _row("Location", location or "Not specified", alt=True)
        + _row("Job Type / Engagement", engagement_type or "Not specified", alt=False)
    )
    logistics_section = _section_header("📦", "Job Details", sec_logist) + logistics_rows

    budget_section = (
        _section_header("💰", "Compensation", sec_budget) + _row("Salary / Rate", budget_text, bold_value=True)
    )

    meta_rows = (
        _row("Posted", time_posted or "—", alt=False)
        + _row("Detected at", detected_at, alt=True)
        + _row("Job ID", project_id, alt=False)
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,Helvetica,sans-serif;color:#333;">
  <div style="max-width:700px;margin:30px auto;background:#fff;border-radius:10px;
       overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.12);">

    <div style="background:{hdr_grad};padding:24px 28px;">
      <p style="margin:0;color:rgba(255,255,255,0.75);font-size:11px;
          letter-spacing:1.5px;text-transform:uppercase;">MoveMeOn Job Monitor</p>
      <h2 style="margin:6px 0 0;color:#fff;font-size:24px;font-weight:700;">🚀 New Job Opportunity</h2>
    </div>

    <div style="padding:22px 28px 4px;">
      <h3 style="margin:0 0 10px;color:#1a252f;font-size:20px;line-height:1.4;">{_esc(title)}</h3>
    </div>

    <div style="padding:0 28px 28px;">
      <table style="width:100%;border-collapse:collapse;font-size:14px;
             border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
        {desc_section}
        {logistics_section}
        {budget_section}
        {_section_header('🕒', 'Detection Info', '#6b7280')}
        {meta_rows}
      </table>
      <div style="text-align:center;margin-top:28px;">
        <a href="{url}" style="display:inline-block;background:{btn_color};color:#fff;
                   padding:14px 36px;text-decoration:none;border-radius:6px;
                   font-weight:bold;font-size:15px;letter-spacing:0.3px;">
          View Full Job on MoveMeOn →
        </a>
      </div>
    </div>

    <div style="background:#f8f9fa;padding:14px 28px;border-top:1px solid #eee;
         font-size:12px;color:#999;text-align:center;">
      MoveMeOn Job Monitor &nbsp;|&nbsp; Automated alert &nbsp;|&nbsp; {detected_at}
    </div>
  </div>
</body></html>"""


# Transient SMTP / network errors that warrant an immediate resend with a fresh connection.
_SMTP_RETRYABLE_ERRORS = (
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
    smtplib.SMTPException,
    ConnectionResetError,
    ConnectionError,
    TimeoutError,
    OSError,
)


def send_email_with_retry(
    msg: MIMEMultipart,
    *,
    max_attempts: Optional[int] = None,
    delays: Optional[tuple] = None,
    label: str = "email",
) -> dict:
    """
    Send ``msg`` with a fresh SMTP connection on every attempt.

    Retries up to ``max_attempts`` (default Config.SMTP_SEND_MAX_ATTEMPTS) on
    connection/SMTP errors. Connections are never held open after send.

    Returns {success, ok, failure_code, error, attempts}.
    """
    attempts = max(int(max_attempts if max_attempts is not None else Config.SMTP_SEND_MAX_ATTEMPTS), 1)
    wait_schedule = tuple(delays if delays is not None else Config.SMTP_RETRY_DELAYS_SECONDS)
    last_error = None
    last_code = "UNKNOWN_ERROR"
    completed = 0

    for attempt in range(1, attempts + 1):
        completed = attempt
        try:
            with smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(Config.SENDER_EMAIL, Config.SENDER_PASSWORD)
                server.send_message(msg)
            # ``with`` closes the connection here — never held across worker sleep.
            if attempt > 1:
                print(f"  ✅ Email sent successfully on attempt {attempt}/{attempts}")
            return {
                "success": True,
                "ok": True,
                "failure_code": None,
                "error": None,
                "attempts": attempt,
            }
        except smtplib.SMTPAuthenticationError as exc:
            last_error = _redact(exc)
            last_code = "SMTP_AUTH_ERROR"
            print(f"  ⚠️ Email attempt {attempt}/{attempts} failed: {last_error}")
            # Auth will not recover by retrying with the same credentials.
            break
        except _SMTP_RETRYABLE_ERRORS as exc:
            last_error = _redact(exc)
            if isinstance(exc, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected)):
                last_code = "SMTP_CONNECT_ERROR"
            elif isinstance(exc, smtplib.SMTPException):
                last_code = "SMTP_SEND_ERROR"
            else:
                last_code = "SMTP_CONNECT_ERROR"
            print(f"  ⚠️ Email attempt {attempt}/{attempts} failed: {last_error}")
            if attempt < attempts:
                delay = wait_schedule[min(attempt - 1, len(wait_schedule) - 1)] if wait_schedule else 2
                print(f"  🔄 Retrying email in {delay} seconds...")
                time.sleep(max(int(delay), 0))
                continue
        except Exception as exc:
            last_error = _redact(f"{type(exc).__name__}: {exc}")
            last_code = "UNKNOWN_ERROR"
            print(f"  ⚠️ Email attempt {attempt}/{attempts} failed: {last_error}")
            if attempt < attempts:
                delay = wait_schedule[min(attempt - 1, len(wait_schedule) - 1)] if wait_schedule else 2
                print(f"  🔄 Retrying email in {delay} seconds...")
                time.sleep(max(int(delay), 0))
                continue

    print(f"  ❌ Email permanently failed after {completed} attempts")
    return {
        "success": False,
        "ok": False,
        "failure_code": last_code,
        "error": last_error,
        "attempts": completed,
    }


def send_notification(row: dict) -> dict:
    """
    Send the job notification email with immediate SMTP retries.
    Always returns {success, ok, message_id, failure_code, error, attempts}.
    """
    title = row.get("title") or "New Job"
    print(f"  📧 Sending email: {title[:60]}")
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🔔 MoveMeOn: {title}"[:250]
        msg["From"] = Config.SENDER_EMAIL
        msg["To"] = ", ".join(Config.RECIPIENT_EMAILS)
        message_id = make_msgid(domain="movemeon-monitor.local")
        msg["Message-ID"] = message_id
        msg.attach(MIMEText(create_email_html(row), "html"))

        result = send_email_with_retry(msg, label=title[:60])
        if result.get("success"):
            if result.get("attempts", 1) == 1:
                print(f"  📧 Email sent: {title[:60]}")
            return {
                "success": True,
                "ok": True,
                "message_id": message_id,
                "failure_code": None,
                "error": None,
                "attempts": result.get("attempts", 1),
            }
        return {
            "success": False,
            "ok": False,
            "message_id": None,
            "failure_code": result.get("failure_code"),
            "error": result.get("error"),
            "attempts": result.get("attempts"),
        }
    except Exception as exc:
        err = _redact(f"{type(exc).__name__}: {exc}")
        print(f"  ❌ Email failed (unexpected): {err}")
        return {
            "success": False,
            "ok": False,
            "message_id": None,
            "failure_code": "UNKNOWN_ERROR",
            "error": err,
            "attempts": 0,
        }


def process_project_email(row: dict) -> dict:
    """
    Create an email_attempts row BEFORE sending, then update the SAME
    projects.id with the outcome. Success -> SENT. Failure -> RETRY_PENDING
    (with backoff) or FAILED once EMAIL_MAX_RETRIES is reached.

    Immediate SMTP retries happen inside send_notification and do not create
    extra DB rows or duplicate notifications.
    """
    row_id = row.get("id")
    if not row_id:
        raise ValueError("row is missing 'id'; cannot record an email attempt")

    attempt_number = int(row.get("email_attempt_count") or 0) + 1
    now_iso = _utc_iso()
    title = row.get("title") or "New Job"
    job_url = row.get("source_url") or row.get("url") or ""

    attempt = db.create_email_attempt(row_id, attempt_number, recipients=Config.RECIPIENT_EMAILS)
    result = send_notification(row)

    if result.get("success"):
        db.complete_email_attempt_success(attempt["id"], message_id=result.get("message_id"))
        db.update_project_email_status(
            row_id,
            email_status="SENT",
            email_sent=True,
            email_sent_at=now_iso,
            email_attempt_count=attempt_number,
            email_last_attempt_at=now_iso,
            email_message_id=result.get("message_id"),
        )
    else:
        db.complete_email_attempt_failure(
            attempt["id"],
            failure_code=result.get("failure_code"),
            failure_reason=result.get("error"),
        )
        if attempt_number >= Config.EMAIL_MAX_RETRIES:
            status = "FAILED"
            next_retry = None
        else:
            status = "RETRY_PENDING"
            next_retry = db.compute_email_next_retry_at(
                attempt_number, Config.EMAIL_RETRY_BASE_MINUTES
            ).isoformat()
        db.update_project_email_status(
            row_id,
            email_status=status,
            email_failure_code=result.get("failure_code"),
            email_last_error=result.get("error"),
            email_attempt_count=attempt_number,
            email_last_attempt_at=now_iso,
            email_next_retry_at=next_retry,
        )
        # Permanent SMTP failure for this send (all immediate retries exhausted).
        smtp_attempts = result.get("attempts") or Config.SMTP_SEND_MAX_ATTEMPTS
        print("  🚨 Sending failure report to ERROR_RECIPIENT")
        send_error_email(
            "Email Sending Failed",
            result.get("error") or "Email send failed",
            details=(
                f"Operation: Send job notification email\n"
                f"Job: {title}\n"
                f"URL: {job_url}\n"
                f"DB lifecycle attempt: {attempt_number}/{Config.EMAIL_MAX_RETRIES}\n"
                f"SMTP attempts: {smtp_attempts}/{Config.SMTP_SEND_MAX_ATTEMPTS}"
            ),
            operation="Send job notification email",
            job_title=title,
            job_url=job_url,
            retry_count=smtp_attempts,
            max_retries=Config.SMTP_SEND_MAX_ATTEMPTS,
            error_type=result.get("failure_code") or "EmailSendError",
        )

    return result


# ---------------------------------------------------------------------------
# Error notification emails (sent to ERROR_RECIPIENT)
# ---------------------------------------------------------------------------

def _load_persistent_error_cooldowns() -> dict:
    """Load error-email cooldown map from scraper_sessions.metadata (survives restarts)."""
    try:
        row = db.load_scraper_session(PLATFORM)
        if not row:
            return {}
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        raw = meta.get("error_email_cooldowns") or {}
        if not isinstance(raw, dict):
            return {}
        out = {}
        for key, value in raw.items():
            try:
                out[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return out
    except Exception as exc:
        print(f"  WARNING: could not load error-email cooldowns: {_redact(exc)}")
        return {}


def _save_persistent_error_cooldown(fingerprint: str, sent_at: float) -> None:
    """Persist one cooldown timestamp so Railway restarts do not re-spam alerts."""
    try:
        cooldowns = _load_persistent_error_cooldowns()
        cooldowns[str(fingerprint)] = float(sent_at)
        # Keep the map bounded.
        if len(cooldowns) > 50:
            newest = sorted(cooldowns.items(), key=lambda kv: kv[1], reverse=True)[:50]
            cooldowns = dict(newest)
        updated = db.merge_scraper_session_metadata(
            PLATFORM, {"error_email_cooldowns": cooldowns}
        )
        if updated is None:
            # No session row yet — create a cleared row so cooldowns can persist.
            db.save_scraper_session(
                PLATFORM,
                [],
                metadata={
                    "source": "error_email_cooldown",
                    "session_state": "cleared",
                    "cookie_count": 0,
                    "error_email_cooldowns": cooldowns,
                },
            )
    except Exception as exc:
        print(f"  WARNING: could not persist error-email cooldown: {_redact(exc)}")


def send_error_email(
    context: str,
    error,
    *,
    details: str = "",
    traceback_text: str = "",
    force: bool = False,
    operation: str = "",
    job_title: str = "",
    job_url: str = "",
    retry_count: Optional[int] = None,
    max_retries: Optional[int] = None,
    error_type: str = "",
) -> bool:
    """Send an operational error email to ERROR_RECIPIENT.

    Uses ``send_email_with_retry`` for SMTP resilience. On failure only logs —
    never triggers another error email (recursion guard via ``_sending_error_email``).
    """
    global _sending_error_email

    if _sending_error_email:
        print("  ⚠️ Error email skipped — already sending an error notification (recursion guard)")
        return False
    if _suppress_error_emails:
        print("  ⚠️ Error email skipped — suppressed for diagnostic mode")
        return False
    if not Config.ERROR_RECIPIENTS:
        print("  ⚠️ Error email skipped — ERROR_RECIPIENT not configured")
        return False
    if not Config.SENDER_EMAIL or not Config.SENDER_PASSWORD:
        print("  ⚠️ Error email skipped — SENDER_EMAIL / SENDER_PASSWORD missing")
        return False

    fingerprint = context
    if not force:
        cooldown_s = max(Config.ERROR_EMAIL_COOLDOWN_MINUTES, 0) * 60
        last = _error_email_last_sent.get(fingerprint)
        if last is None:
            last = _load_persistent_error_cooldowns().get(fingerprint)
            if last is not None:
                _error_email_last_sent[fingerprint] = float(last)
        if last is not None and (time.time() - float(last)) < cooldown_s:
            print(f"  ⏳ Error email for '{context}' suppressed (cooldown active)")
            return False

    error_str = _redact(error) if error else "Unknown error"
    if not error_type:
        error_type = (
            type(error).__name__
            if error is not None and not isinstance(error, str)
            else "Error"
        )
    now_pkt = datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S PKT")
    worker_id = get_worker_owner()
    op_label = operation or context
    attempts_line = ""
    if retry_count is not None:
        cap = max_retries if max_retries is not None else Config.SMTP_SEND_MAX_ATTEMPTS
        attempts_line = f"Attempts: {retry_count}/{cap}"

    body_lines = [
        "Movemeon production worker encountered an error.",
        "",
        f"Operation: {op_label}",
    ]
    if job_title:
        body_lines.append(f"Job: {job_title}")
    if job_url:
        body_lines.append(f"URL: {job_url}")
    body_lines.extend(
        [
            f"Error Type: {error_type}",
            f"Error: {error_str}",
        ]
    )
    if attempts_line:
        body_lines.append(attempts_line)
    body_lines.extend(
        [
            f"Timestamp: {now_pkt}",
            f"Worker: {worker_id}",
            f"Platform: {PLATFORM}",
        ]
    )
    if details:
        body_lines.extend(["", "Details:", details])
    if traceback_text:
        body_lines.extend(["", "Traceback:", traceback_text[:3000]])
    plain_body = "\n".join(body_lines)

    rows_html = ""
    info_rows = [
        ("Operation", op_label),
        ("Error Type", error_type),
        ("Error", error_str),
        ("Time", now_pkt),
        ("Worker", worker_id),
        ("Platform", PLATFORM),
    ]
    if job_title:
        info_rows.insert(1, ("Job", job_title))
    if job_url:
        info_rows.insert(2 if job_title else 1, ("URL", job_url))
    if attempts_line:
        info_rows.append(("Attempts", attempts_line.replace("Attempts: ", "")))
    if details:
        info_rows.append(("Details", details))
    if traceback_text:
        info_rows.append(
            ("Traceback", f"<pre style='white-space:pre-wrap'>{_esc(traceback_text[:3000])}</pre>")
        )
    for label, val in info_rows:
        # Traceback is already HTML-escaped; other fields need escaping.
        cell = val if label == "Traceback" else _esc(str(val))
        rows_html += (
            f"<tr><td style='padding:8px 14px;color:#555;border-bottom:1px solid #eee;'>"
            f"<strong>{_esc(label)}</strong></td>"
            f"<td style='padding:8px 14px;border-bottom:1px solid #eee;'>{cell}</td></tr>"
        )

    html = f"""\
<html><body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
<div style="max-width:600px;margin:20px auto;background:#fff;border-radius:8px;overflow:hidden;
            box-shadow:0 2px 8px rgba(0,0,0,0.08);">
  <div style="background:#d32f2f;color:#fff;padding:20px 28px;">
    <h2 style="margin:0;">Movemeon Worker Error</h2>
  </div>
  <div style="padding:20px 28px;">
    <p style="color:#333;">Movemeon production worker encountered an error.</p>
    <table style="width:100%;border-collapse:collapse;">{rows_html}</table>
  </div>
  <div style="background:#f8f9fa;padding:14px 28px;border-top:1px solid #eee;
       font-size:12px;color:#999;text-align:center;">
    MoveMeOn Job Monitor &nbsp;|&nbsp; Operational error alert &nbsp;|&nbsp; {now_pkt}
  </div>
</div></body></html>"""

    subject = f"Movemeon Worker Error - {context}"[:250]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = Config.SENDER_EMAIL
    msg["To"] = ", ".join(Config.ERROR_RECIPIENTS)
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html, "html"))

    _sending_error_email = True
    try:
        result = send_email_with_retry(msg, label=f"error:{context}")
        if result.get("success"):
            sent_at = time.time()
            _error_email_last_sent[fingerprint] = sent_at
            _save_persistent_error_cooldown(fingerprint, sent_at)
            print(f"  📧 Error email sent to {Config.ERROR_RECIPIENT}")
            return True
        # Do NOT call send_error_email again — recursion guard + log only.
        print(f"  ❌ Failed to send error email to ERROR_RECIPIENT: {result.get('error')}")
        return False
    except Exception as exc:
        print(f"  ❌ Failed to send error email to ERROR_RECIPIENT: {_redact(exc)}")
        return False
    finally:
        _sending_error_email = False


def retry_pending_emails(*, dry_run: bool = False) -> dict:
    """Retry every RETRY_PENDING email that is due, for this platform."""
    try:
        rows = db.get_retryable_email_projects(
            max_attempts=Config.EMAIL_MAX_RETRIES, platform=PLATFORM, limit=50
        )
    except Exception as exc:
        print(f"  Failed to query retryable emails: {_redact(exc)}")
        raise

    if not rows:
        print("  No RETRY_PENDING emails are due.")
        return {"sent": 0, "failed": 0, "total": 0}

    print(f"  {len(rows)} email(s) due for retry.")
    sent = failed = 0
    for row in rows:
        title = row.get("title") or ""
        if dry_run:
            next_attempt = int(row.get("email_attempt_count") or 0) + 1
            print(f"  [DRY RUN] Would retry email for: {title[:60]} (attempt {next_attempt})")
            continue
        try:
            result = process_project_email(row)
            if result.get("success"):
                sent += 1
                print(f"  Sent: {title[:60]}")
            else:
                failed += 1
                print(f"  Failed: {title[:60]} -> {result.get('failure_code')}")
        except Exception as exc:
            failed += 1
            print(f"  Error retrying {title[:60]}: {_redact(exc)}")

    return {"sent": sent, "failed": failed, "total": len(rows)}


# ---------------------------------------------------------------------------
# Driver initialization
# ---------------------------------------------------------------------------

def initialize_driver():
    """Remote Selenium (if SELENIUM_REMOTE_URL set) or local Chromium/Chrome."""
    import subprocess

    from selenium.webdriver.chrome.service import Service

    proxy_url = os.getenv("PROXY_URL", "").strip()

    if Config.SELENIUM_REMOTE_URL:
        print("🔧 Initializing remote Selenium driver...", flush=True)
        print(f"Using remote Selenium: {Config.SELENIUM_REMOTE_URL}", flush=True)

        options = Options()
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(f"--user-agent={USER_AGENT}")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        if proxy_url:
            print(f"Using proxy: {proxy_url.split('@')[-1]}", flush=True)
            options.add_argument(f"--proxy-server={proxy_url}")

        profile_dir = os.getenv("CHROME_PROFILE_DIR", "").strip()
        if profile_dir:
            print(f"Using Chrome profile dir: {profile_dir}", flush=True)
            options.add_argument(f"--user-data-dir={profile_dir}")

        driver = webdriver.Remote(command_executor=Config.SELENIUM_REMOTE_URL, options=options)
        driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": USER_AGENT})
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                    window.chrome = {runtime: {}};
                """
            },
        )
        return driver

    print("🔧 Initializing local Chromium driver...", flush=True)

    options = Options()

    if Config.CHROME_BIN and os.path.exists(Config.CHROME_BIN):
        print(f"Chrome binary override: {Config.CHROME_BIN}", flush=True)
        options.binary_location = Config.CHROME_BIN
        try:
            print(subprocess.check_output([Config.CHROME_BIN, "--version"]).decode(), flush=True)
        except Exception as exc:
            print(f"Chrome version check failed: {exc}", flush=True)
    else:
        for default_path in ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"):
            if os.path.exists(default_path):
                print(f"Found default Chrome binary: {default_path}", flush=True)
                options.binary_location = default_path
                break

    if Config.CHROMEDRIVER_PATH and os.path.exists(Config.CHROMEDRIVER_PATH):
        print(f"ChromeDriver override path: {Config.CHROMEDRIVER_PATH}", flush=True)
        service = Service(Config.CHROMEDRIVER_PATH)
        try:
            print(subprocess.check_output([Config.CHROMEDRIVER_PATH, "--version"]).decode(), flush=True)
        except Exception as exc:
            print(f"ChromeDriver version check failed: {exc}", flush=True)
    else:
        default_driver = None
        for default_path in ("/usr/bin/chromedriver", "/usr/lib/chromium/chromedriver"):
            if os.path.exists(default_path):
                default_driver = default_path
                break
        if default_driver:
            print(f"Found default ChromeDriver: {default_driver}", flush=True)
            service = Service(default_driver)
        else:
            print("Using default ChromeDriver via Selenium Manager", flush=True)
            service = Service()

    if Config.HEADLESS:
        options.add_argument("--headless=new")
    else:
        print("Running in headed mode (HEADLESS=False)", flush=True)
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--renderer-process-limit=1")
    options.add_argument("--js-flags=--max-old-space-size=256")
    options.add_argument("--window-size=1280,720")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(f"user-agent={USER_AGENT}")

    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": USER_AGENT})
    return driver


BROWSER_CRASH_TOKENS = (
    "tab crashed",
    "chrome not reachable",
    "disconnected",
    "invalid session id",
    "session deleted",
    "session not created",
    "unable to discover open pages",
    "renderer",
    "chrome failed to start",
    "cannot connect to chrome",
    "no such window",
    "web view not found",
    "target window already closed",
)


def is_browser_session_dead(exc: BaseException) -> bool:
    """True when Chrome/Selenium can no longer be reused and must be recreated."""
    if isinstance(exc, WebDriverException):
        text = f"{type(exc).__name__} {exc}".lower()
        return any(tok in text for tok in BROWSER_CRASH_TOKENS)
    text = str(exc or "").lower()
    return any(tok in text for tok in BROWSER_CRASH_TOKENS)


def _safe_quit_driver(driver) -> None:
    if driver is None:
        return
    try:
        driver.quit()
    except Exception:
        pass


def recreate_browser_session(driver):
    """
    Kill the current Chrome process and start a fresh authenticated session.
    Used after a tab crash and before each new daily scan so memory cannot accumulate overnight.
    """
    print("  Recreating Chrome session...")
    _safe_quit_driver(driver)
    time.sleep(2)
    new_driver = initialize_driver()
    if not setup_session(new_driver):
        _safe_quit_driver(new_driver)
        raise RuntimeError("Failed to re-establish MoveMeOn session after Chrome restart")
    print("  Chrome session recreated.")
    return new_driver


# ---------------------------------------------------------------------------
# Optional health check server (Railway)
# ---------------------------------------------------------------------------

_health_server: Optional[HTTPServer] = None


def start_health_server() -> Optional[HTTPServer]:
    """Tiny /health HTTP server on $PORT, if set. Optional — safe to skip."""
    global _health_server
    if not Config.PORT:
        return None
    try:
        port = int(Config.PORT)
    except ValueError:
        print(f"  WARNING: PORT={Config.PORT!r} is not a valid integer; skipping health server.")
        return None

    class _HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
            if self.path in ("/health", "/"):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):  # noqa: A002 - silence default logging
            pass

    try:
        server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    except Exception as exc:
        print(f"  WARNING: could not start health server on port {port}: {exc}")
        return None

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _health_server = server
    print(f"  Health check server listening on :{port}/health")
    return server


# ---------------------------------------------------------------------------
# Daily schedule
# ---------------------------------------------------------------------------

def next_run_time() -> datetime:
    now = datetime.now(PKT)
    target = now.replace(
        hour=Config.DAILY_RUN_HOUR, minute=Config.DAILY_RUN_MINUTE, second=0, microsecond=0
    )
    if now >= target:
        target += timedelta(days=1)
    return target


def sleep_until_next_run() -> None:
    """Block until the next scheduled run. Never creates a scraper_run while sleeping."""
    target = next_run_time()
    seconds = (target - datetime.now(PKT)).total_seconds()
    hours = seconds / 3600
    print(f"💤 Next run at {target.strftime('%Y-%m-%d %H:%M:%S')} PKT (in {hours:.1f} hours)")
    time.sleep(max(seconds, 0))


# ---------------------------------------------------------------------------
# Scrape cycle
# ---------------------------------------------------------------------------

_SCRAPER_RUN_COUNTERS = (
    "cards_found",
    "cards_parsed",
    "cards_failed",
    "details_attempted",
    "details_completed",
    "details_failed",
    "projects_inserted",
    "projects_skipped",
    "emails_sent",
    "emails_failed",
    "emails_suppressed",
)


def _new_stats() -> dict:
    return {k: 0 for k in _SCRAPER_RUN_COUNTERS}


def run_scrape_cycle(
    driver,
    *,
    dry_run: bool = False,
    debug_extraction: bool = False,
    test_details: bool = False,
    lock: Optional[WorkerLockGuard] = None,
) -> dict:
    """
    One full scan: create a scraper_run (unless dry_run), scan cards, fetch
    details, insert occurrences, send emails, then complete/fail the run.
    Category NOT_EXPOSED never counts as a real failure and never causes
    PARTIAL — only genuine extraction/DB/email failures do.
    """
    global _first_scan_done
    stats = _new_stats()
    had_real_failure = False
    auth_failed = False
    run_id: Optional[str] = None

    if not dry_run:
        try:
            run = db.create_scraper_run(
                platform=PLATFORM,
                scraper_name=db.SCRAPER_NAME,
                scraper_version=db.SCRAPER_VERSION,
                metadata={"headless": Config.HEADLESS, "owner": get_worker_owner()},
            )
            run_id = run["id"]
        except Exception as exc:
            print(f"  Could not create scraper_run: {_redact(exc)}")
            raise

    try:
        _navigate_to_search(driver)
        current_url_lower = (driver.current_url or "").lower()
        if "signin" in current_url_lower or "/auth" in current_url_lower:
            auth_failed = True
            raise RuntimeError("Redirected to login while navigating to the jobs page")

        projects, cards_found = scan_for_projects(driver)
        stats["cards_found"] = cards_found
        stats["cards_parsed"] = len(projects)
        stats["cards_failed"] = max(cards_found - len(projects), 0)

        if test_details:
            print("  TEST MODE: fetching details for the first 2 jobs (no DB writes)...")
            for project in projects[:2]:
                if lock:
                    lock.maybe_renew()
                details = fetch_project_details_with_retry(driver, project)
                merged = extraction.merge_project_data(project, details)
                description = merged.get("description") or ""
                print(f"    -> {merged.get('title', '')[:60]}")
                print(
                    f"       detail_status={merged.get('detail_extraction_status')} "
                    f"description_len={len(description)}"
                )
                if debug_extraction:
                    print(f"       missing_fields={merged.get('missing_fields')}")
                    print(f"       warnings={merged.get('extraction_warnings')}")
            _navigate_to_search(driver)
            if run_id:
                db.complete_scraper_run(run_id, status="COMPLETED", **stats)
            return stats

        cold_start = (not dry_run) and (not db.platform_has_projects(PLATFORM))

        if cold_start:
            print(f"  Cold start for platform={PLATFORM}: seeding {len(projects)} job(s), no emails.")
            for project in projects:
                if lock:
                    lock.maybe_renew()
                stats["details_attempted"] += 1
                details = fetch_project_details_with_retry(driver, project)
                merged = extraction.merge_project_data(project, details)
                if merged.get("detail_extraction_status") in ("FAILED", "TIMEOUT"):
                    stats["details_failed"] += 1
                    had_real_failure = True
                else:
                    stats["details_completed"] += 1
                try:
                    db.insert_project_occurrence(
                        merged,
                        scraper_run_id=run_id,
                        email_status="SUPPRESSED",
                        email_eligible=False,
                        email_not_sent_reason="COLD_START_SEED",
                    )
                    stats["projects_inserted"] += 1
                    stats["emails_suppressed"] += 1
                except Exception as exc:
                    print(f"    DB insert failed for {merged.get('project_id')}: {_redact(exc)}")
                    had_real_failure = True
            _navigate_to_search(driver)

        else:
            for project in projects:
                if lock:
                    lock.maybe_renew()
                project_id = project.get("project_id") or project.get("id")
                if not project_id:
                    continue

                try:
                    eligible, reason, _latest = db.should_process_project(PLATFORM, project_id)
                except Exception as exc:
                    print(f"    should_process_project failed for {project_id}: {_redact(exc)}")
                    had_real_failure = True
                    continue

                if not eligible:
                    stats["projects_skipped"] += 1
                    if debug_extraction:
                        print(f"    Skip {project_id}: {reason}")
                    continue

                stats["details_attempted"] += 1
                details = fetch_project_details_with_retry(driver, project)
                merged = extraction.merge_project_data(project, details)
                if merged.get("detail_extraction_status") in ("FAILED", "TIMEOUT"):
                    stats["details_failed"] += 1
                    had_real_failure = True
                else:
                    stats["details_completed"] += 1

                age_minutes = compute_age_minutes(merged)
                if age_minutes is not None and age_minutes > Config.MAX_AGE_MINUTES:
                    email_eligible, email_status, email_reason = (
                        False,
                        "NOT_REQUIRED",
                        "OUTSIDE_NOTIFICATION_AGE_WINDOW",
                    )
                else:
                    email_eligible, email_status, email_reason = True, "PENDING", None

                if (
                    email_eligible
                    and not _first_scan_done
                    and Config.SUPPRESS_PROJECT_EMAILS_ON_FIRST_SCAN
                ):
                    email_eligible, email_status, email_reason = (
                        False, "SUPPRESSED", "FIRST_SCAN_SUPPRESSED",
                    )

                if dry_run:
                    print(
                        f"    [DRY RUN] Would insert '{merged.get('title', '')[:60]}' "
                        f"(email_eligible={email_eligible})"
                    )
                    stats["projects_inserted"] += 1
                    if email_eligible:
                        print(f"    [DRY RUN] Would send email for '{merged.get('title', '')[:60]}'")
                    else:
                        stats["emails_suppressed"] += 1
                    continue

                try:
                    inserted = db.insert_project_occurrence(
                        merged,
                        scraper_run_id=run_id,
                        email_status=email_status,
                        email_eligible=email_eligible,
                        email_not_sent_reason=email_reason,
                    )
                    stats["projects_inserted"] += 1
                except Exception as exc:
                    print(f"    DB insert failed for {project_id}: {_redact(exc)}")
                    had_real_failure = True
                    continue

                if email_eligible:
                    try:
                        result = process_project_email(inserted)
                        if result.get("success"):
                            stats["emails_sent"] += 1
                        else:
                            stats["emails_failed"] += 1
                            had_real_failure = True
                    except Exception as exc:
                        print(f"    Email processing failed for {project_id}: {_redact(exc)}")
                        stats["emails_failed"] += 1
                        had_real_failure = True
                else:
                    stats["emails_suppressed"] += 1

            _navigate_to_search(driver)

    except Exception as exc:
        if not dry_run and run_id:
            status = "AUTH_FAILED" if auth_failed else "FAILED"
            try:
                db.fail_scraper_run(
                    run_id,
                    failure_code=type(exc).__name__,
                    failure_reason=str(exc),
                    status=status,
                    **stats,
                )
            except Exception:
                pass
        print(f"  Scan cycle error: {_redact(exc)}")
        raise
    else:
        _first_scan_done = True
        if not dry_run and run_id:
            status = "PARTIAL" if had_real_failure else "COMPLETED"
            try:
                db.complete_scraper_run(run_id, status=status, **stats)
            except Exception as exc:
                print(f"  WARNING: could not complete scraper_run: {_redact(exc)}")

    return stats


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def _cmd_test_supabase() -> int:
    try:
        result = db.test_supabase_connection(cleanup=True, platform=PLATFORM)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        print(f"Supabase test failed: {_redact(exc)}")
        return 1


def _cmd_inspect_project(id_or_url: str) -> int:
    driver = initialize_driver()
    try:
        if not setup_session(driver):
            print("Failed to establish a session. See movemeon_login_failed.png/html.")
            return 1

        url = id_or_url if id_or_url.startswith("http") else f"{Config.JOBS_URL.rstrip('/')}/{id_or_url}"
        print(f"Opening: {url}")
        driver.get(url)
        time.sleep(Config.DETAIL_FETCH_DELAY_SECONDS)

        ok, state = wait_for_movemeon_job_detail_page(driver)
        print(f"Page state: {'OK' if ok else state}")

        _expand_read_more(driver)

        body_text = ""
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            pass

        extracted = extraction.extract_movemeon_detail_fields_from_body(
            body_text, title="", project={"platform": PLATFORM}
        )
        dom_description = _extract_description_via_dom(driver)
        if dom_description and len(dom_description) > len(extracted.get("description") or ""):
            extracted["description"] = dom_description

        meta = extracted.get("extraction_metadata") or {}
        print("--- Extraction diagnostics ---")
        print(f"detail_extraction_status:     {extracted.get('detail_extraction_status')}")
        print(f"fields_extracted:             {meta.get('fields_extracted')}")
        print(f"fields_visible_on_page:       {meta.get('fields_visible_on_page')}")
        print(f"fields_missing_but_visible:   {meta.get('fields_missing_but_visible')}")
        print(f"fields_not_exposed:           {meta.get('fields_not_exposed')}")
        print(f"missing_fields:               {extracted.get('missing_fields')}")
        print(f"extraction_warnings:          {extracted.get('extraction_warnings')}")
        for key in ("description", "location", "budget_text", "engagement_type", "remote_or_onsite", "industry"):
            value = extracted.get(key)
            preview = (str(value)[:200] + "...") if value and len(str(value)) > 200 else value
            print(f"{key}: {preview}")

        safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", id_or_url)[:60] or "job"
        _save_page_debug(driver, f"movemeon_inspect_{safe_name}")
        return 0
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _cmd_backfill(*, dry_run: bool = False, limit: int = 20, project_id: Optional[str] = None, retry_failed: bool = False) -> int:
    print(
        f"Backfill missing details (dry_run={dry_run}, limit={limit}, "
        f"project_id={project_id or 'ALL'}, retry_failed={retry_failed})"
    )
    try:
        rows = db.get_projects_needing_detail_enrichment(
            platform=PLATFORM, limit=limit, project_id=project_id, retry_failed=retry_failed
        )
    except Exception as exc:
        print(f"Failed to query rows needing enrichment: {_redact(exc)}")
        return 1

    if not rows:
        print("No rows need detail enrichment.")
        return 0
    print(f"Found {len(rows)} row(s) needing enrichment.")

    eligible_rows = [
        row
        for row in rows
        if db.needs_detail_enrichment(
            row,
            auto_enabled=Config.DETAIL_AUTO_ENRICHMENT_ENABLED,
            max_attempts=Config.DETAIL_MAX_AUTOMATIC_ATTEMPTS,
            cooldown_minutes=Config.DETAIL_RETRY_COOLDOWN_MINUTES,
            respect_limits=not retry_failed,
        )
    ]
    if not eligible_rows:
        print("All candidate rows are outside attempt/cooldown limits. Use --retry-failed to override.")
        return 0
    print(f"{len(eligible_rows)} row(s) eligible after attempt/cooldown checks.")

    owner = get_worker_owner()
    lock = WorkerLockGuard(
        PLATFORM, owner, Config.WORKER_LOCK_TTL_SECONDS,
        enabled=Config.WORKER_LOCK_ENABLED, allow_missing_schema=dry_run,
    )
    driver = initialize_driver()
    updated = 0
    failed = 0
    try:
        try:
            lock.acquire()
        except db.WorkerLockError as exc:
            print(f"WORKER LOCK ERROR: {exc}")
            return 1

        if not setup_session(driver):
            print("Failed to establish a session.")
            return 1

        for row in eligible_rows:
            lock.maybe_renew()
            title = row.get("title") or ""
            url = row.get("source_url") or ""
            pseudo_project = {
                "title": title,
                "source_url": url,
                "url": url,
                "platform": PLATFORM,
                "project_id": row.get("project_id"),
            }
            print(f"  Enriching: {title[:60]} ({row.get('project_id')})")
            details = fetch_project_details_with_retry(driver, pseudo_project)
            merged = extraction.merge_project_data(row, details)

            update_payload = {
                k: merged[k]
                for k in db.DETAIL_UPDATE_ALLOWED
                if k in merged
                and k
                not in (
                    "detail_extraction_status",
                    "detail_attempt_count",
                    "detail_last_attempt_at",
                    "detail_failure_code",
                    "detail_last_error",
                    "detail_completed_at",
                )
            }
            update_payload["detail_extraction_status"] = (
                details.get("detail_extraction_status") or merged.get("detail_extraction_status")
            )
            update_payload["detail_attempt_count"] = int(row.get("detail_attempt_count") or 0) + int(
                details.get("detail_attempt_count") or 1
            )
            update_payload["detail_last_attempt_at"] = details.get("detail_last_attempt_at") or _utc_iso()

            fetch_failed = update_payload["detail_extraction_status"] in ("FAILED", "TIMEOUT")
            update_payload["detail_failure_code"] = details.get("detail_failure_code") if fetch_failed else None
            update_payload["detail_last_error"] = details.get("detail_last_error") if fetch_failed else None
            if not fetch_failed:
                update_payload["detail_completed_at"] = details.get("detail_completed_at") or _utc_iso()

            if dry_run:
                print(
                    f"    [DRY RUN] Would update: status={update_payload['detail_extraction_status']} "
                    f"missing={merged.get('missing_fields')}"
                )
                continue
            try:
                db.update_project_details(row["id"], update_payload)
                updated += 1
            except Exception as exc:
                print(f"    Update failed: {_redact(exc)}")
                failed += 1

        _navigate_to_search(driver)
    finally:
        lock.release()
        try:
            driver.quit()
        except Exception:
            pass

    print(f"Backfill complete: updated={updated} failed={failed} dry_run={dry_run}")
    return 0 if failed == 0 else 1


def _cmd_retry_pending_emails(*, dry_run: bool = False) -> int:
    try:
        result = retry_pending_emails(dry_run=dry_run)
    except Exception as exc:
        print(f"Retry pending emails failed: {_redact(exc)}")
        return 1
    print(f"Retry complete: sent={result['sent']} failed={result['failed']} total={result['total']}")
    return 0 if result["failed"] == 0 else 1


def _cmd_test_error_email() -> int:
    print("=" * 60)
    print("Test error email (--test-error-email)")
    print("=" * 60)
    if Config.ERROR_RECIPIENT:
        print(f"  ERROR_RECIPIENT: {Config.ERROR_RECIPIENT}")
    else:
        print("  ERROR_RECIPIENT: NOT CONFIGURED")
        print("❌ Cannot send test — configure ERROR_RECIPIENT first")
        return 1
    ok = send_error_email(
        "Email Sending Failed",
        "Forced test alert from MoveMeOn Monitor",
        details="This is a forced test of send_error_email(). No browser or Supabase write was opened.",
        force=True,
        operation="Test error notification",
        error_type="TEST_ERROR_EMAIL",
    )
    print("✅ Test error email sent" if ok else "❌ Test error email failed")
    return 0 if ok else 1


def _cmd_test_login() -> int:
    """Local/diagnostic MoveMeOn login only — no scrape cycle, no error emails."""
    global _suppress_error_emails
    print("=" * 60)
    print("Test MoveMeOn login (--test-login)")
    print("=" * 60)
    print(f"  Email: {Config.EMAIL}")
    print(f"  Headless: {Config.HEADLESS}")
    driver = None
    _suppress_error_emails = True
    try:
        driver = initialize_driver()
        ok = perform_login(driver)
        print("✅ Login test succeeded" if ok else "❌ Login test failed")
        if ok:
            print(f"  URL: {driver.current_url}")
        return 0 if ok else 1
    finally:
        _suppress_error_emails = False
        _safe_quit_driver(driver)


def _cmd_run_monitor(*, run_once: bool, test_details: bool, dry_run: bool, debug_extraction: bool) -> int:
    print("=" * 60)
    print(f"🚀 MoveMeOn Job Monitor ({'DRY RUN' if dry_run else 'LIVE'})")
    print(f"📅 Daily run: {Config.DAILY_RUN_HOUR:02d}:{Config.DAILY_RUN_MINUTE:02d} PKT")
    print("=" * 60)

    start_health_server()

    owner = get_worker_owner()
    lock = WorkerLockGuard(
        PLATFORM, owner, Config.WORKER_LOCK_TTL_SECONDS,
        enabled=Config.WORKER_LOCK_ENABLED, allow_missing_schema=dry_run,
    )

    driver = None
    try:
        try:
            lock.acquire()
        except db.WorkerLockError as exc:
            print(f"WORKER LOCK ERROR: {exc}")
            return 1

        try:
            driver = initialize_driver()
        except Exception as init_exc:
            print(f"❌ Browser/ChromeDriver initialization failed: {_redact(init_exc)}")
            send_error_email(
                "Browser Initialization Failed",
                init_exc,
                operation="Initialize ChromeDriver",
                traceback_text=traceback.format_exc(),
            )
            return 1
        if not setup_session(driver):
            print(
                "❌ Failed to establish a session. Check movemeon_login_failed.png/html "
                "or cookie_session_failed.png/html on the server."
            )
            # Release before the delay so a replacement replica can start.
            lock.release()
            delay = max(int(Config.SESSION_FAIL_EXIT_DELAY_SECONDS), 0)
            if delay:
                print(
                    f"  Waiting {delay}s before exit to avoid Railway crash-loop / alert spam..."
                )
                time.sleep(delay)
            # perform_login already sent the Login Failed alert (with cooldown).
            return 1

        check_count = 0
        while True:
            lock.maybe_renew()
            check_count += 1
            print(f"\n{'=' * 30}")
            print(f"🔄 Check #{check_count} - {datetime.now(PKT).strftime('%Y-%m-%d %H:%M:%S')} PKT")
            print("=" * 30)

            # Fresh Chrome each scheduled day after the first — overnight memory
            # growth is the usual cause of "tab crashed".
            if check_count > 1:
                try:
                    driver = recreate_browser_session(driver)
                except Exception as rec_exc:
                    print(f"❌ Could not recreate Chrome before scan: {_redact(rec_exc)}")
                    send_error_email(
                        "Chrome Recreate Failed",
                        rec_exc,
                        details="Pre-scan browser restart failed",
                        operation="Recreate Chrome before daily scan",
                    )
                    return 1

            max_attempts = max(int(Config.SCAN_CRASH_MAX_RETRIES), 1)
            scan_ok = False
            last_error: Optional[BaseException] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    run_scrape_cycle(
                        driver,
                        dry_run=dry_run,
                        debug_extraction=debug_extraction,
                        test_details=test_details and check_count == 1 and attempt == 1,
                        lock=lock,
                    )
                    scan_ok = True
                    break
                except Exception as exc:
                    last_error = exc
                    crashed = is_browser_session_dead(exc)
                    print(f"  Scan cycle error: {_redact(exc)}")
                    if crashed and attempt < max_attempts:
                        print(
                            f"⚠️ Chrome session died (attempt {attempt}/{max_attempts}). "
                            f"Restarting browser and retrying this scan now "
                            f"(not waiting until tomorrow)."
                        )
                        try:
                            driver = recreate_browser_session(driver)
                        except Exception as rec_exc:
                            print(f"❌ Chrome restart failed: {_redact(rec_exc)}")
                            send_error_email(
                                "Chrome Restart Failed",
                                rec_exc,
                                details=f"Crash retry {attempt}/{max_attempts} — browser restart failed",
                                operation="Restart Chrome after tab crash",
                                retry_count=attempt,
                                max_retries=max_attempts,
                            )
                            return 1
                        time.sleep(max(int(Config.SCAN_CRASH_RETRY_SECONDS), 0))
                        continue
                    tb_text = traceback.format_exc() if crashed else ""
                    send_error_email(
                        "Scan Cycle Failed",
                        exc,
                        details=(
                            f"Same-day retries exhausted ({attempt}/{max_attempts})"
                            if crashed
                            else f"Scan failed (non-crash error, attempt {attempt})"
                        ),
                        traceback_text=tb_text,
                        operation="Scheduled job scan",
                        retry_count=attempt,
                        max_retries=max_attempts,
                    )
                    break

            if run_once:
                print("\n✅ Run-once complete. Exiting.")
                return 0 if scan_ok else 1

            if not scan_ok:
                print(
                    "❌ Scan did not complete. Exiting so the platform can restart "
                    "the process and retry without waiting 24 hours."
                )
                if last_error is not None:
                    print(f"   Last error: {_redact(last_error)}")
                    send_error_email(
                        "Scan Not Completed",
                        last_error,
                        details="Scan did not complete. Process exiting for container restart.",
                        traceback_text=traceback.format_exc(),
                        operation="Scheduled worker run",
                    )
                return 1

            lock.maybe_renew()
            sleep_until_next_run()

        return 0
    finally:
        lock.release()
        _safe_quit_driver(driver)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="script_clean.py",
        description="MoveMeOn Job Monitor (Supabase-backed)",
    )
    parser.add_argument(
        "--run-once", "--once", dest="run_once", action="store_true",
        help="Run a single scan cycle and exit",
    )
    parser.add_argument(
        "--test-details", action="store_true",
        help="Diagnostic: fetch and print details for a couple of jobs; no DB writes",
    )
    parser.add_argument(
        "--test-supabase", action="store_true",
        help="Validate Supabase connectivity/schema and exit",
    )
    parser.add_argument(
        "--inspect-project", metavar="ID_OR_URL",
        help="Open a single job page and print field diagnostics; no DB writes, no email",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run real extraction without permanent project writes or emails",
    )
    parser.add_argument(
        "--debug-extraction", action="store_true",
        help="Print verbose extraction diagnostics during scans",
    )
    parser.add_argument(
        "--backfill-missing-details", action="store_true",
        help="Enrich existing rows that are missing detail fields",
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="Max rows to process for --backfill-missing-details (default: 20)",
    )
    parser.add_argument(
        "--project-id", default=None,
        help="Restrict --backfill-missing-details to a single project_id",
    )
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="Include FAILED/TIMEOUT rows in backfill regardless of attempt/cooldown limits",
    )
    parser.add_argument(
        "--retry-pending-emails", action="store_true",
        help="Retry RETRY_PENDING emails that are due, then exit",
    )
    parser.add_argument(
        "--test-error-email", action="store_true",
        help="Send a test error email to ERROR_RECIPIENT and exit",
    )
    parser.add_argument(
        "--test-login", action="store_true",
        help="Test MoveMeOn login only (no scrape, no error emails) and exit",
    )
    return parser.parse_args(argv)


def cli_main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)

    try:
        validate_environment(dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 1

    try:
        if args.test_supabase:
            return _cmd_test_supabase()
        if args.inspect_project:
            return _cmd_inspect_project(args.inspect_project)
        if args.backfill_missing_details:
            return _cmd_backfill(
                dry_run=args.dry_run,
                limit=args.limit,
                project_id=args.project_id,
                retry_failed=args.retry_failed,
            )
        if args.retry_pending_emails:
            return _cmd_retry_pending_emails(dry_run=args.dry_run)
        if args.test_error_email:
            return _cmd_test_error_email()
        if args.test_login:
            return _cmd_test_login()

        return _cmd_run_monitor(
            run_once=args.run_once,
            test_details=args.test_details,
            dry_run=args.dry_run,
            debug_extraction=args.debug_extraction,
        )
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 1
    except db.WorkerLockError as exc:
        print(f"WORKER LOCK ERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n⏹️ Interrupted by user.")
        return 130


def main(argv: Optional[list] = None) -> int:
    """Backward-compatible alias for cli_main (direct `python script_clean.py` usage)."""
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(cli_main())
