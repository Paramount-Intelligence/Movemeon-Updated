"""
Supabase PostgreSQL data-access layer for MoveMeOn (and shared multi-platform scrapers).

Normal scraper runtime uses only this module for database I/O — no MongoDB.
Shares the same public.projects / scraper_runs / email_attempts / scraper_sessions
tables as Catalant and BTG (platform='movemeon').
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dotenv import load_dotenv

# encoding="utf-8-sig" tolerates a UTF-8 BOM, which some Windows editors write
# into .env files and which would otherwise silently break every os.getenv() call.
load_dotenv(encoding="utf-8-sig")

PLATFORM_MOVEMEON = "movemeon"
PLATFORM_BTG = "btg"
PLATFORM_CATALANT = "catalant"
SCRAPER_NAME = "movemeon-monitor"
SCRAPER_VERSION = "2.0.0"
DEFAULT_PLATFORM = PLATFORM_MOVEMEON

THREE_DAY_WINDOW = timedelta(days=3)

_supabase_client = None


class SupabaseConfigError(RuntimeError):
    """Missing or invalid Supabase configuration."""


class SupabaseNetworkError(RuntimeError):
    """Network / transport failure talking to Supabase."""


class SupabaseAPIError(RuntimeError):
    """Supabase API returned an error or unexpected payload."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    value = dt or _utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamptz(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SupabaseAPIError(f"Invalid timestamptz value: {text[:64]}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def redact_db_error(value: Any) -> str:
    """Redact secrets from database error strings (no keys, no session payloads)."""
    if value is None:
        return ""
    out = str(value)
    for secret in (
        os.getenv("SUPABASE_SECRET_KEY"),
        os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
        os.getenv("SUPABASE_ACCESS_TOKEN"),
        os.getenv("SUPABASE_DB_PASSWORD"),
        os.getenv("SUPABASE_DB_URL"),
        os.getenv("MOVEMEON_PASSWORD"),
        os.getenv("BTG_PASSWORD"),
        os.getenv("CATALANT_PASSWORD"),
        os.getenv("SENDER_PASSWORD"),
        os.getenv("MONGO_URI"),
    ):
        if secret:
            out = out.replace(secret, "[REDACTED]")
    out = re.sub(
        r"(?i)(sb_secret_|sb_publishable_|eyJ)[A-Za-z0-9._\-]+",
        "[REDACTED_KEY]",
        out,
    )
    out = re.sub(
        r"(?i)(apikey|authorization|service[_-]?role|secret[_-]?key)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        out,
    )
    out = re.sub(
        r"(postgresql(?:\+?\w*)?://)([^:@/\s]+):([^@/\s]+)@",
        r"\1[REDACTED_USER]:[REDACTED_PASSWORD]@",
        out,
        flags=re.IGNORECASE,
    )
    return out


def get_supabase_credentials() -> tuple[str, str, str]:
    """
    Return (url, key, key_source).
    Prefer SUPABASE_SECRET_KEY; fall back to SUPABASE_SERVICE_ROLE_KEY only.
    Never accepts publishable/anonymous keys.
    """
    url = (os.getenv("SUPABASE_URL") or "").strip()
    secret = (os.getenv("SUPABASE_SECRET_KEY") or "").strip()
    legacy = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    publishable = (os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_ANON_KEY") or "").strip()

    if not url:
        raise SupabaseConfigError("SUPABASE_URL is required")

    if secret:
        key, source = secret, "SUPABASE_SECRET_KEY"
    elif legacy:
        key, source = legacy, "SUPABASE_SERVICE_ROLE_KEY"
    else:
        raise SupabaseConfigError(
            "SUPABASE_SECRET_KEY is required "
            "(or legacy SUPABASE_SERVICE_ROLE_KEY for compatibility)"
        )

    lowered = key.lower()
    if publishable and key == publishable:
        raise SupabaseConfigError(
            "Publishable/anonymous Supabase keys cannot be used for scraper writes"
        )
    if "publishable" in lowered or lowered.startswith("sb_publishable_"):
        raise SupabaseConfigError(
            "Publishable/anonymous Supabase keys cannot be used for scraper writes"
        )
    if "anon" in source.lower() or lowered.startswith("eyj") and "role\":\"anon" in key:
        # JWT anon keys are rejected; service_role JWTs are allowed via legacy fallback.
        try:
            import base64
            import json as _json

            payload_b64 = key.split(".")[1]
            pad = "=" * (-len(payload_b64) % 4)
            payload = _json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
            if payload.get("role") == "anon":
                raise SupabaseConfigError(
                    "Anonymous Supabase keys cannot be used for scraper writes"
                )
        except SupabaseConfigError:
            raise
        except Exception:
            pass

    return url, key, source


def reset_supabase_client() -> None:
    """Clear the cached client (tests only)."""
    global _supabase_client
    _supabase_client = None


def get_supabase_client():
    """
    Return a reusable Supabase client.
    Raises SupabaseConfigError / SupabaseNetworkError — never returns None.
    """
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    url, key, _source = get_supabase_credentials()
    try:
        from supabase import create_client
    except ImportError as exc:
        raise SupabaseConfigError(
            "supabase package is not installed or broken; "
            "run: python -m pip install -r requirements.txt"
        ) from exc

    try:
        _supabase_client = create_client(url, key)
    except Exception as exc:
        msg = redact_db_error(exc).lower()
        if any(tok in msg for tok in ("config", "key", "url", "credential", "invalid api")):
            raise SupabaseConfigError(redact_db_error(exc)) from exc
        raise SupabaseNetworkError(redact_db_error(exc)) from exc

    if _supabase_client is None:
        raise SupabaseConfigError("Supabase client initialization returned None")
    return _supabase_client


def _execute(operation: str, table: str, builder, platform: str = "", project_id: str = ""):
    """Run a PostgREST builder and normalize errors."""
    try:
        response = builder.execute()
    except SupabaseConfigError:
        raise
    except Exception as exc:
        msg = redact_db_error(exc)
        lowered = msg.lower()
        context = (
            f"operation={operation} table={table} "
            f"platform={platform or '-'} project_id={project_id or '-'}: {msg}"
        )
        if any(
            tok in lowered
            for tok in (
                "timeout",
                "timed out",
                "connection",
                "network",
                "name or service not known",
                "temporarily unavailable",
                "failed to establish",
            )
        ):
            raise SupabaseNetworkError(context) from exc
        raise SupabaseAPIError(context) from exc

    if response is None:
        raise SupabaseAPIError(
            f"operation={operation} table={table}: empty response"
        )
    return response


def _rows(response) -> list:
    data = getattr(response, "data", None)
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    raise SupabaseAPIError(f"Unexpected response data type: {type(data).__name__}")


def _one(response, required: bool = True) -> Optional[dict]:
    rows = _rows(response)
    if not rows:
        if required:
            raise SupabaseAPIError("Expected at least one row, got none")
        return None
    row = rows[0]
    if not isinstance(row, dict):
        raise SupabaseAPIError("Expected row dict from Supabase")
    return row


# ---------------------------------------------------------------------------
# Scraper runs
# ---------------------------------------------------------------------------

def create_scraper_run(
    platform: str = PLATFORM_MOVEMEON,
    scraper_name: str = SCRAPER_NAME,
    scraper_version: str = SCRAPER_VERSION,
    metadata: Optional[dict] = None,
) -> dict:
    client = get_supabase_client()
    payload = {
        "platform": platform,
        "scraper_name": scraper_name,
        "scraper_version": scraper_version,
        "status": "RUNNING",
        "started_at": _iso(),
        "metadata": metadata or {},
    }
    response = _execute(
        "create_scraper_run",
        "scraper_runs",
        client.table("scraper_runs").insert(payload).select("*"),
        platform=platform,
    )
    return _one(response)


def update_scraper_run_counts(run_id: str, **counts) -> dict:
    if not run_id:
        raise ValueError("run_id is required")
    allowed = {
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
        "metadata",
        "status",
        "failure_code",
        "failure_reason",
        "completed_at",
    }
    payload = {k: v for k, v in counts.items() if k in allowed and v is not None}
    if not payload:
        return get_scraper_run(run_id)
    client = get_supabase_client()
    response = _execute(
        "update_scraper_run_counts",
        "scraper_runs",
        client.table("scraper_runs").update(payload).eq("id", run_id).select("*"),
    )
    return _one(response)


def get_scraper_run(run_id: str) -> dict:
    client = get_supabase_client()
    response = _execute(
        "get_scraper_run",
        "scraper_runs",
        client.table("scraper_runs").select("*").eq("id", run_id).limit(1),
    )
    return _one(response)


def complete_scraper_run(run_id: str, status: str = "COMPLETED", **counts) -> dict:
    if status not in ("COMPLETED", "PARTIAL", "CANCELLED"):
        raise ValueError(f"Invalid completion status: {status}")
    payload = dict(counts)
    payload["status"] = status
    payload["completed_at"] = _iso()
    return update_scraper_run_counts(run_id, **payload)


def fail_scraper_run(
    run_id: str,
    failure_code: str,
    failure_reason: str,
    status: str = "FAILED",
    **counts,
) -> dict:
    if status not in ("FAILED", "AUTH_FAILED", "CANCELLED"):
        raise ValueError(f"Invalid failure status: {status}")
    payload = dict(counts)
    payload["status"] = status
    payload["completed_at"] = _iso()
    payload["failure_code"] = failure_code
    payload["failure_reason"] = redact_db_error(failure_reason)[:2000]
    return update_scraper_run_counts(run_id, **payload)


def mark_stale_running_runs(
    platform: str = PLATFORM_MOVEMEON,
    older_than_hours: int = 6,
) -> int:
    """Best-effort: mark abandoned RUNNING rows as FAILED."""
    client = get_supabase_client()
    cutoff = _iso(_utc_now() - timedelta(hours=max(older_than_hours, 1)))
    response = _execute(
        "mark_stale_running_runs",
        "scraper_runs",
        client.table("scraper_runs")
        .update(
            {
                "status": "FAILED",
                "completed_at": _iso(),
                "failure_code": "STALE_RUNNING",
                "failure_reason": "Run left in RUNNING past threshold",
            }
        )
        .eq("platform", platform)
        .eq("status", "RUNNING")
        .lt("started_at", cutoff)
        .select("id"),
        platform=platform,
    )
    return len(_rows(response))


# ---------------------------------------------------------------------------
# Three-day eligibility
# ---------------------------------------------------------------------------

def get_latest_project_occurrence(platform: str, project_id: str) -> Optional[dict]:
    """
    Latest occurrence for platform + project_id ordered by scraped_at desc.
    Raises on database failure — never treats failure as 'no row'.
    """
    if not platform or not project_id:
        raise ValueError("platform and project_id are required")
    client = get_supabase_client()
    # Include detail fields so existing-row enrichment can skip already-complete rows
    response = _execute(
        "get_latest_project_occurrence",
        "projects",
        client.table("projects")
        .select(
            "id,scraped_at,email_status,email_sent,email_eligible,email_not_sent_reason,"
            "title,source_url,detail_extraction_status,description,location_preference,"
            "project_length,start_date_text,level_of_support,industry,contracting_process,"
            "short_description,budget_text,platform_category,"
            "detail_attempt_count,detail_last_attempt_at,detail_completed_at,"
            "detail_failure_code,extraction_metadata,extraction_warnings,missing_fields"
        )
        .eq("platform", platform)
        .eq("project_id", project_id)
        .order("scraped_at", desc=True)
        .limit(1),
        platform=platform,
        project_id=project_id,
    )
    return _one(response, required=False)


def should_process_project(
    platform: str,
    project_id: str,
    now: Optional[datetime] = None,
) -> tuple[bool, str, Optional[dict]]:
    """
    Three-day repeated-project rule based on projects.scraped_at (UTC).

    Returns (eligible, reason, latest_row).
    age > 3 days → eligible; age <= 3 days (including exactly 3 days) → skip.
    """
    latest = get_latest_project_occurrence(platform, project_id)
    if latest is None:
        return True, "first_occurrence", None

    scraped_at = _parse_timestamptz(latest.get("scraped_at"))
    if scraped_at is None:
        raise SupabaseAPIError(
            f"Latest project row missing scraped_at "
            f"(platform={platform} project_id={project_id})"
        )

    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)

    age = current - scraped_at
    if age > THREE_DAY_WINDOW:
        return True, f"eligible_after_{age.total_seconds():.0f}s", latest
    return False, f"skipped_within_3_days_age_{age.total_seconds():.0f}s", latest


def platform_has_projects(platform: str) -> bool:
    """Platform-specific cold-start detection. Raises on DB failure."""
    client = get_supabase_client()
    response = _execute(
        "platform_has_projects",
        "projects",
        client.table("projects")
        .select("id")
        .eq("platform", platform)
        .limit(1),
        platform=platform,
    )
    return bool(_rows(response))


# ---------------------------------------------------------------------------
# Projects insert / email lifecycle
# ---------------------------------------------------------------------------

_PLACEHOLDER_STRINGS = {
    "",
    "unknown",
    "unclassified",
    "n/a",
    "na",
    "none",
    "not provided",
    "not specified",
    "tbd",
}


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _PLACEHOLDER_STRINGS or not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def merge_project_data(card_data: dict, detail_data: Optional[dict] = None) -> dict:
    """
    Merge card + detail extraction without letting empty/placeholder detail
    values overwrite useful card values.
    """
    merged = dict(card_data or {})
    warnings = list(merged.get("extraction_warnings") or [])
    detail_data = detail_data or {}

    for key, detail_val in detail_data.items():
        if key in ("extraction_warnings", "missing_fields", "extraction_metadata"):
            continue
        card_val = merged.get(key)
        if _is_empty_value(detail_val):
            if not _is_empty_value(card_val):
                warnings.append(f"detail_empty_preserved_card:{key}")
            continue
        if isinstance(detail_val, str) and detail_val.strip().lower() == "unclassified":
            if not _is_empty_value(card_val) and str(card_val).strip().lower() != "unclassified":
                warnings.append("detail_rejected_unclassified_category")
                continue
            # Do not invent Unclassified when card also empty
            if _is_empty_value(card_val):
                warnings.append("detail_rejected_unclassified_empty")
                continue
        if isinstance(detail_val, list) and not detail_val and card_val:
            continue
        merged[key] = detail_val

    # Merge metadata dicts
    meta = dict(merged.get("extraction_metadata") or {})
    meta.update(detail_data.get("extraction_metadata") or {})
    if meta:
        merged["extraction_metadata"] = meta

    detail_warnings = detail_data.get("extraction_warnings") or []
    warnings.extend(detail_warnings)
    if warnings:
        merged["extraction_warnings"] = warnings

    platform = (merged.get("platform") or detail_data.get("platform") or DEFAULT_PLATFORM).strip().lower()

    # Identity + genuinely visible misses only. Never require BTG category.
    identity_fields = ("title", "project_id", "source_url")
    missing = []
    for field in identity_fields:
        if _is_empty_value(merged.get(field)) and _is_empty_value(merged.get("id" if field == "project_id" else field)):
            if field == "project_id" and not _is_empty_value(merged.get("id")):
                continue
            if field == "source_url" and not _is_empty_value(merged.get("url")):
                continue
            missing.append(field)

    meta = merged.get("extraction_metadata") or {}
    visible_miss = list(meta.get("fields_missing_but_visible") or [])
    if not platform_exposes_field_local(platform, "platform_category"):
        visible_miss = [
            f for f in visible_miss
            if f not in ("platform_category", "platform_category_path", "platform_category_raw", "category")
        ]
    missing.extend(f for f in visible_miss if f not in missing)

    # Preserve detail-provided missing_fields when present (already visibility-based)
    detail_missing = detail_data.get("missing_fields")
    if isinstance(detail_missing, list):
        for item in detail_missing:
            text = str(item)
            if text in ("platform_category", "platform_category_path", "platform_category_raw", "category"):
                if not platform_exposes_field_local(platform, "platform_category"):
                    continue
            if text not in missing:
                missing.append(text)

    merged["missing_fields"] = missing

    if not platform_exposes_field_local(platform, "platform_category"):
        if _is_empty_value(merged.get("platform_category")) and (
            merged.get("platform_category_extraction_status") in (None, "", "MISSING")
        ):
            merged["platform_category"] = None
            merged["platform_category_path"] = []
            merged["platform_category_raw"] = None
            merged["platform_category_source"] = None
            merged["platform_category_confidence"] = None
            merged["platform_category_extraction_status"] = "NOT_EXPOSED"
            meta = dict(merged.get("extraction_metadata") or {})
            not_exposed = list(meta.get("fields_not_exposed") or [])
            for field in (
                "platform_category",
                "platform_category_path",
                "platform_category_raw",
            ):
                if field not in not_exposed:
                    not_exposed.append(field)
            meta["fields_not_exposed"] = not_exposed
            meta["fields_missing_but_visible"] = [
                f for f in (meta.get("fields_missing_but_visible") or [])
                if f not in (
                    "platform_category",
                    "platform_category_path",
                    "platform_category_raw",
                    "category",
                )
            ]
            meta.setdefault("platform_capabilities", {})["category_exposed"] = False
            merged["extraction_metadata"] = meta
            # Strip category-only warnings
            merged["extraction_warnings"] = [
                w for w in (merged.get("extraction_warnings") or [])
                if "CATEGORY" not in str(w).upper()
                and "platform_category" not in str(w)
            ]
            # Drop category from missing
            merged["missing_fields"] = [
                f for f in merged["missing_fields"]
                if f not in (
                    "platform_category",
                    "platform_category_path",
                    "platform_category_raw",
                    "category",
                )
                and "platform_category" not in str(f)
            ]

    return merged


def platform_exposes_field_local(platform: str, field_name: str) -> bool:
    """Lightweight capability check without importing extraction cycles."""
    try:
        import extraction as _ext
        return _ext.platform_exposes_field(platform, field_name)
    except Exception:
        plat = (platform or "").lower()
        if plat in ("btg", "movemeon") and field_name in (
            "platform_category",
            "platform_category_path",
            "platform_category_raw",
            "category",
        ):
            return False
        if plat == "catalant" and field_name.startswith("platform_category"):
            return True
        return False


def project_to_row(
    project: dict,
    *,
    scraper_run_id: Optional[str] = None,
    email_status: str = "PENDING",
    email_eligible: bool = True,
    email_sent: bool = False,
    email_not_sent_reason: Optional[str] = None,
    scraped_at: Optional[datetime] = None,
) -> dict:
    """Normalize internal project dict into a projects table insert payload."""
    now = scraped_at or _utc_now()
    project = dict(project or {})
    # Legacy field aliases → shared schema
    if not project.get("remote_or_onsite") and project.get("remote_type"):
        project["remote_or_onsite"] = project.get("remote_type")
    if not project.get("location_preference") and project.get("location_pref"):
        project["location_preference"] = project.get("location_pref")
    if not project.get("application_deadline") and project.get("deadline"):
        project["application_deadline"] = project.get("deadline")

    platform = (project.get("platform") or DEFAULT_PLATFORM).strip().lower()
    project_id = str(project.get("project_id") or project.get("id") or "").strip()
    title = (project.get("title") or "").strip()
    source_url = (
        project.get("source_url")
        or project.get("url")
        or ""
    ).strip()
    if not project_id or not title or not source_url:
        raise ValueError("project_id, title, and source_url are required")

    cat = project.get("platform_category")
    if isinstance(cat, str) and cat.strip().lower() == "unclassified":
        cat = None

    path = project.get("platform_category_path") or []
    if not isinstance(path, list):
        path = []

    cat_status = project.get("platform_category_extraction_status")
    category_not_exposed = platform in (PLATFORM_BTG, PLATFORM_MOVEMEON)
    if category_not_exposed and not cat and cat_status in (None, "", "MISSING"):
        cat_status = "NOT_EXPOSED"
        path = []
        project["platform_category_raw"] = None
        project["platform_category_source"] = None
        project["platform_category_confidence"] = None

    skills = project.get("skills") or []
    expertise = project.get("expertise") or []
    deliverables = project.get("deliverables") or []

    # Preserve platform-specific values under raw_data
    raw_data = dict(project.get("raw_data") or {})
    if project.get("requirements") and "btg_requirements" not in raw_data:
        raw_data["btg_requirements"] = project.get("requirements")
    if project.get("timeline") and "btg_timeline" not in raw_data:
        raw_data["btg_timeline"] = project.get("timeline")
    if project.get("company") and "movemeon_company" not in raw_data:
        raw_data["movemeon_company"] = project.get("company")
    project["raw_data"] = raw_data

    budget_text = (
        project.get("budget_text")
        or project.get("budget")
        or project.get("detail_budget")
        or None
    )
    if budget_text is not None:
        budget_text = str(budget_text).strip() or None

    missing_fields = list(project.get("missing_fields") or [])
    warnings = list(project.get("extraction_warnings") or [])
    if category_not_exposed:
        drop = {
            "platform_category",
            "platform_category_path",
            "platform_category_raw",
            "category",
        }
        missing_fields = [
            f for f in missing_fields
            if str(f) not in drop and "platform_category" not in str(f)
        ]
        warnings = [
            w for w in warnings
            if "CATEGORY" not in str(w).upper() and "platform_category" not in str(w)
        ]

    row = {
        "platform": platform,
        "project_id": project_id,
        "source_url": source_url,
        "title": title,
        "short_description": project.get("short_description") or None,
        "description": project.get("description"),
        "status": project.get("status"),
        "platform_category": cat or None,
        "platform_category_path": path,
        "platform_category_raw": project.get("platform_category_raw"),
        "platform_category_source": project.get("platform_category_source"),
        "platform_category_confidence": project.get("platform_category_confidence"),
        "platform_category_extraction_status": cat_status
        or project.get("platform_category_extraction_status")
        or ("NOT_EXPOSED" if category_not_exposed and not cat else ("MISSING" if not cat else None)),
        "location": project.get("location"),
        "location_preference": project.get("location_preference")
        or project.get("location_pref"),
        "budget_text": budget_text,
        "budget_min": project.get("budget_min"),
        "budget_max": project.get("budget_max"),
        "budget_currency": project.get("budget_currency"),
        "billing_type": project.get("billing_type"),
        "hourly_rate": project.get("hourly_rate"),
        "daily_rate": project.get("daily_rate"),
        "rate_currency": project.get("rate_currency"),
        "budget_source": project.get("budget_source"),
        "budget_confidence": project.get("budget_confidence"),
        "duration_text": project.get("duration_text") or project.get("duration"),
        "project_length": project.get("project_length"),
        "start_date_text": project.get("start_date_text") or project.get("start_date"),
        "source_start_date": project.get("source_start_date"),
        "level_of_support": project.get("level_of_support"),
        "industry": project.get("industry"),
        "contracting_process": project.get("contracting_process")
        or project.get("contracting"),
        "skills": skills if isinstance(skills, list) else [],
        "expertise": expertise if isinstance(expertise, list) else [],
        "deliverables": deliverables if isinstance(deliverables, list) else [],
        "engagement_type": project.get("engagement_type"),
        "project_type": project.get("project_type"),
        "workstream": project.get("workstream"),
        "estimated_hours": project.get("estimated_hours"),
        "weekly_commitment": project.get("weekly_commitment"),
        "remote_or_onsite": project.get("remote_or_onsite"),
        "country_or_region": project.get("country_or_region"),
        "application_deadline": project.get("application_deadline"),
        "time_posted_text": project.get("time_posted_text") or project.get("time_posted"),
        "source_posted_at": project.get("source_posted_at"),
        "source_posted_at_is_estimated": bool(
            project.get("source_posted_at_is_estimated", False)
        ),
        "scraped_at": _iso(now),
        "first_detected_at": _iso(now),
        "last_seen_at": _iso(now),
        "card_extraction_status": project.get("card_extraction_status") or "COMPLETE",
        "detail_extraction_status": project.get("detail_extraction_status")
        or "NOT_ATTEMPTED",
        "detail_last_attempt_at": project.get("detail_last_attempt_at"),
        "detail_attempt_count": int(project.get("detail_attempt_count") or 0),
        "detail_failure_code": project.get("detail_failure_code"),
        "detail_last_error": project.get("detail_last_error"),
        "detail_completed_at": project.get("detail_completed_at"),
        "missing_fields": missing_fields,
        "extraction_warnings": warnings,
        "extraction_metadata": project.get("extraction_metadata") or {},
        "raw_data": project.get("raw_data") or {},
        "email_eligible": bool(email_eligible),
        "email_status": email_status,
        "email_sent": bool(email_sent),
        "email_not_sent_reason": email_not_sent_reason,
        "scraper_run_id": scraper_run_id,
    }

    # Prefer short_description from card when description is the long detail text
    if not project.get("short_description") and project.get("description"):
        # Keep description as-is; short_description may equal card blurb stored separately
        pass

    # Drop None values that would clear NOT NULL DEFAULT columns incorrectly — keep required ones
    cleaned = {}
    for k, v in row.items():
        if v is None and k not in (
            "short_description",
            "description",
            "status",
            "platform_category",
            "platform_category_raw",
            "platform_category_source",
            "platform_category_confidence",
            "platform_category_extraction_status",
            "location",
            "location_preference",
            "budget_text",
            "budget_min",
            "budget_max",
            "budget_currency",
            "duration_text",
            "project_length",
            "start_date_text",
            "source_start_date",
            "level_of_support",
            "industry",
            "contracting_process",
            "engagement_type",
            "project_type",
            "workstream",
            "estimated_hours",
            "weekly_commitment",
            "remote_or_onsite",
            "country_or_region",
            "application_deadline",
            "time_posted_text",
            "source_posted_at",
            "email_not_sent_reason",
            "scraper_run_id",
            "email_failure_code",
            "email_last_error",
            "email_last_attempt_at",
            "email_next_retry_at",
            "email_sent_at",
            "email_message_id",
            "budget_currency",
            "billing_type",
            "hourly_rate",
            "daily_rate",
            "rate_currency",
            "budget_source",
            "budget_confidence",
            "duration_text",
            "project_length",
            "start_date_text",
            "source_start_date",
            "level_of_support",
            "industry",
            "contracting_process",
            "engagement_type",
            "project_type",
            "workstream",
            "estimated_hours",
            "weekly_commitment",
            "remote_or_onsite",
            "country_or_region",
            "application_deadline",
            "time_posted_text",
            "source_posted_at",
            "email_not_sent_reason",
            "scraper_run_id",
            "email_failure_code",
            "email_last_error",
            "email_last_attempt_at",
            "email_next_retry_at",
            "email_sent_at",
            "email_message_id",
            "detail_last_attempt_at",
            "detail_failure_code",
            "detail_last_error",
            "detail_completed_at",
        ):
            continue
        cleaned[k] = v
    return _strip_unavailable_enrichment_columns(cleaned)


DETAIL_ENRICHMENT_COLUMNS = {
    "billing_type",
    "hourly_rate",
    "daily_rate",
    "rate_currency",
    "budget_source",
    "budget_confidence",
    "detail_last_attempt_at",
    "detail_attempt_count",
    "detail_failure_code",
    "detail_last_error",
    "detail_completed_at",
}

_detail_enrichment_schema_ready: Optional[bool] = None
_detail_enrichment_warn_printed = False


def reset_detail_enrichment_schema_cache() -> None:
    global _detail_enrichment_schema_ready, _detail_enrichment_warn_printed
    _detail_enrichment_schema_ready = None
    _detail_enrichment_warn_printed = False


def detail_enrichment_schema_ready() -> bool:
    """True when migration 20260801120000 columns exist in projects."""
    global _detail_enrichment_schema_ready
    if _detail_enrichment_schema_ready is not None:
        return _detail_enrichment_schema_ready
    try:
        client = get_supabase_client()
        _execute(
            "probe_detail_enrichment_columns",
            "projects",
            client.table("projects")
            .select("billing_type,detail_attempt_count,budget_source")
            .limit(1),
        )
        _detail_enrichment_schema_ready = True
    except Exception as exc:
        text = str(exc or "").lower()
        if "pgrst204" in text or "could not find the" in text or "schema cache" in text:
            _detail_enrichment_schema_ready = False
        else:
            # Unexpected error — do not cache forever; treat as not ready for safety
            _detail_enrichment_schema_ready = False
    return bool(_detail_enrichment_schema_ready)


def detail_enrichment_migration_message() -> str:
    return (
        "Detail enrichment columns are missing. Apply the new migration:\n"
        "  1) Open https://supabase.com/dashboard/project/sdaqjqvcxvtxxcblmlev/sql/new\n"
        "  2) Paste contents of "
        "supabase/migrations/20260801120000_add_detail_enrichment_columns.sql\n"
        "  3) Click Run\n"
        "  4) Verify with: python monitor.py --test-supabase"
    )


def warn_detail_enrichment_migration_once() -> None:
    """Print the missing-migration notice at most once per process."""
    global _detail_enrichment_warn_printed
    if detail_enrichment_schema_ready() or _detail_enrichment_warn_printed:
        return
    _detail_enrichment_warn_printed = True
    print(f"⚠️  {detail_enrichment_migration_message()}")


def _strip_unavailable_enrichment_columns(payload: dict) -> dict:
    if detail_enrichment_schema_ready():
        return payload
    return {k: v for k, v in payload.items() if k not in DETAIL_ENRICHMENT_COLUMNS}


DETAIL_UPDATE_ALLOWED = {
    "title",
    "short_description",
    "description",
    "status",
    "platform_category",
    "platform_category_path",
    "platform_category_raw",
    "platform_category_source",
    "platform_category_confidence",
    "platform_category_extraction_status",
    "location",
    "location_preference",
    "budget_text",
    "budget_min",
    "budget_max",
    "budget_currency",
    "billing_type",
    "hourly_rate",
    "daily_rate",
    "rate_currency",
    "budget_source",
    "budget_confidence",
    "duration_text",
    "project_length",
    "start_date_text",
    "source_start_date",
    "level_of_support",
    "industry",
    "contracting_process",
    "skills",
    "expertise",
    "deliverables",
    "engagement_type",
    "project_type",
    "workstream",
    "estimated_hours",
    "weekly_commitment",
    "remote_or_onsite",
    "country_or_region",
    "application_deadline",
    "time_posted_text",
    "source_posted_at",
    "source_posted_at_is_estimated",
    "card_extraction_status",
    "detail_extraction_status",
    "detail_last_attempt_at",
    "detail_attempt_count",
    "detail_failure_code",
    "detail_last_error",
    "detail_completed_at",
    "missing_fields",
    "extraction_warnings",
    "extraction_metadata",
    "raw_data",
    "last_seen_at",
}

DETAIL_UPDATE_FORBIDDEN = {
    "id",
    "platform",
    "project_id",
    "email_status",
    "email_sent",
    "email_eligible",
    "email_not_sent_reason",
    "email_attempt_count",
    "email_sent_at",
    "email_failure_code",
    "email_last_error",
    "email_last_attempt_at",
    "email_next_retry_at",
    "email_message_id",
    "scraped_at",
    "first_detected_at",
    "scraper_run_id",
    "created_at",
}


def update_project_details(project_row_id: str, detail_updates: dict) -> dict:
    """Update enrichment fields for an existing projects.id. Raises on failure."""
    if not project_row_id:
        raise ValueError("project_row_id is required")
    if not isinstance(detail_updates, dict):
        raise ValueError("detail_updates must be a dict")

    forbidden_hit = [k for k in detail_updates if k in DETAIL_UPDATE_FORBIDDEN]
    if forbidden_hit:
        raise ValueError(f"Refusing to update protected columns: {', '.join(forbidden_hit)}")

    payload = {}
    for key, value in detail_updates.items():
        if key not in DETAIL_UPDATE_ALLOWED:
            continue
        if value is None:
            # Allow explicit clears for nullable enrichment fields
            payload[key] = None
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and len(value) == 0 and key in (
            "skills", "expertise", "deliverables", "platform_category_path",
            "missing_fields", "extraction_warnings",
        ):
            payload[key] = value
            continue
        payload[key] = value

    if not payload:
        raise ValueError("No valid detail updates provided")

    if "detail_last_error" in payload and payload["detail_last_error"] is not None:
        payload["detail_last_error"] = redact_db_error(payload["detail_last_error"])[:2000]

    payload["last_seen_at"] = payload.get("last_seen_at") or _iso()
    payload = _strip_unavailable_enrichment_columns(payload)
    warn_detail_enrichment_migration_once()

    client = get_supabase_client()
    response = _execute(
        "update_project_details",
        "projects",
        client.table("projects").update(payload).eq("id", project_row_id).select("*"),
    )
    return _one(response)


def get_projects_needing_detail_enrichment(
    *,
    platform: str = PLATFORM_MOVEMEON,
    limit: int = 20,
    project_id: Optional[str] = None,
    retry_failed: bool = False,
) -> list:
    """
    Fetch platform rows that need detail enrichment.
    Status-driven: COMPLETE rows are not selected merely for optional nulls.
    Explicit --retry-failed includes FAILED/TIMEOUT regardless of attempt limits.
    """
    client = get_supabase_client()
    query = (
        client.table("projects")
        .select("*")
        .eq("platform", platform)
        .order("scraped_at", desc=True)
        .limit(max(limit * 5, 50))
    )
    if project_id:
        query = query.eq("project_id", project_id)
    response = _execute(
        "get_projects_needing_detail_enrichment",
        "projects",
        query,
        platform=platform,
        project_id=project_id or "",
    )
    rows = _rows(response)
    selected = []
    for row in rows:
        status = (row.get("detail_extraction_status") or "").upper()
        meta = row.get("extraction_metadata") or {}
        missing_visible = meta.get("fields_missing_but_visible") or []

        if status == "COMPLETE":
            # Never select COMPLETE solely because optional shared columns are null.
            needs = bool(missing_visible)
        elif status in ("NOT_ATTEMPTED", "PARTIAL"):
            needs = True
        elif status in ("FAILED", "TIMEOUT"):
            # Backfill includes these; automatic scans apply attempt/cooldown separately.
            needs = True
        elif not status:
            needs = not bool(str(row.get("description") or "").strip())
        else:
            needs = False

        if retry_failed and status in ("FAILED", "TIMEOUT"):
            needs = True

        if needs:
            selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def needs_detail_enrichment(
    project: dict,
    *,
    auto_enabled: bool = True,
    max_attempts: int = 3,
    cooldown_minutes: int = 360,
    now: Optional[datetime] = None,
    respect_limits: bool = True,
) -> bool:
    """
    Decide whether an existing occurrence should be detail-enriched.

    COMPLETE rows are never enriched merely because optional columns are null.
    Automatic scans also respect attempt-count and cooldown limits.
    """
    if not auto_enabled and respect_limits:
        return False

    status = str(project.get("detail_extraction_status") or "").upper()
    meta = project.get("extraction_metadata") or {}
    missing_visible = meta.get("fields_missing_but_visible") or []

    if status == "COMPLETE":
        return False

    if status in {"NOT_ATTEMPTED", "PARTIAL", "FAILED", "TIMEOUT"}:
        base_needs = True
    elif not status:
        # Legacy compatibility: no status → enrich only when description absent.
        base_needs = not bool(str(project.get("description") or "").strip())
    else:
        base_needs = False

    # Filter category-only legacy PARTIAL rows: treat as not needing enrichment
    # when the only issue was category (repair command also normalizes these).
    if status == "PARTIAL" and base_needs:
        plat = str(project.get("platform") or DEFAULT_PLATFORM).lower()
        if not platform_exposes_field_local(plat, "platform_category"):
            meta = project.get("extraction_metadata") or {}
            missing_visible = [
                f for f in (meta.get("fields_missing_but_visible") or [])
                if f not in (
                    "platform_category",
                    "platform_category_path",
                    "platform_category_raw",
                    "category",
                )
            ]
            missing_fields = [
                f for f in (project.get("missing_fields") or [])
                if str(f) not in (
                    "platform_category",
                    "platform_category_path",
                    "platform_category_raw",
                    "category",
                )
                and "platform_category" not in str(f)
            ]
            if (
                not missing_visible
                and not missing_fields
                and str(project.get("description") or "").strip()
                and (project.get("detail_failure_code") in (None, "", "CATEGORY_MISSING"))
            ):
                base_needs = False

    if not base_needs:
        return False

    if not respect_limits:
        return True

    try:
        attempts = int(project.get("detail_attempt_count") or 0)
    except (TypeError, ValueError):
        attempts = 0
    if attempts >= max(1, int(max_attempts)):
        return False

    last_at = _parse_timestamptz(project.get("detail_last_attempt_at"))
    if last_at is not None:
        current = now or _utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        age = current - last_at
        if age < timedelta(minutes=max(0, int(cooldown_minutes))):
            return False

    return True


def insert_project_occurrence(
    project: dict,
    scraper_run_id: Optional[str] = None,
    *,
    email_status: str = "PENDING",
    email_eligible: bool = True,
    email_sent: bool = False,
    email_not_sent_reason: Optional[str] = None,
    scraped_at: Optional[datetime] = None,
) -> dict:
    """Insert a new occurrence row. Always creates a new row (no upsert)."""
    payload = project_to_row(
        project,
        scraper_run_id=scraper_run_id,
        email_status=email_status,
        email_eligible=email_eligible,
        email_sent=email_sent,
        email_not_sent_reason=email_not_sent_reason,
        scraped_at=scraped_at,
    )
    client = get_supabase_client()
    response = _execute(
        "insert_project_occurrence",
        "projects",
        client.table("projects").insert(payload).select("*"),
        platform=payload.get("platform", ""),
        project_id=payload.get("project_id", ""),
    )
    return _one(response)


def get_project_by_id(row_id: str) -> Optional[dict]:
    client = get_supabase_client()
    response = _execute(
        "get_project_by_id",
        "projects",
        client.table("projects").select("*").eq("id", row_id).limit(1),
    )
    return _one(response, required=False)


def update_project_email_status(row_id: str, **fields) -> dict:
    if not row_id:
        raise ValueError("row_id is required")
    allowed = {
        "email_eligible",
        "email_status",
        "email_sent",
        "email_not_sent_reason",
        "email_failure_code",
        "email_last_error",
        "email_attempt_count",
        "email_last_attempt_at",
        "email_next_retry_at",
        "email_sent_at",
        "email_message_id",
        "last_seen_at",
    }
    payload = {k: v for k, v in fields.items() if k in allowed}
    if "email_last_error" in payload and payload["email_last_error"] is not None:
        payload["email_last_error"] = redact_db_error(payload["email_last_error"])[:2000]
    client = get_supabase_client()
    response = _execute(
        "update_project_email_status",
        "projects",
        client.table("projects").update(payload).eq("id", row_id).select("*"),
    )
    return _one(response)


def get_retryable_email_projects(
    *,
    max_attempts: int,
    now: Optional[datetime] = None,
    limit: int = 20,
    platform: Optional[str] = None,
) -> list:
    client = get_supabase_client()
    current = _iso(now or _utc_now())
    query = (
        client.table("projects")
        .select("*")
        .eq("email_status", "RETRY_PENDING")
        .lte("email_next_retry_at", current)
        .lt("email_attempt_count", max_attempts)
        .eq("email_eligible", True)
        .order("email_next_retry_at", desc=False)
        .limit(limit)
    )
    if platform:
        query = query.eq("platform", platform)
    response = _execute("get_retryable_email_projects", "projects", query)
    return _rows(response)


def record_email_attempt(
    project_row_id: str,
    attempt_number: int,
    status: str = "SENDING",
    *,
    recipients: Optional[list] = None,
    provider: str = "smtp",
    message_id: Optional[str] = None,
    failure_code: Optional[str] = None,
    failure_reason: Optional[str] = None,
    metadata: Optional[dict] = None,
    attempt_id: Optional[str] = None,
) -> dict:
    client = get_supabase_client()
    if attempt_id:
        payload = {
            "status": status,
            "completed_at": _iso() if status in ("SENT", "FAILED") else None,
            "message_id": message_id,
            "failure_code": failure_code,
            "failure_reason": redact_db_error(failure_reason) if failure_reason else None,
        }
        if metadata is not None:
            payload["metadata"] = metadata
        response = _execute(
            "update_email_attempt",
            "email_attempts",
            client.table("email_attempts")
            .update(payload)
            .eq("id", attempt_id)
            .select("*"),
        )
        return _one(response)

    payload = {
        "project_id": project_row_id,
        "attempt_number": attempt_number,
        "status": status,
        "attempted_at": _iso(),
        "recipients": recipients or [],
        "provider": provider,
        "message_id": message_id,
        "failure_code": failure_code,
        "failure_reason": redact_db_error(failure_reason) if failure_reason else None,
        "metadata": metadata or {},
    }
    if status in ("SENT", "FAILED"):
        payload["completed_at"] = _iso()
    response = _execute(
        "record_email_attempt",
        "email_attempts",
        client.table("email_attempts").insert(payload).select("*"),
    )
    return _one(response)


def compute_email_next_retry_at(
    attempt_count: int,
    base_minutes: int = 15,
    now: Optional[datetime] = None,
) -> datetime:
    """Bounded exponential backoff: base * 2^(attempt-1), capped at 24h."""
    current = now or _utc_now()
    exponent = max(attempt_count - 1, 0)
    minutes = min(base_minutes * (2 ** exponent), 24 * 60)
    return current + timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _earliest_cookie_expiry(cookies: list) -> Optional[str]:
    expiries = []
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        exp = cookie.get("expiry") or cookie.get("expires")
        if exp is None:
            continue
        try:
            ts = float(exp)
            if ts > 1e12:
                ts = ts / 1000.0
            expiries.append(datetime.fromtimestamp(ts, tz=timezone.utc))
        except (TypeError, ValueError, OSError):
            continue
    if not expiries:
        return None
    return _iso(min(expiries))


def save_scraper_session(
    platform: str,
    cookies: list,
    *,
    metadata: Optional[dict] = None,
    local_storage: Optional[dict] = None,
) -> dict:
    """
    Persist cookies for a platform without overwriting worker-lock columns.
    Uses update-when-exists / insert-when-missing so lock state is preserved.
    Always sends a non-null UTC saved_at.
    Optional local_storage is stored under session_data.local_storage.
    """
    if not platform:
        raise ValueError("platform is required")
    normalized_platform = platform.strip().lower()
    if not isinstance(cookies, list):
        raise ValueError("cookies must be a list")

    # Sanitize: only keep JSON-safe cookie dicts; never store passwords
    safe_cookies = []
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        safe = {
            k: v
            for k, v in cookie.items()
            if k.lower() not in ("password", "passwd", "smtp_password")
        }
        safe_cookies.append(safe)

    safe_local_storage: dict = {}
    if isinstance(local_storage, dict):
        for key, value in local_storage.items():
            if not isinstance(key, str):
                continue
            lowered = key.lower()
            if any(tok in lowered for tok in ("password", "passwd", "secret", "smtp")):
                continue
            if value is None:
                continue
            safe_local_storage[key] = value if isinstance(value, str) else str(value)

    now = _iso()
    session_data: dict[str, Any] = {"cookies": safe_cookies}
    if safe_local_storage:
        session_data["local_storage"] = safe_local_storage

    session_payload = {
        "session_data": session_data,
        "saved_at": now,
        "expires_at": _earliest_cookie_expiry(safe_cookies),
        "session_version": 1,
        "metadata": metadata
        or {
            "cookie_count": len(safe_cookies),
            "source": "selenium",
            "session_state": "active" if safe_cookies else "cleared",
            "last_validated_at": now,
        },
    }
    # Never include worker-lock columns in cookie payloads.
    for forbidden in (
        "worker_lock_owner",
        "worker_lock_expires_at",
        "worker_lock_heartbeat_at",
    ):
        session_payload.pop(forbidden, None)
    _assert_session_write_contract(session_payload, operation="save_scraper_session")

    client = get_supabase_client()
    existing = load_scraper_session(normalized_platform)
    if existing:
        response = _execute(
            "save_scraper_session",
            "scraper_sessions",
            client.table("scraper_sessions")
            .update(session_payload)
            .eq("platform", normalized_platform)
            .select("*"),
            platform=normalized_platform,
        )
        return _one(response)

    insert_payload = {"platform": normalized_platform, **session_payload}
    response = _execute(
        "save_scraper_session",
        "scraper_sessions",
        client.table("scraper_sessions").insert(insert_payload).select("*"),
        platform=normalized_platform,
    )
    return _one(response)


def load_scraper_session(platform: str) -> Optional[dict]:
    if not platform:
        raise ValueError("platform is required")
    normalized_platform = platform.strip().lower()
    client = get_supabase_client()
    response = _execute(
        "load_scraper_session",
        "scraper_sessions",
        client.table("scraper_sessions")
        .select("*")
        .eq("platform", normalized_platform)
        .limit(1),
        platform=normalized_platform,
    )
    return _one(response, required=False)


def _assert_session_write_contract(payload: dict, *, operation: str) -> None:
    """Application contract: cookie save/clear must never send saved_at=NULL."""
    if payload.get("saved_at") is None:
        raise SupabaseAPIError(
            f"operation={operation} table=scraper_sessions: "
            "saved_at must be a non-null UTC timestamp"
        )
    for forbidden in (
        "worker_lock_owner",
        "worker_lock_expires_at",
        "worker_lock_heartbeat_at",
    ):
        if forbidden in payload:
            raise SupabaseAPIError(
                f"operation={operation} table=scraper_sessions: "
                f"refusing to overwrite {forbidden} via cookie session write"
            )


def delete_scraper_session(platform: str) -> bool:
    """
    Clear cookie session_data for a platform while preserving worker-lock fields.

    Idempotent. Never deletes the scraper_sessions row. Never sends saved_at=NULL
    (column is NOT NULL). Marks the cleared session as immediately expired.
    """
    if not platform:
        raise ValueError("platform is required")
    normalized_platform = platform.strip().lower()
    now = _iso()
    payload = {
        "session_data": {"cookies": []},
        "saved_at": now,
        "expires_at": now,
        "metadata": {
            "source": "selenium",
            "session_state": "cleared",
            "cleared_at": now,
            "cookie_count": 0,
        },
    }
    _assert_session_write_contract(payload, operation="delete_scraper_session")

    client = get_supabase_client()
    existing = load_scraper_session(normalized_platform)
    if not existing:
        # Missing row is idempotent success for cookie clear.
        return True

    # Already-cleared empty cookie list is still a successful no-op update.
    try:
        response = _execute(
            "delete_scraper_session",
            "scraper_sessions",
            client.table("scraper_sessions")
            .update(payload)
            .eq("platform", normalized_platform)
            .select("*"),
            platform=normalized_platform,
        )
        # allow_empty: update can return [] when row vanished mid-flight
        _rows(response)
        return True
    except SupabaseAPIError:
        raise
    except Exception as exc:
        raise SupabaseAPIError(
            f"operation=delete_scraper_session table=scraper_sessions "
            f"platform={normalized_platform}: {redact_db_error(exc)}"
        ) from exc


def test_session_cookie_clear(*, cleanup: bool = True) -> dict:
    """
    Self-test cookie clear on a temporary platform row.
    Never modifies the production platform='btg' session.
    """
    import secrets

    platform = f"btg_session_test_{secrets.token_hex(5)}"
    client = get_supabase_client()
    now = _iso()
    lock_expires = _iso(_utc_now() + timedelta(minutes=30))
    insert_payload = {
        "platform": platform,
        "session_data": {"cookies": [{"name": "test", "value": "1", "domain": ".example"}]},
        "saved_at": now,
        "expires_at": _iso(_utc_now() + timedelta(hours=1)),
        "session_version": 1,
        "metadata": {"temporary_test": True, "source": "test_session_cookie_clear"},
    }
    if worker_lock_schema_ready():
        insert_payload.update(
            {
                "worker_lock_owner": "session-clear-test",
                "worker_lock_expires_at": lock_expires,
                "worker_lock_heartbeat_at": now,
            }
        )

    try:
        _execute(
            "test_session_cookie_clear_insert",
            "scraper_sessions",
            client.table("scraper_sessions").insert(insert_payload).select("*"),
            platform=platform,
        )
        before = load_scraper_session(platform)
        if not before:
            raise SupabaseAPIError("test session row missing after insert")

        delete_scraper_session(platform)
        after = load_scraper_session(platform)
        if not after:
            raise SupabaseAPIError("test session row missing after cookie clear")
        if after.get("saved_at") is None:
            raise SupabaseAPIError("saved_at became null after cookie clear")
        cookies = (after.get("session_data") or {}).get("cookies")
        if cookies != []:
            raise SupabaseAPIError(f"cookies not cleared: {cookies!r}")
        meta = after.get("metadata") or {}
        if meta.get("session_state") != "cleared":
            raise SupabaseAPIError("metadata.session_state was not cleared")
        if worker_lock_schema_ready():
            if after.get("worker_lock_owner") != before.get("worker_lock_owner"):
                raise SupabaseAPIError("worker_lock_owner changed during cookie clear")
            if after.get("worker_lock_expires_at") != before.get("worker_lock_expires_at"):
                raise SupabaseAPIError("worker_lock_expires_at changed during cookie clear")

        return {
            "ok": True,
            "platform": platform,
            "saved_at": after.get("saved_at"),
            "expires_at": after.get("expires_at"),
            "cookie_count": 0,
            "worker_lock_preserved": bool(worker_lock_schema_ready()),
            "cleaned_up": False,
        }
    finally:
        if cleanup:
            try:
                _execute(
                    "test_session_cookie_clear_cleanup",
                    "scraper_sessions",
                    client.table("scraper_sessions").delete().eq("platform", platform),
                    platform=platform,
                )
            except Exception:
                pass


def is_missing_schema_error(error) -> bool:
    text = str(error or "").lower()
    return "pgrst205" in text or "could not find the table" in text or "schema cache" in text


def schema_missing_message(table: str = "projects") -> str:
    return (
        f"Supabase table '{table}' is missing. Apply the migration first:\n"
        f"  1) Open https://supabase.com/dashboard/project/sdaqjqvcxvtxxcblmlev/sql/new\n"
        f"  2) Paste contents of supabase/migrations/20260331120000_create_project_monitor_schema.sql\n"
        f"  3) Click Run\n"
        f"  4) Verify with: python monitor.py --test-supabase"
    )


def list_required_tables() -> list[str]:
    return ["projects", "scraper_runs", "email_attempts", "scraper_sessions"]


def ensure_schema_ready() -> None:
    """Raise a clear error if required tables / lock columns are missing."""
    client = get_supabase_client()
    for table in list_required_tables():
        try:
            _execute(
                "ensure_schema",
                table,
                client.table(table)
                .select("id" if table != "scraper_sessions" else "platform")
                .limit(1),
            )
        except Exception as exc:
            if is_missing_schema_error(exc):
                raise SupabaseConfigError(schema_missing_message(table)) from exc
            raise
    # Contract check: scraper_sessions.saved_at must be readable (NOT NULL is desired).
    try:
        _execute(
            "ensure_schema_saved_at",
            "scraper_sessions",
            client.table("scraper_sessions")
            .select("platform,saved_at,expires_at,session_data")
            .limit(1),
        )
    except Exception as exc:
        if is_missing_schema_error(exc):
            raise SupabaseConfigError(schema_missing_message("scraper_sessions")) from exc
        raise
    if not detail_enrichment_schema_ready():
        warn_detail_enrichment_migration_once()
    if not worker_lock_schema_ready():
        warn_worker_lock_migration_once()


def test_supabase_connection(cleanup: bool = True, platform: str = PLATFORM_MOVEMEON) -> dict:
    """
    Validate credentials and tables with a reversible temporary insert.
    Never prints keys. Never clears the production BTG session.
    Returns a summary dict.
    """
    client = get_supabase_client()
    availability = {}
    for table in list_required_tables():
        try:
            _execute(
                "test_table",
                table,
                client.table(table).select(
                    "id" if table != "scraper_sessions" else "platform"
                ).limit(1),
            )
            availability[table] = True
        except Exception as exc:
            availability[table] = False
            if is_missing_schema_error(exc):
                raise SupabaseConfigError(schema_missing_message(table)) from exc
            raise SupabaseAPIError(
                f"Table check failed for {table}: {redact_db_error(exc)}"
            ) from exc

    enrichment_ok = detail_enrichment_schema_ready()
    lock_ok = worker_lock_schema_ready()
    session_clear_ok = False
    session_clear_summary = {}
    try:
        session_clear_summary = test_session_cookie_clear(cleanup=True)
        session_clear_ok = bool(session_clear_summary.get("ok"))
    except Exception as exc:
        session_clear_summary = {"ok": False, "error": redact_db_error(exc)}

    run = create_scraper_run(
        platform=platform,
        scraper_name=f"{SCRAPER_NAME}-test",
        metadata={"temporary_test": True},
    )
    run_id = run["id"]
    project = None
    try:
        project = insert_project_occurrence(
            {
                "platform": platform,
                "project_id": f"__test_{run_id[:8]}__",
                "title": "Temporary Supabase connectivity test",
                "source_url": "https://example.invalid/test",
                "description": "auto-deleted",
                "raw_data": {"temporary_test": True},
            },
            scraper_run_id=run_id,
            email_status="NOT_REQUIRED",
            email_eligible=False,
            email_not_sent_reason="CONNECTIVITY_TEST",
        )
        complete_scraper_run(run_id, status="COMPLETED", projects_inserted=1)
    finally:
        if cleanup:
            if project and project.get("id"):
                _execute(
                    "cleanup_test_project",
                    "projects",
                    client.table("projects").delete().eq("id", project["id"]),
                )
            _execute(
                "cleanup_test_run",
                "scraper_runs",
                client.table("scraper_runs").delete().eq("id", run_id),
            )

    return {
        "ok": True,
        "tables": availability,
        "detail_enrichment_columns": enrichment_ok,
        "worker_lock_ready": lock_ok,
        "session_cookie_clear_ok": session_clear_ok,
        "session_cookie_clear": session_clear_summary,
        "test_run_id": run_id,
        "cleaned_up": cleanup,
        "platform": platform,
        "note": "Cookie-clear self-test used a temporary platform row; production btg session was not modified.",
    }


# ---------------------------------------------------------------------------
# Email attempt aliases (contract names)
# ---------------------------------------------------------------------------

def create_email_attempt(
    project_row_id: str,
    attempt_number: int,
    *,
    recipients: Optional[list] = None,
    provider: str = "smtp",
    metadata: Optional[dict] = None,
) -> dict:
    return record_email_attempt(
        project_row_id,
        attempt_number,
        status="SENDING",
        recipients=recipients,
        provider=provider,
        metadata=metadata,
    )


def complete_email_attempt_success(
    attempt_id: str,
    *,
    message_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    return record_email_attempt(
        "",
        0,
        status="SENT",
        attempt_id=attempt_id,
        message_id=message_id,
        metadata=metadata,
    )


def complete_email_attempt_failure(
    attempt_id: str,
    *,
    failure_code: Optional[str] = None,
    failure_reason: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    return record_email_attempt(
        "",
        0,
        status="FAILED",
        attempt_id=attempt_id,
        failure_code=failure_code,
        failure_reason=failure_reason,
        metadata=metadata,
    )


def get_project_by_uuid(row_id: str) -> Optional[dict]:
    return get_project_by_id(row_id)


# ---------------------------------------------------------------------------
# Worker locks (atomic via PostgreSQL RPCs)
# ---------------------------------------------------------------------------

_worker_lock_schema_ready: Optional[bool] = None
_worker_lock_warn_printed = False


def reset_worker_lock_schema_cache() -> None:
    global _worker_lock_schema_ready, _worker_lock_warn_printed
    _worker_lock_schema_ready = None
    _worker_lock_warn_printed = False


def worker_lock_schema_ready() -> bool:
    """True when worker-lock columns exist on scraper_sessions."""
    global _worker_lock_schema_ready
    if _worker_lock_schema_ready is not None:
        return _worker_lock_schema_ready
    try:
        client = get_supabase_client()
        _execute(
            "probe_worker_lock_columns",
            "scraper_sessions",
            client.table("scraper_sessions")
            .select("worker_lock_owner,worker_lock_expires_at,worker_lock_heartbeat_at")
            .limit(1),
        )
        _worker_lock_schema_ready = True
    except Exception as exc:
        text = str(exc or "").lower()
        if "pgrst204" in text or "could not find the" in text or "schema cache" in text:
            _worker_lock_schema_ready = False
        else:
            _worker_lock_schema_ready = False
    return bool(_worker_lock_schema_ready)


def worker_lock_migration_message() -> str:
    return (
        "Worker-lock columns/functions are missing. Apply the migration:\n"
        "  1) Open Supabase SQL Editor for the shared project\n"
        "  2) Paste contents of "
        "supabase/migrations/20260802010000_add_scraper_session_worker_lock.sql\n"
        "  3) Click Run\n"
        "  4) Verify with: python monitor.py --test-supabase"
    )


def warn_worker_lock_migration_once() -> None:
    global _worker_lock_warn_printed
    if worker_lock_schema_ready() or _worker_lock_warn_printed:
        return
    _worker_lock_warn_printed = True
    try:
        print(f"WARNING: {worker_lock_migration_message()}")
    except UnicodeEncodeError:
        print("WARNING: worker-lock migration missing — see supabase/migrations/")


class WorkerLockError(RuntimeError):
    """Worker lock could not be acquired or verified (fail closed)."""


def _rpc_lock_row(response) -> dict:
    rows = _rows(response)
    if not rows:
        raise SupabaseAPIError("Worker lock RPC returned empty result")
    return rows[0] if isinstance(rows[0], dict) else {"acquired": bool(rows[0])}


def acquire_worker_lock(
    platform: str,
    owner: str,
    ttl_seconds: int = 180,
) -> dict:
    """
    Atomically acquire the platform worker lock via PostgreSQL.
    Raises WorkerLockError / Supabase* on failure (fail closed — never pretend success).
    Returns dict with acquired, owner, expires_at, heartbeat_at.
    """
    if not platform or not owner:
        raise ValueError("platform and owner are required")
    if not worker_lock_schema_ready():
        warn_worker_lock_migration_once()
        raise WorkerLockError(worker_lock_migration_message())

    client = get_supabase_client()
    try:
        response = client.rpc(
            "acquire_scraper_worker_lock",
            {
                "p_platform": platform,
                "p_owner": owner,
                "p_ttl_seconds": int(ttl_seconds),
            },
        ).execute()
    except Exception as exc:
        raise WorkerLockError(
            f"acquire_worker_lock failed: {redact_db_error(exc)}"
        ) from exc

    row = _rpc_lock_row(response)
    acquired = bool(row.get("acquired"))
    return {
        "acquired": acquired,
        "owner": row.get("owner"),
        "expires_at": row.get("expires_at"),
        "heartbeat_at": row.get("heartbeat_at"),
    }


def renew_worker_lock(
    platform: str,
    owner: str,
    ttl_seconds: int = 180,
) -> dict:
    if not platform or not owner:
        raise ValueError("platform and owner are required")
    if not worker_lock_schema_ready():
        raise WorkerLockError(worker_lock_migration_message())

    client = get_supabase_client()
    try:
        response = client.rpc(
            "renew_scraper_worker_lock",
            {
                "p_platform": platform,
                "p_owner": owner,
                "p_ttl_seconds": int(ttl_seconds),
            },
        ).execute()
    except Exception as exc:
        raise WorkerLockError(
            f"renew_worker_lock failed: {redact_db_error(exc)}"
        ) from exc

    row = _rpc_lock_row(response)
    return {
        "renewed": bool(row.get("renewed")),
        "owner": row.get("owner"),
        "expires_at": row.get("expires_at"),
        "heartbeat_at": row.get("heartbeat_at"),
    }


def release_worker_lock(platform: str, owner: str) -> dict:
    """Release lock for current owner only. Does not clear session cookies."""
    if not platform or not owner:
        raise ValueError("platform and owner are required")
    if not worker_lock_schema_ready():
        raise WorkerLockError(worker_lock_migration_message())

    client = get_supabase_client()
    try:
        response = client.rpc(
            "release_scraper_worker_lock",
            {
                "p_platform": platform,
                "p_owner": owner,
            },
        ).execute()
    except Exception as exc:
        raise WorkerLockError(
            f"release_worker_lock failed: {redact_db_error(exc)}"
        ) from exc

    row = _rpc_lock_row(response)
    return {
        "released": bool(row.get("released")),
        "owner": row.get("owner"),
        "expires_at": row.get("expires_at"),
        "heartbeat_at": row.get("heartbeat_at"),
    }


# ---------------------------------------------------------------------------
# One-time BTG category / run status repairs
# ---------------------------------------------------------------------------

_CATEGORY_MISSING_TOKENS = {
    "platform_category",
    "platform_category_path",
    "platform_category_raw",
    "category",
}


def _strip_category_noise_from_lists(missing_fields, warnings, meta):
    meta = dict(meta or {})
    missing = [
        f for f in (missing_fields or [])
        if str(f) not in _CATEGORY_MISSING_TOKENS and "platform_category" not in str(f)
    ]
    # Date-only timeline is not a failed visible field for BTG.
    warns = []
    has_timeline_note = False
    for w in warnings or []:
        ws = str(w)
        if "CATEGORY" in ws.upper() or "platform_category" in ws:
            continue
        if ws == "TIMELINE_NOT_A_DURATION":
            has_timeline_note = True
            continue  # informational, not a failure signal for repair eligibility
        warns.append(w)
    missing_vis = [
        f for f in (meta.get("fields_missing_but_visible") or [])
        if f not in _CATEGORY_MISSING_TOKENS
    ]
    if has_timeline_note:
        missing_vis = [f for f in missing_vis if f != "project_length"]
        missing = [f for f in missing if f != "project_length"]
    not_exposed = list(meta.get("fields_not_exposed") or [])
    for field in (
        "platform_category",
        "platform_category_path",
        "platform_category_raw",
    ):
        if field not in not_exposed:
            not_exposed.append(field)
    visible = [
        f for f in (meta.get("fields_visible_on_page") or [])
        if f not in _CATEGORY_MISSING_TOKENS
    ]
    if has_timeline_note:
        # project_length was never truly visible as a duration
        visible = [f for f in visible if f != "project_length" or f in (meta.get("fields_extracted") or [])]
    meta["fields_not_exposed"] = not_exposed
    meta["fields_missing_but_visible"] = missing_vis
    meta["fields_visible_on_page"] = visible
    meta.setdefault("platform_capabilities", {})["category_exposed"] = False
    return missing, warns, meta


def btg_row_eligible_for_category_repair(row: dict) -> tuple[bool, str]:
    """Return (eligible, skip_reason)."""
    if (row.get("platform") or "").lower() != PLATFORM_BTG:
        return False, "not_btg"
    status = (row.get("detail_extraction_status") or "").upper()
    if status in ("FAILED", "TIMEOUT"):
        return False, "real_detail_failure"
    failure = (row.get("detail_failure_code") or "").upper()
    if failure and failure not in (
        "",
        "CATEGORY_MISSING",
        "PLATFORM_CATEGORY_MISSING",
    ):
        return False, f"failure_code:{failure}"
    if not str(row.get("description") or "").strip():
        return False, "no_meaningful_description"

    missing, warns, meta = _strip_category_noise_from_lists(
        row.get("missing_fields"),
        row.get("extraction_warnings"),
        row.get("extraction_metadata"),
    )
    if meta.get("fields_missing_but_visible"):
        return False, "visible_fields_still_missing"
    if missing:
        return False, f"missing_fields_remain:{missing}"
    # Already correct
    if (
        status == "COMPLETE"
        and (row.get("platform_category_extraction_status") or "").upper() == "NOT_EXPOSED"
        and not any(str(f) in _CATEGORY_MISSING_TOKENS for f in (row.get("missing_fields") or []))
    ):
        return False, "already_repaired"
    return True, ""


def repair_btg_category_status(*, dry_run: bool = False, limit: int = 5000) -> dict:
    """
    Normalize BTG rows that were marked PARTIAL/MISSING only because category
    is not exposed on BTG. Preserves email and occurrence timestamps.
    """
    client = get_supabase_client()
    scanned = eligible = updated = skipped = failed = 0
    offset = 0
    page = 200
    while scanned < limit:
        response = _execute(
            "repair_btg_category_scan",
            "projects",
            client.table("projects")
            .select(
                "id,platform,project_id,title,description,detail_extraction_status,"
                "detail_failure_code,detail_last_error,missing_fields,extraction_warnings,"
                "extraction_metadata,platform_category,platform_category_path,"
                "platform_category_raw,platform_category_source,platform_category_confidence,"
                "platform_category_extraction_status,scraped_at,email_status,email_sent"
            )
            .eq("platform", PLATFORM_BTG)
            .order("scraped_at", desc=True)
            .range(offset, offset + page - 1),
        )
        rows = _rows(response)
        if not rows:
            break
        for row in rows:
            scanned += 1
            ok, reason = btg_row_eligible_for_category_repair(row)
            if not ok:
                skipped += 1
                continue
            eligible += 1
            missing, warns, meta = _strip_category_noise_from_lists(
                row.get("missing_fields"),
                row.get("extraction_warnings"),
                row.get("extraction_metadata"),
            )
            payload = {
                "platform_category": None,
                "platform_category_path": [],
                "platform_category_raw": None,
                "platform_category_source": None,
                "platform_category_confidence": None,
                "platform_category_extraction_status": "NOT_EXPOSED",
                "detail_extraction_status": "COMPLETE",
                "detail_failure_code": None,
                "detail_last_error": None,
                "missing_fields": missing,
                "extraction_warnings": warns,
                "extraction_metadata": meta,
            }
            # Clear Material-icon leak left in location
            loc = str(row.get("location") or "").strip().lower()
            if loc in ("home", "home_work", "home_work_filled", "place", "location_on"):
                payload["location"] = None
            if dry_run:
                updated += 1
                continue
            try:
                _execute(
                    "repair_btg_category_update",
                    "projects",
                    client.table("projects").update(payload).eq("id", row["id"]).select("id"),
                    platform=PLATFORM_BTG,
                    project_id=row.get("project_id") or "",
                )
                updated += 1
            except Exception:
                failed += 1
        if len(rows) < page:
            break
        offset += page
    return {
        "rows_scanned": scanned,
        "rows_eligible": eligible,
        "rows_updated": updated,
        "rows_skipped_due_to_real_failures": skipped,
        "rows_failed": failed,
        "dry_run": dry_run,
    }


def repair_btg_run_status(*, dry_run: bool = False, limit: int = 500) -> dict:
    """
    Mark BTG scraper_runs COMPLETED when PARTIAL only due to category-era noise.
    """
    client = get_supabase_client()
    scanned = eligible = updated = skipped = failed = 0
    response = _execute(
        "repair_btg_run_scan",
        "scraper_runs",
        client.table("scraper_runs")
        .select("*")
        .eq("platform", PLATFORM_BTG)
        .eq("status", "PARTIAL")
        .order("started_at", desc=True)
        .limit(limit),
    )
    runs = _rows(response)
    for run in runs:
        scanned += 1
        run_id = run.get("id")
        if run.get("failure_code") or run.get("failure_reason"):
            skipped += 1
            continue
        if int(run.get("cards_failed") or 0) > 0:
            skipped += 1
            continue
        if int(run.get("emails_failed") or 0) > 0:
            skipped += 1
            continue
        # Any related project still not COMPLETE?
        proj_resp = _execute(
            "repair_btg_run_projects",
            "projects",
            client.table("projects")
            .select("id,detail_extraction_status")
            .eq("platform", PLATFORM_BTG)
            .eq("scraper_run_id", run_id)
            .neq("detail_extraction_status", "COMPLETE")
            .limit(5),
        )
        bad = _rows(proj_resp)
        if bad:
            skipped += 1
            continue
        # Failed email attempts for projects in this run
        proj_ids_resp = _execute(
            "repair_btg_run_project_ids",
            "projects",
            client.table("projects")
            .select("id")
            .eq("scraper_run_id", run_id)
            .limit(200),
        )
        project_ids = [p["id"] for p in _rows(proj_ids_resp) if p.get("id")]
        email_failed = False
        for pid in project_ids[:50]:
            er = _execute(
                "repair_btg_run_email_one",
                "email_attempts",
                client.table("email_attempts")
                .select("id")
                .eq("project_id", pid)
                .eq("status", "FAILED")
                .limit(1),
            )
            if _rows(er):
                email_failed = True
                break
        if email_failed:
            skipped += 1
            continue

        eligible += 1
        payload = {
            "status": "COMPLETED",
            "failure_code": None,
            "failure_reason": None,
        }
        # Only rewrite counters when they clearly look category-inflated and no real fails remain.
        details_failed = int(run.get("details_failed") or 0)
        details_attempted = int(run.get("details_attempted") or 0)
        if details_failed > 0 and not bad and details_attempted > 0:
            # Do not rewrite if historical failed details may have been real — only zero when
            # every linked project is COMPLETE (already checked).
            payload["details_failed"] = 0
            payload["details_completed"] = details_attempted

        if dry_run:
            updated += 1
            continue
        try:
            _execute(
                "repair_btg_run_update",
                "scraper_runs",
                client.table("scraper_runs").update(payload).eq("id", run_id).select("id"),
                platform=PLATFORM_BTG,
            )
            updated += 1
        except Exception:
            failed += 1
    return {
        "rows_scanned": scanned,
        "rows_eligible": eligible,
        "rows_updated": updated,
        "rows_skipped_due_to_real_failures": skipped,
        "rows_failed": failed,
        "dry_run": dry_run,
    }

