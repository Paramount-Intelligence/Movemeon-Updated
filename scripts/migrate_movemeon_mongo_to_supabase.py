#!/usr/bin/env python
"""
Optional one-off MongoDB → Supabase migration for historical MoveMeOn rows.

Production monitor must NEVER import this module.
Does not delete MongoDB data. Run explicitly when needed:

  # Preview counts and mappings (no writes):
  python scripts/migrate_movemeon_mongo_to_supabase.py --dry-run

  # Insert into Supabase (requires SUPABASE_URL + SUPABASE_SECRET_KEY):
  python scripts/migrate_movemeon_mongo_to_supabase.py --batch-size 100

Environment:
  MONGO_URI          Required for this script only (MongoDB source).
  SUPABASE_URL       Required for inserts (same as production monitor).
  SUPABASE_SECRET_KEY (or legacy SUPABASE_SERVICE_ROLE_KEY)

Source collection: office_monitor.movemeon_projects
Target table: public.projects (platform='movemeon')

Historical rows are inserted with email_status SUPPRESSED or NOT_REQUIRED so
the live monitor will not auto-email migrated records.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(encoding="utf-8-sig")

# Isolated optional dependency — not a production runtime import path.
try:
    from pymongo import MongoClient
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "pymongo is required only for this optional migration script. "
        "Install temporarily: pip install pymongo"
    ) from exc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import database as db  # noqa: E402

PLATFORM = db.PLATFORM_MOVEMEON
MONGO_DB = "office_monitor"
MONGO_COLLECTION = "movemeon_projects"

_TITLE_COMPANY_SUFFIX = re.compile(r"^(.+?)\s+\(([^)]+)\)\s*$")
_ENGAGEMENT_KEYWORDS = (
    "contract",
    "permanent",
    "full-time",
    "part-time",
    "temporary",
)
_MAPPED_MONGO_KEYS = frozenset(
    {
        "_id",
        "id",
        "project_id",
        "title",
        "description",
        "location",
        "budget",
        "duration",
        "time_posted",
        "status",
        "url",
        "detected_at",
        "platform",
        "emailed",
    }
)


def _null_placeholder(value, placeholders: frozenset[str]):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in placeholders:
        return None
    return text


def _parse_title(raw_title: str | None) -> tuple[str, str | None]:
    title = (raw_title or "").strip()
    company = None
    match = _TITLE_COMPANY_SUFFIX.match(title)
    if match:
        title = match.group(1).strip()
        company = match.group(2).strip() or None
    return title or "Untitled", company


def _split_duration(duration: str | None) -> tuple[str | None, str | None]:
    text = (duration or "").strip()
    if not text:
        return None, None
    lower = text.lower()
    if any(kw in lower for kw in _ENGAGEMENT_KEYWORDS):
        return text, None
    return None, text


def _parse_detected_at(value) -> datetime | None:
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
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _build_raw_data(doc: dict, company: str | None) -> dict:
    raw = {
        "mongo_id": str(doc.get("_id")),
        "legacy_detected_at": doc.get("detected_at"),
        "migration_source": "mongo_movemeon_projects",
    }
    if company:
        raw["movemeon_company"] = company
    for key, value in doc.items():
        if key not in _MAPPED_MONGO_KEYS:
            raw[key] = value
    return raw


def _email_fields(doc: dict) -> dict:
    emailed = bool(doc.get("emailed"))
    if emailed:
        return {
            "email_status": "SUPPRESSED",
            "email_eligible": False,
            "email_sent": True,
            "email_not_sent_reason": "MIGRATED_HISTORICAL_ALREADY_EMAILED",
        }
    return {
        "email_status": "NOT_REQUIRED",
        "email_eligible": False,
        "email_sent": False,
        "email_not_sent_reason": "MIGRATED_HISTORICAL_NOT_REQUIRED",
    }


def map_mongo_doc(doc: dict) -> dict:
    project_id = str(doc.get("project_id") or doc.get("id") or "").strip()
    title, company = _parse_title(doc.get("title"))
    description = (doc.get("description") or "").strip() or None
    engagement_type, duration_text = _split_duration(doc.get("duration"))
    scraped_at = _parse_detected_at(doc.get("detected_at"))
    email = _email_fields(doc)

    source_url = (doc.get("url") or "").strip()
    if not source_url and project_id:
        source_url = f"https://portal.movemeon.com/jobs/{project_id}"

    mapped = {
        "platform": PLATFORM,
        "project_id": project_id,
        "source_url": source_url,
        "title": title,
        "short_description": description,
        "description": description,
        "location": doc.get("location"),
        "budget_text": doc.get("budget"),
        "duration_text": duration_text,
        "engagement_type": engagement_type,
        "time_posted_text": _null_placeholder(
            doc.get("time_posted"), frozenset({"Recently"})
        ),
        "status": _null_placeholder(doc.get("status"), frozenset({"New"})),
        "raw_data": _build_raw_data(doc, company),
        "card_extraction_status": "PARTIAL",
        "detail_extraction_status": "NOT_ATTEMPTED",
        "extraction_metadata": {"migrated_from_mongo": True},
        **email,
    }
    if scraped_at is not None:
        mapped["_scraped_at"] = scraped_at
    return mapped


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Migrate historical MoveMeOn MongoDB projects to Supabase."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args(argv)

    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        raise SystemExit("MONGO_URI is required for migration")

    client = MongoClient(mongo_uri)
    coll = client[MONGO_DB][MONGO_COLLECTION]
    cursor = coll.find({"platform": {"$in": [PLATFORM, None]}})

    batch = []
    total = inserted = skipped = 0
    for doc in cursor:
        total += 1
        mapped = map_mongo_doc(doc)
        if not mapped["project_id"] or not mapped["title"]:
            skipped += 1
            continue

        existing = db.get_latest_project_occurrence(PLATFORM, mapped["project_id"])
        mongo_id = mapped["raw_data"]["mongo_id"]
        if existing and (existing.get("raw_data") or {}).get("mongo_id") == mongo_id:
            skipped += 1
            continue

        batch.append(mapped)
        if len(batch) >= args.batch_size:
            inserted += _flush(batch, dry_run=args.dry_run)
            batch = []

    if batch:
        inserted += _flush(batch, dry_run=args.dry_run)

    print(
        f"Done total={total} inserted={inserted} skipped={skipped} dry_run={args.dry_run}"
    )
    return 0


def _flush(batch, *, dry_run):
    if dry_run:
        print(f"  [dry-run] would insert {len(batch)} rows")
        return len(batch)

    n = 0
    for item in batch:
        scraped_at = item.pop("_scraped_at", None)
        db.insert_project_occurrence(
            item,
            email_status=item["email_status"],
            email_eligible=item["email_eligible"],
            email_sent=item["email_sent"],
            email_not_sent_reason=item.get("email_not_sent_reason"),
            scraped_at=scraped_at,
        )
        n += 1
    print(f"  inserted {n}")
    return n


if __name__ == "__main__":
    raise SystemExit(main())
