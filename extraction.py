"""
Robust MoveMeOn field extraction helpers (no database I/O).
Shared status/merge/budget helpers also support Catalant and BTG platforms.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_CATEGORY_NOISE = (
    "posted", "login", "search", "budget", "location", "timeline",
    "start date", "contracting", "industry", "description", "summary",
    "apply", "save", "share", "filter", "sort", "results", "recommended",
)
_INVALID_CATEGORY_VALUES = {"unclassified", "unknown", "n/a", "na", "none", "null"}

_PLACEHOLDER_STRINGS = {
    "",
    "unknown",
    "unclassified",
    "n/a",
    "na",
    "none",
    "not specified",
    "tbd",
    "recently",
    "new",
}

# Platform field exposure — avoid scattering `if platform == ...` checks.
PLATFORM_CAPABILITIES: dict[str, dict[str, Any]] = {
    "catalant": {
        "category_exposed": True,
        "card_required_fields": ("project_id", "title", "source_url"),
        "card_expected_visible_fields": (
            "project_id", "title", "source_url", "time_posted_text",
            "location", "budget_text", "short_description",
        ),
        "never_required_detail_fields": (),
    },
    "btg": {
        "category_exposed": False,
        "card_required_fields": ("project_id", "title", "source_url"),
        "card_expected_visible_fields": (
            "project_id", "title", "source_url",
        ),
        "never_required_detail_fields": (
            "platform_category",
            "platform_category_path",
            "platform_category_raw",
            "skills",
            "expertise",
            "deliverables",
            "weekly_commitment",
            "application_deadline",
            "contracting_process",
        ),
    },
    "movemeon": {
        "category_exposed": False,
        "card_required_fields": ("project_id", "title", "source_url"),
        "card_expected_visible_fields": (
            "project_id", "title", "source_url",
        ),
        "never_required_detail_fields": (
            "platform_category",
            "platform_category_path",
            "platform_category_raw",
            "contracting_process",
            "level_of_support",
            "workstream",
            "estimated_hours",
            "weekly_commitment",
            "expertise",
            "deliverables",
        ),
    },
}

CATEGORY_FIELDS = (
    "platform_category",
    "platform_category_path",
    "platform_category_raw",
)

CARD_REQUIRED_FIELDS = ("project_id", "title", "source_url")
CARD_EXPECTED_VISIBLE_FIELDS = (
    "project_id", "title", "source_url", "time_posted_text",
    "location", "budget_text", "short_description",
)
CORE_DETAIL_FIELDS = (
    "description",
    "location",
    "location_preference",
    "remote_or_onsite",
    "project_length",
    "start_date_text",
    "budget_text",
    "level_of_support",
    "industry",
    "engagement_type",
)


def get_platform_capabilities(platform: str) -> dict[str, Any]:
    key = (platform or "").strip().lower()
    return dict(PLATFORM_CAPABILITIES.get(key) or {})


def platform_exposes_field(platform: str, field_name: str) -> bool:
    """
    Whether a platform is known to expose a field.
    Unknown platforms: do not assume exposure (caller should use DOM visibility).
    """
    caps = get_platform_capabilities(platform)
    if not caps:
        return False
    if field_name in CATEGORY_FIELDS or field_name == "category":
        return bool(caps.get("category_exposed"))
    never = set(caps.get("never_required_detail_fields") or ())
    if field_name in never:
        return False
    return True


def apply_not_exposed_category(details: dict, *, platform: str, metadata: Optional[dict] = None) -> None:
    """Stamp BTG-style not-exposed category fields without inventing a category."""
    details["platform_category"] = None
    details["platform_category_path"] = []
    details["platform_category_raw"] = None
    details["platform_category_source"] = None
    details["platform_category_confidence"] = None
    details["platform_category_extraction_status"] = "NOT_EXPOSED"
    if metadata is not None:
        metadata.setdefault("fields_not_exposed", [])
        for field in CATEGORY_FIELDS:
            if field not in metadata["fields_not_exposed"]:
                metadata["fields_not_exposed"].append(field)
        # Ensure category is never treated as a visible miss
        missing = metadata.get("fields_missing_but_visible") or []
        metadata["fields_missing_but_visible"] = [
            f for f in missing if f not in CATEGORY_FIELDS and f != "category"
        ]
        visible = metadata.get("fields_visible_on_page") or []
        metadata["fields_visible_on_page"] = [
            f for f in visible if f not in CATEGORY_FIELDS and f != "category"
        ]
        extracted = metadata.get("fields_extracted") or []
        metadata["fields_extracted"] = [
            f for f in extracted if f not in CATEGORY_FIELDS and f != "category"
        ]
        metadata.setdefault("platform_capabilities", {})
        metadata["platform_capabilities"]["category_exposed"] = platform_exposes_field(
            platform, "platform_category"
        )


def filter_category_from_missing_and_warnings(
    missing_fields: Optional[list],
    warnings: Optional[list],
    *,
    platform: str,
) -> tuple[list, list]:
    if platform_exposes_field(platform, "platform_category"):
        return list(missing_fields or []), list(warnings or [])
    drop_missing = set(CATEGORY_FIELDS) | {"category"}
    cleaned_missing = []
    for item in missing_fields or []:
        text = str(item)
        if text in drop_missing:
            continue
        if text.startswith("VISIBLE_FIELD_NOT_EXTRACTED:") and text.split(":", 1)[-1] in drop_missing:
            continue
        cleaned_missing.append(item)
    category_warn_tokens = (
        "CATEGORY_NOT_FOUND",
        "CATEGORY_CONTAINER_NOT_FOUND",
        "PLATFORM_CATEGORY_MISSING",
        "VISIBLE_FIELD_NOT_EXTRACTED:platform_category",
        "VISIBLE_FIELD_NOT_EXTRACTED:platform_category_path",
        "VISIBLE_FIELD_NOT_EXTRACTED:platform_category_raw",
    )
    cleaned_warnings = []
    for w in warnings or []:
        ws = str(w)
        if any(tok in ws for tok in category_warn_tokens):
            continue
        cleaned_warnings.append(w)
    return cleaned_missing, cleaned_warnings


BTG_BASE = "https://talent.businesstalentgroup.com"
TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "source",
}


def category_path_from_text(cat_text: str) -> list[str]:
    if not cat_text:
        return []
    text = (
        str(cat_text)
        .replace("\u00a0", " ")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .strip()
    )
    text = re.sub(r"\s*[›»→]\s*", " > ", text)
    text = re.sub(r"\s*\|\s*", " > ", text)
    return [p.strip() for p in re.split(r"\s*>\s*", text) if p and p.strip()]


def normalize_category_candidate(raw, *, allow_single: bool = False):
    """Return (category, path, raw, rejected_reason)."""
    if not raw:
        return None, [], "", "empty"
    text = str(raw).replace("\u00a0", " ").strip()
    if not text or len(text) > 200:
        return None, [], text, "too_long_or_empty"
    path = category_path_from_text(text)
    if not path and allow_single:
        if "\n" in text or ":" in text:
            return None, [], text, "looks_like_label"
        if not (3 <= len(text) <= 80):
            return None, [], text, "length"
        lowered = text.lower()
        if any(n in lowered for n in _CATEGORY_NOISE):
            return None, [], text, "noise"
        path = [text.strip()]
    if not path:
        return None, [], text, "no_path"
    top = path[0].strip()
    if top.lower() in _INVALID_CATEGORY_VALUES:
        return None, path, text, "invalid_placeholder"
    if any(n in top.lower() for n in _CATEGORY_NOISE):
        return None, path, text, "noise"
    return top, path, text, None


def category_result(category, path, raw, source, confidence, status):
    return {
        "platform_category": category or None,
        "platform_category_path": path or [],
        "platform_category_raw": raw or None,
        "platform_category_source": source,
        "platform_category_confidence": confidence,
        "platform_category_extraction_status": status,
    }


def extract_category_from_body_text(body_text: str) -> dict:
    if not body_text:
        return category_result(None, [], None, None, None, "MISSING")

    for m in re.finditer(
        r"(?im)^(?:Category|Practice Area|Functional Area|Pools?)\s*:\s*(.+)$",
        body_text,
    ):
        cat, path, cleaned, rejected = normalize_category_candidate(
            m.group(1), allow_single=True
        )
        if rejected == "invalid_placeholder":
            return category_result(
                None, path, cleaned, "text_label", None, "REJECTED_INVALID_CANDIDATE"
            )
        if cat:
            return category_result(
                cat, path, cleaned, "text_label", "LOW", "FOUND_TEXT_FALLBACK"
            )

    for m in re.finditer(
        r"(?m)^([A-Za-z][^\n:]{2,60}?)\s*>\s*([^\n]{2,80})$",
        body_text,
    ):
        line = m.group(0).strip()
        if len(line) > 120:
            continue
        cat, path, cleaned, rejected = normalize_category_candidate(
            line, allow_single=False
        )
        if rejected == "invalid_placeholder":
            return category_result(
                None, path, cleaned, "text_breadcrumb", None, "REJECTED_INVALID_CANDIDATE"
            )
        if cat and len(path) >= 2:
            return category_result(
                cat, path, cleaned, "text_breadcrumb", "LOW", "FOUND_TEXT_FALLBACK"
            )

    if re.search(r"(?im)^\s*unclassified\s*$", body_text):
        return category_result(
            None, [], "Unclassified", "text_reject", None, "REJECTED_INVALID_CANDIDATE"
        )

    return category_result(None, [], None, None, None, "MISSING")


def extract_category_from_embedded_json(content: str) -> dict:
    if not content or len(content) > 200000:
        return category_result(None, [], None, None, None, "MISSING")
    if not any(tok in content.lower() for tok in ("category", "breadcrumb", "practice")):
        return category_result(None, [], None, None, None, "MISSING")
    for pattern in (
        r'"category"\s*:\s*"([^"]{2,120})"',
        r'"practiceArea"\s*:\s*"([^"]{2,120})"',
        r'"functionalArea"\s*:\s*"([^"]{2,120})"',
    ):
        m = re.search(pattern, content, re.IGNORECASE)
        if not m:
            continue
        cat, path, cleaned, rejected = normalize_category_candidate(
            m.group(1), allow_single=True
        )
        if rejected == "invalid_placeholder":
            return category_result(
                None, path, cleaned, "embedded_json", None, "REJECTED_INVALID_CANDIDATE"
            )
        if cat:
            return category_result(
                cat, path, cleaned, "embedded_json", "MEDIUM", "FOUND_EMBEDDED_DATA"
            )
    return category_result(None, [], None, None, None, "MISSING")


def is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _PLACEHOLDER_STRINGS or not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def normalize_visible_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def field_result(value=None, *, raw_value=None, source=None, selector_or_label=None, confidence=None):
    return {
        "value": value,
        "raw_value": raw_value if raw_value is not None else value,
        "source": source,
        "selector_or_label": selector_or_label,
        "confidence": confidence,
    }


def validate_extracted_value(value: Any, *, title: str = "", category: str = "") -> bool:
    if is_empty_value(value):
        return False
    if not isinstance(value, str):
        return True
    text = normalize_visible_text(value)
    if not text or len(text) < 2:
        return False
    if title and text.strip().lower() == title.strip().lower():
        return False
    if category and text.strip().lower() == category.strip().lower():
        return False
    return True


def extract_value_by_label(body_text: str, labels, *, take_last: bool = False) -> dict:
    """Extract 'Label: Value' from normalized body text (same line only)."""
    if not body_text:
        return field_result()
    label_alt = "|".join(re.escape(lbl) for lbl in labels)
    # Use [ \t]* so we never pull the next line into this label's value.
    pattern = rf"(?im)^(?:{label_alt})[ \t]*:[ \t]*(.+)$"
    matches = list(re.finditer(pattern, body_text))
    if not matches:
        return field_result()
    m = matches[-1] if take_last else matches[0]
    value = normalize_visible_text(m.group(1))
    if not value:
        return field_result()
    # Reject values that are clearly another label line
    if re.match(
        r"(?i)^(skills|requirements|benefits|apply|compensation|salary|location|industry|"
        r"function|language|flexibility|details|why apply|must-have)\b",
        value,
    ):
        return field_result()
    return field_result(
        value,
        source="label_value",
        selector_or_label=m.group(0).split(":", 1)[0].strip(),
        confidence="HIGH",
    )


def extract_section_text(body_text: str, start_labels, end_labels) -> dict:
    if not body_text:
        return field_result()
    start_alt = "|".join(re.escape(s) for s in start_labels)
    end_alt = "|".join(re.escape(s) for s in end_labels)
    pattern = (
        rf"(?is)(?:^|\n)\s*(?:{start_alt})\s*\n+"
        rf"(.+?)"
        rf"(?=\n\s*(?:{end_alt})\b|\Z)"
    )
    m = re.search(pattern, body_text)
    if not m:
        return field_result()
    value = normalize_visible_text(m.group(1))
    if len(value) < 30:
        return field_result()
    return field_result(
        value,
        source="section_text",
        selector_or_label=start_labels[0],
        confidence="MEDIUM",
    )


def extract_list_values(body_text: str, labels) -> dict:
    result = extract_value_by_label(body_text, labels)
    if is_empty_value(result.get("value")):
        return field_result(value=[], source=None)
    raw = str(result["value"])
    parts = [p.strip() for p in re.split(r"[,;/|•\n]+", raw) if p.strip()]
    return field_result(
        parts,
        raw_value=raw,
        source=result.get("source"),
        selector_or_label=result.get("selector_or_label"),
        confidence=result.get("confidence") or "MEDIUM",
    )


def extract_embedded_json(content: str, keys) -> dict:
    if not content:
        return field_result()
    for key in keys:
        m = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]{{2,500}})"', content, re.I)
        if m:
            return field_result(
                normalize_visible_text(m.group(1)),
                source="embedded_json",
                selector_or_label=key,
                confidence="MEDIUM",
            )
    return field_result()


def validate_budget_candidate(candidate: str, project: Optional[dict] = None) -> tuple[bool, str]:
    project = project or {}
    title = normalize_visible_text(project.get("title") or "")
    text = normalize_visible_text(candidate)
    if not text:
        return False, "empty"
    if text.lower() in ("not provided", "n/a", "none", "not available", "tbd"):
        return True, "ok_not_provided"
    if title and text.lower() == title.lower():
        return False, "equals_title"
    if title and title.lower() in text.lower() and len(text) > len(title) * 0.8:
        return False, "contains_title"
    if len(text) > 80:
        return False, "too_long"
    if not re.search(r"(\$|usd|eur|gbp|\d)", text, re.I):
        return False, "no_money_token"
    # Reject long descriptive prose with many words and no clear rate pattern
    if len(text.split()) > 12 and not re.search(r"\$?\d[\d,]*(?:\.\d+)?\s*/?\s*(hr|hour|day|mo|month)?", text, re.I):
        return False, "looks_like_prose"
    return True, "ok"


def _is_non_duration_timeline(value: str) -> bool:
    """True when Timeline is a start cue (ASAP) rather than a length."""
    s = normalize_visible_text(value).lower()
    return s in ("asap", "immediately", "immediate", "tbd", "flexible", "as soon as possible")


def parse_budget(candidate: str) -> dict:
    text = normalize_visible_text(candidate)
    out = {
        "budget_text": text or None,
        "budget_min": None,
        "budget_max": None,
        "budget_currency": None,
        "billing_type": None,
        "hourly_rate": None,
        "daily_rate": None,
        "rate_currency": None,
    }
    if not text or text.lower() in ("not provided", "n/a", "none"):
        out["budget_text"] = None if not text else text
        return out

    if re.search(r"(?i)\bUSD\b|\$", text) and not re.search(r"(?i)\bA\$|S\$|C\$|AU\$|SG\$", text):
        out["budget_currency"] = "USD"
        out["rate_currency"] = "USD"
    elif re.search(r"(?i)\bGBP\b|£", text):
        out["budget_currency"] = "GBP"
        out["rate_currency"] = "GBP"
    elif re.search(r"(?i)\bEUR\b|€", text):
        out["budget_currency"] = "EUR"
        out["rate_currency"] = "EUR"
    elif re.search(r"(?i)\bA\$|AU\$|AUD\b", text):
        out["budget_currency"] = "AUD"
        out["rate_currency"] = "AUD"
    elif re.search(r"(?i)\bS\$|SG\$|SGD\b", text):
        out["budget_currency"] = "SGD"
        out["rate_currency"] = "SGD"

    def _scale(num: float, had_k: bool) -> float:
        return num * 1000.0 if had_k and num < 1000 else num

    hourly = re.search(
        r"(?:£|\$|€)?\s*([\d,]+(?:\.\d+)?)\s*([kK])?\s*/\s*(?:hr|hour)\b",
        text,
        re.I,
    )
    if hourly:
        amount = _scale(float(hourly.group(1).replace(",", "")), bool(hourly.group(2)))
        out["hourly_rate"] = amount
        out["billing_type"] = "hourly"
        return out

    daily = re.search(
        r"(?:£|\$|€)?\s*([\d,]+(?:\.\d+)?)\s*([kK])?\s*/\s*day\b",
        text,
        re.I,
    )
    if daily:
        amount = _scale(float(daily.group(1).replace(",", "")), bool(daily.group(2)))
        out["daily_rate"] = amount
        out["billing_type"] = "daily"
        return out

    rng = re.search(
        r"(?:£|\$|€)?\s*([\d,]+(?:\.\d+)?)\s*([kK])?\s*[–\-—]+\s*(?:to\s+)?"
        r"(?:£|\$|€)?\s*([\d,]+(?:\.\d+)?)\s*([kK])?",
        text,
        re.I,
    )
    if not rng:
        rng = re.search(
            r"(?:£|\$|€)?\s*([\d,]+(?:\.\d+)?)\s*([kK])?\s+to\s+"
            r"(?:£|\$|€)?\s*([\d,]+(?:\.\d+)?)\s*([kK])?",
            text,
            re.I,
        )
    if rng:
        lo = _scale(float(rng.group(1).replace(",", "")), bool(rng.group(2)))
        hi = _scale(float(rng.group(3).replace(",", "")), bool(rng.group(4)))
        out["budget_min"] = lo
        out["budget_max"] = hi
        out["billing_type"] = "fixed_range"
        return out

    single = re.search(r"(?:£|\$|€)?\s*([\d,]+(?:\.\d+)?)\s*([kK])?\b", text)
    if single:
        amount = _scale(float(single.group(1).replace(",", "")), bool(single.group(2)))
        out["budget_min"] = amount
        out["budget_max"] = amount
        out["billing_type"] = "fixed"
        return out
    return out


def extract_title_rate_fallback(title: str) -> dict:
    """Low-confidence: isolate $N/hr from title without storing the whole title."""
    title = normalize_visible_text(title)
    m = re.search(r"(\$\s*[\d,]+(?:\.\d+)?\s*/\s*(?:hr|hour|day))", title, re.I)
    if not m:
        return {}
    parsed = parse_budget(m.group(1))
    parsed["budget_source"] = "title_rate_fallback"
    parsed["budget_confidence"] = "LOW"
    return parsed


def parse_relative_posted_time(text: str, scraped_at: Optional[datetime] = None):
    """Return (source_posted_at, is_estimated) or (None, False)."""
    scraped_at = scraped_at or datetime.now(timezone.utc)
    if scraped_at.tzinfo is None:
        scraped_at = scraped_at.replace(tzinfo=timezone.utc)
    s = normalize_visible_text(text).lower()
    if not s or s in ("unknown",):
        return None, False
    if any(tok in s for tok in ("just now", "moment", "second")):
        return scraped_at, True
    if re.search(r"\ba\s+minute\b", s):
        return scraped_at - timedelta(minutes=1), True
    if re.search(r"\ban?\s+hour\b", s):
        return scraped_at - timedelta(hours=1), True
    if re.search(r"\ba\s+day\b", s):
        return scraped_at - timedelta(days=1), True
    if re.search(r"\ba\s+week\b", s):
        return scraped_at - timedelta(weeks=1), True
    m = re.search(r"(\d+)\s*(minute|hour|day|week|month)s?", s)
    if not m:
        return None, False
    n = int(m.group(1))
    unit = m.group(2)
    delta = {
        "minute": timedelta(minutes=n),
        "hour": timedelta(hours=n),
        "day": timedelta(days=n),
        "week": timedelta(weeks=n),
        "month": timedelta(days=30 * n),
    }[unit]
    return scraped_at - delta, True


def parse_source_start_date(text: str):
    text = normalize_visible_text(text)
    if not text:
        return None
    if re.search(r"(?i)\b(asap|immediately|tbd|flexible)\b", text):
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text[:40], fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).date().isoformat()
        except ValueError:
            return None
    return None


def calculate_card_extraction_status(project: dict, platform: Optional[str] = None) -> str:
    platform = (platform or project.get("platform") or "movemeon").strip().lower()
    caps = get_platform_capabilities(platform)
    required = tuple(caps.get("card_required_fields") or CARD_REQUIRED_FIELDS)
    expected = tuple(caps.get("card_expected_visible_fields") or CARD_EXPECTED_VISIBLE_FIELDS)

    pid = project.get("project_id") or project.get("id")
    title = project.get("title")
    url = project.get("source_url") or project.get("url")
    if is_empty_value(pid) or is_empty_value(title) or is_empty_value(url):
        return "FAILED"

    missing_expected = []
    for field in expected:
        if field in required:
            continue
        val = project.get(field)
        if field == "time_posted_text":
            val = project.get("time_posted_text") or project.get("time_posted")
        if field == "budget_text":
            val = project.get("budget_text") or project.get("budget")
        if field == "short_description":
            val = project.get("short_description") or project.get("description")
        if field == "location":
            # Remote-only BTG cards may have no geo; remote mode is enough.
            val = (
                project.get("location")
                or project.get("remote_or_onsite")
                or project.get("location_preference")
            )
        if is_empty_value(val) or (field == "time_posted_text" and str(val).lower() == "unknown"):
            missing_expected.append(field)
    if missing_expected:
        return "PARTIAL"
    return "COMPLETE"


def calculate_detail_extraction_status(
    *,
    attempted: bool,
    page_ok: bool,
    timeout: bool = False,
    fields_visible: Optional[list] = None,
    fields_extracted: Optional[list] = None,
    fields_missing_but_visible: Optional[list] = None,
    meaningful: bool = False,
    platform: Optional[str] = None,
) -> str:
    if not attempted:
        return "NOT_ATTEMPTED"
    if timeout:
        return "TIMEOUT"
    if not page_ok:
        return "FAILED"
    fields_visible = list(fields_visible or [])
    fields_extracted = list(fields_extracted or [])
    missing_visible = list(fields_missing_but_visible or [])
    if not missing_visible:
        missing_visible = [f for f in fields_visible if f not in fields_extracted]

    # Drop fields the platform does not expose (e.g. MoveMeOn/BTG category).
    plat = (platform or "movemeon").strip().lower()
    if not platform_exposes_field(plat, "platform_category"):
        drop = set(CATEGORY_FIELDS) | {"category"}
        fields_visible = [f for f in fields_visible if f not in drop]
        missing_visible = [f for f in missing_visible if f not in drop]
        caps = get_platform_capabilities(plat)
        never = set(caps.get("never_required_detail_fields") or ())
        missing_visible = [f for f in missing_visible if f not in never]
        fields_visible = [f for f in fields_visible if f not in never or f in fields_extracted]

    if not meaningful and not fields_extracted:
        return "FAILED"
    if missing_visible:
        return "PARTIAL"
    if fields_visible and all(f in fields_extracted for f in fields_visible):
        return "COMPLETE"
    if meaningful:
        return "COMPLETE"
    return "PARTIAL"


def compute_missing_fields(
    project: dict,
    *,
    expected_fields: Optional[list] = None,
    platform: Optional[str] = None,
) -> list:
    plat = (platform or project.get("platform") or "movemeon").strip().lower()
    if expected_fields is None:
        expected = list(CARD_REQUIRED_FIELDS)
        if project.get("detail_extraction_status") not in (None, "NOT_ATTEMPTED"):
            expected.extend(
                f for f in CORE_DETAIL_FIELDS
                if platform_exposes_field(plat, f) or f in (
                    "description", "location", "remote_or_onsite", "budget_text",
                    "level_of_support", "industry", "engagement_type",
                    "project_length", "start_date_text", "location_preference",
                )
            )
            # Prefer visibility metadata when present
            meta = project.get("extraction_metadata") or {}
            visible_miss = meta.get("fields_missing_but_visible")
            if isinstance(visible_miss, list):
                expected = list(CARD_REQUIRED_FIELDS) + list(visible_miss)
    else:
        expected = list(expected_fields)

    if not platform_exposes_field(plat, "platform_category"):
        expected = [f for f in expected if f not in CATEGORY_FIELDS and f != "category"]
        caps = get_platform_capabilities(plat)
        never = set(caps.get("never_required_detail_fields") or ())
        expected = [f for f in expected if f not in never]

    missing = []
    for field in expected:
        val = project.get(field)
        if field == "project_id":
            val = project.get("project_id") or project.get("id")
        if field == "source_url":
            val = project.get("source_url") or project.get("url")
        if field == "time_posted_text":
            val = project.get("time_posted_text") or project.get("time_posted")
        if field == "project_length":
            val = project.get("project_length") or project.get("duration_text") or project.get("duration")
        if field == "budget_text":
            val = project.get("budget_text") or project.get("budget")
        if field == "location_preference":
            val = project.get("location_preference") or project.get("location_pref")
        if field == "location":
            # Remote-only is acceptable when mode is present.
            if is_empty_value(val) and not is_empty_value(
                project.get("remote_or_onsite") or project.get("location_preference")
            ):
                continue
        if is_empty_value(val) or (field == "time_posted_text" and str(val).lower() == "unknown"):
            missing.append(field)
    return missing



def extract_detail_fields_from_body(body_text: str, *, title: str = "", project: Optional[dict] = None) -> dict:
    """Pure-text detail extraction used by Selenium wrapper and unit tests."""
    project = project or {}
    warnings = []
    metadata = {
        "fields_visible_on_page": [],
        "fields_extracted": [],
        "fields_missing_but_visible": [],
        "fields_not_exposed": [],
    }
    details: dict[str, Any] = {}

    # Description — Catalant uses "Project Description" (often truncated until More)
    desc = extract_section_text(
        body_text,
        ["Project Description", "Description", "Summary"],
        [
            "Project Logistics",
            "Other Details",
            "Budget",
            "Expert Preferences",
            "Contracting",
            "Skills",
            "Your Pitch",
            "Bookmark Project",
        ],
    )
    if desc.get("value") and validate_extracted_value(desc["value"], title=title):
        details["description"] = desc["value"]
        metadata["fields_extracted"].append("description")
        metadata["fields_visible_on_page"].append("description")
    elif re.search(r"(?im)^(?:Project )?Description\s*$", body_text or ""):
        metadata["fields_visible_on_page"].append("description")
        metadata["fields_missing_but_visible"].append("description")
        warnings.append("DESCRIPTION_CONTAINER_NOT_FOUND")
    else:
        metadata["fields_not_exposed"].append("description")

    # Duration / Timeline — prefer Duration; Timeline may be ASAP (start cue) or a length
    duration_item = extract_value_by_label(body_text, ["Duration"])
    timeline_items = []
    for m in re.finditer(
        r"(?im)^(?:Timeline|Project Length|Expected Duration|Engagement Length)\s*:\s*(.+)$",
        body_text or "",
    ):
        timeline_items.append(normalize_visible_text(m.group(1)))

    if duration_item.get("value") and validate_extracted_value(duration_item["value"], title=title):
        details["project_length"] = duration_item["value"]
        details["duration_text"] = duration_item["value"]
        metadata["fields_extracted"].append("project_length")
        metadata["fields_visible_on_page"].append("project_length")
    else:
        length_candidate = None
        for t in reversed(timeline_items):
            if t and not _is_non_duration_timeline(t):
                length_candidate = t
                break
        if length_candidate:
            details["project_length"] = length_candidate
            details["duration_text"] = length_candidate
            metadata["fields_extracted"].append("project_length")
            metadata["fields_visible_on_page"].append("project_length")
        elif timeline_items:
            metadata["fields_visible_on_page"].append("project_length")
            metadata["fields_missing_but_visible"].append("project_length")
            warnings.append("TIMELINE_NOT_A_DURATION")
        else:
            metadata["fields_not_exposed"].append("project_length")

    # Structured label fields (duration/timeline handled above)
    label_map = {
        "start_date_text": ["Start Date", "Expected Start"],
        "level_of_support": ["Expert Type", "Level of Support"],
        "industry": ["Industry", "Desired Industry Background"],
        "contracting_process": ["Contracting Process"],
        "engagement_type": ["Engagement Type"],
        "project_type": ["Project Type"],
        "workstream": ["Workstream"],
        "weekly_commitment": ["Weekly Commitment", "Hours per Week"],
        "remote_or_onsite": [
            "In-person vs. Remote",
            "In-person vs Remote",
            "Remote or Onsite",
            "Work Arrangement",
        ],
        "country_or_region": ["Country", "Region", "Country or Region"],
        "application_deadline": ["Application Deadline"],
    }
    for field, labels in label_map.items():
        item = extract_value_by_label(body_text, labels)
        visible = bool(re.search(
            rf"(?im)^(?:{'|'.join(re.escape(x) for x in labels)})\s*:",
            body_text or "",
        ))
        if visible:
            metadata["fields_visible_on_page"].append(field)
        if item.get("value") and validate_extracted_value(item["value"], title=title):
            details[field] = item["value"]
            metadata["fields_extracted"].append(field)
            if field == "start_date_text":
                details["source_start_date"] = parse_source_start_date(item["value"])
        elif visible:
            metadata["fields_missing_but_visible"].append(field)
            warnings.append(f"VISIBLE_FIELD_NOT_EXTRACTED:{field}")
        else:
            metadata["fields_not_exposed"].append(field)

    # Location preference — last Location: line (sidebar), not prose
    loc = extract_value_by_label(body_text, ["Location Preference", "Location"], take_last=True)
    if loc.get("value") and validate_extracted_value(loc["value"], title=title):
        # Reject values that look like paragraphs
        if len(str(loc["value"]).split()) <= 20:
            details["location_preference"] = loc["value"]
            details["location_pref"] = loc["value"]
            details.setdefault("location", loc["value"])
            metadata["fields_extracted"].append("location_preference")
            metadata["fields_visible_on_page"].append("location_preference")
        else:
            warnings.append("LOCATION_AMBIGUOUS")
            metadata["fields_visible_on_page"].append("location_preference")
            metadata["fields_missing_but_visible"].append("location_preference")
    elif re.search(r"(?im)^Location(?: Preference)?\s*:", body_text or ""):
        metadata["fields_visible_on_page"].append("location_preference")
        metadata["fields_missing_but_visible"].append("location_preference")
        warnings.append("VISIBLE_FIELD_NOT_EXTRACTED:location_preference")
    else:
        metadata["fields_not_exposed"].append("location_preference")

    # Budget — structured only
    budget_item = None
    m = re.search(r"(?im)^Project Budget:\s*$", body_text or "")
    if m:
        # value on following non-empty line
        after = (body_text or "")[m.end():]
        line = ""
        for raw_line in after.splitlines():
            if raw_line.strip():
                line = normalize_visible_text(raw_line)
                break
        if line:
            budget_item = field_result(line, source="label_next_line", selector_or_label="Project Budget", confidence="HIGH")
    if not budget_item or is_empty_value(budget_item.get("value")):
        budget_item = extract_value_by_label(body_text, ["Project Budget", "Budget", "Hourly Rate", "Rate"])

    if budget_item.get("value"):
        metadata["fields_visible_on_page"].append("budget_text")
        ok, reason = validate_budget_candidate(budget_item["value"], {"title": title, **project})
        if ok:
            parsed = parse_budget(budget_item["value"])
            parsed["budget_source"] = budget_item.get("source") or "structured"
            parsed["budget_confidence"] = budget_item.get("confidence") or "HIGH"
            details.update({k: v for k, v in parsed.items() if v is not None})
            metadata["fields_extracted"].append("budget_text")
        else:
            warnings.append(f"BUDGET_CANDIDATE_REJECTED_{reason.upper()}")
            metadata["fields_missing_but_visible"].append("budget_text")
    else:
        # Optional low-confidence title rate fallback
        fallback = extract_title_rate_fallback(title)
        if fallback.get("budget_text"):
            details.update(fallback)
            metadata["fields_extracted"].append("budget_text")
            warnings.append("BUDGET_FROM_TITLE_RATE_FALLBACK")
        else:
            metadata["fields_not_exposed"].append("budget_text")

    # Skills / expertise / deliverables
    for field, labels in (
        ("skills", ["Skills", "Required Skills"]),
        ("expertise", ["Expertise", "Expert Preferences"]),
        ("deliverables", ["Deliverables"]),
    ):
        item = extract_list_values(body_text, labels)
        if item.get("value"):
            details[field] = item["value"]
            metadata["fields_extracted"].append(field)
            metadata["fields_visible_on_page"].append(field)
        else:
            metadata["fields_not_exposed"].append(field)

    # Expert type standalone line
    if is_empty_value(details.get("level_of_support")):
        m = re.search(
            r"(?im)^(Independent Expert|Open to Both|Consulting Firm|Both)$",
            body_text or "",
        )
        if m:
            details["level_of_support"] = m.group(1)
            metadata["fields_extracted"].append("level_of_support")
            metadata["fields_visible_on_page"].append("level_of_support")

    meaningful = any(
        not is_empty_value(details.get(f))
        for f in ("description", "location_preference", "project_length", "industry", "contracting_process", "budget_text", "level_of_support")
    )
    platform = (project.get("platform") or "catalant").strip().lower() or "catalant"
    status = calculate_detail_extraction_status(
        attempted=True,
        page_ok=True,
        fields_visible=metadata["fields_visible_on_page"],
        fields_extracted=metadata["fields_extracted"],
        fields_missing_but_visible=metadata["fields_missing_but_visible"],
        meaningful=meaningful,
        platform=platform,
    )
    details["detail_extraction_status"] = status
    details["extraction_metadata"] = metadata
    details["extraction_warnings"] = warnings
    details["missing_fields"] = [
        f for f in metadata["fields_visible_on_page"] if f not in metadata["fields_extracted"]
    ]
    details["missing_fields"], details["extraction_warnings"] = filter_category_from_missing_and_warnings(
        details["missing_fields"], details["extraction_warnings"], platform=platform
    )
    return details


def merge_project_data(card_data: dict, detail_data: Optional[dict] = None) -> dict:
    """Safe card/detail merge — empty/placeholder detail values do not overwrite card."""
    merged = dict(card_data or {})
    warnings = list(merged.get("extraction_warnings") or [])
    detail_data = detail_data or {}

    for key, detail_val in detail_data.items():
        if key in ("extraction_warnings", "missing_fields", "extraction_metadata"):
            continue
        card_val = merged.get(key)
        if is_empty_value(detail_val):
            if not is_empty_value(card_val):
                warnings.append(f"detail_empty_preserved_card:{key}")
            continue
        if isinstance(detail_val, str) and detail_val.strip().lower() == "unclassified":
            if not is_empty_value(card_val) and str(card_val).strip().lower() != "unclassified":
                warnings.append("detail_rejected_unclassified_category")
                continue
            if is_empty_value(card_val):
                warnings.append("detail_rejected_unclassified_empty")
                continue
        if isinstance(detail_val, list) and not detail_val and card_val:
            continue
        # Never let a rejected title-budget overwrite nothing useful incorrectly
        if key == "budget_text":
            ok, reason = validate_budget_candidate(str(detail_val), merged)
            if not ok:
                warnings.append(f"BUDGET_CANDIDATE_REJECTED_{reason.upper()}")
                continue
        merged[key] = detail_val

    meta = dict(merged.get("extraction_metadata") or {})
    meta.update(detail_data.get("extraction_metadata") or {})
    if meta:
        merged["extraction_metadata"] = meta

    warnings.extend(detail_data.get("extraction_warnings") or [])
    if warnings:
        # de-dupe while preserving order
        seen = set()
        deduped = []
        for w in warnings:
            if w not in seen:
                seen.add(w)
                deduped.append(w)
        merged["extraction_warnings"] = deduped

    # Posted time normalize
    posted = merged.get("time_posted_text") or merged.get("time_posted")
    if posted and is_empty_value(merged.get("source_posted_at")):
        scraped = datetime.now(timezone.utc)
        parsed, estimated = parse_relative_posted_time(str(posted), scraped)
        if parsed is not None:
            merged["source_posted_at"] = parsed.isoformat()
            merged["source_posted_at_is_estimated"] = estimated

    # Short description vs description
    short = merged.get("short_description")
    title = merged.get("title") or ""
    if short and (
        short.strip().lower() == title.strip().lower()
        or short == merged.get("platform_category")
        or short.lower().startswith("posted")
    ):
        warnings.append("SHORT_DESCRIPTION_REJECTED_NOISE")
        merged["short_description"] = None

    merged["card_extraction_status"] = calculate_card_extraction_status(merged)
    if "detail_extraction_status" not in merged:
        merged["detail_extraction_status"] = detail_data.get("detail_extraction_status") or "NOT_ATTEMPTED"

    expected = list(CARD_REQUIRED_FIELDS)
    if merged.get("detail_extraction_status") not in (None, "NOT_ATTEMPTED"):
        expected.extend([f for f in CORE_DETAIL_FIELDS if f in (meta.get("fields_visible_on_page") or CORE_DETAIL_FIELDS)])
    platform = (merged.get("platform") or detail_data.get("platform") or "movemeon").strip().lower()
    # Prefer detail missing_fields when present
    if detail_data.get("missing_fields") is not None:
        merged["missing_fields"] = list(dict.fromkeys(
            list(detail_data.get("missing_fields") or [])
            + compute_missing_fields(
                merged, expected_fields=list(CARD_REQUIRED_FIELDS), platform=platform
            )
        ))
    else:
        merged["missing_fields"] = compute_missing_fields(
            merged, expected_fields=expected, platform=platform
        )

    merged["missing_fields"], warn_out = filter_category_from_missing_and_warnings(
        merged.get("missing_fields"),
        merged.get("extraction_warnings") or warnings,
        platform=platform,
    )
    if warn_out:
        merged["extraction_warnings"] = warn_out
    if not platform_exposes_field(platform, "platform_category"):
        if is_empty_value(merged.get("platform_category")) and (
            merged.get("platform_category_extraction_status") in (None, "", "MISSING")
        ):
            apply_not_exposed_category(
                merged,
                platform=platform,
                metadata=merged.setdefault("extraction_metadata", dict(meta or {})),
            )
    if warnings and "extraction_warnings" not in merged:
        merged["extraction_warnings"] = warnings
    return merged

# Appended BTG-specific helpers (imported into extraction.py)

import hashlib
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

MATERIAL_ICON_NAMES = {
    "savings", "place", "insert_invitation", "schedule",
    "location_on", "attach_money", "event", "timer",
    "work", "business", "person", "star", "info",
    "person_pin_circle", "date_range", "watch_later",
    # BTG remote chips often use the Material "home" ligature (renders as a house icon).
    "home_work_filled", "home_work", "home", "expand_more", "add",
}
_ICON_PREFIX_RE = re.compile(
    r"^(?:" + "|".join(re.escape(n) for n in sorted(MATERIAL_ICON_NAMES, key=len, reverse=True)) + r")\s+",
    re.IGNORECASE,
)
_WORK_MODE_PHRASE_RE = re.compile(
    r"\b(?:hybrid|remote|on[- ]?site|onsite|occasionally|occasional|travel|primarily)\b",
    re.IGNORECASE,
)


def strip_material_icons(text, join_with=" "):
    if not text:
        return ""
    cleaned = []
    for line in str(text).splitlines():
        s = line.strip()
        if not s or s.lower() in MATERIAL_ICON_NAMES:
            continue
        s = _ICON_PREFIX_RE.sub("", s).strip()
        if s and s.lower() not in MATERIAL_ICON_NAMES:
            cleaned.append(s)
    return join_with.join(cleaned)


def clean_location_geo(location):
    if not location:
        return ""
    t = strip_material_icons(location)
    # Icon-only leftovers (e.g. "home" alone) are not geography.
    if not t or t.lower() in MATERIAL_ICON_NAMES:
        return ""
    parts = [p.strip() for p in re.split(r"[,;]", t) if p.strip()]
    geo = [
        p for p in parts
        if not _WORK_MODE_PHRASE_RE.search(p) and p.lower() not in MATERIAL_ICON_NAMES
    ]
    if geo:
        return ", ".join(geo)
    # Do not fall back to icon/work-mode noise (previously returned "home" / "home Remote").
    if _WORK_MODE_PHRASE_RE.search(t) or t.lower() in MATERIAL_ICON_NAMES:
        return ""
    return t


def normalize_remote_type(value):
    if not value:
        return ""
    v = value.strip().lower().replace("_", " ")
    if "hybrid" in v:
        return "Hybrid"
    if re.search(r"\bon[- ]?site\b", v) or v == "onsite":
        return "Onsite"
    if "remote" in v:
        return "Remote"
    return ""


def infer_remote_type(*text_parts):
    block = "\n".join(p for p in text_parts if p)
    if not block:
        return ""
    if re.search(r"(?i)(?:^|\n)\s*hybrid\b", block) or re.search(r"(?i)\bhybrid\b\s*\(", block):
        return "Hybrid"
    if re.search(r"(?i)(?:^|\n)\s*remote\b", block):
        if re.search(r"(?i)primarily\s+remote", block) and re.search(
            r"(?i)(?:occasional|travel|on[- ]?site)", block
        ):
            return "Hybrid"
        return "Remote"
    if re.search(r"(?i)(?:^|\n)\s*on[- ]?site\b", block):
        return "Onsite"
    if re.search(r"(?i)\bhybrid\b", block):
        return "Hybrid"
    if re.search(r"(?i)primarily\s+remote", block):
        if re.search(r"(?i)(?:occasional|travel|on[- ]?site)", block):
            return "Hybrid"
        return "Remote"
    if re.search(r"(?i)\bremote\b", block) and not re.search(
        r"(?i)occasionally\s+on[- ]?site", block
    ):
        return "Remote"
    if re.search(r"(?i)\bon[- ]?site\b", block) and not re.search(
        r"(?i)occasional(?:ly)?\s+on[- ]?site", block
    ):
        return "Onsite"
    return ""


def parse_project_location_block(body_text):
    m = re.search(
        r"(?:^|\n)\s*Project Location\s*\n"
        r"([\s\S]+?)"
        r"(?=\n(?:Timeline|date_range|Budget|savings|Apply Now|Deadline|"
        r"Requirements?|Level of Support|Not for you)|\Z)",
        body_text or "",
        re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r"(?:^|\n)\s*person_pin_circle\s*\n"
            r"([\s\S]+?)"
            r"(?=\n(?:Timeline|date_range|Budget|savings|Apply Now|Deadline|"
            r"Project Location|Requirements?|Level of Support|Not for you)|\Z)",
            body_text or "",
            re.IGNORECASE,
        )
    if not m:
        return "", "", ""
    raw = m.group(1).strip()
    skip_labels = {"project location", "location", "timeline", "budget"}
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.lower() in MATERIAL_ICON_NAMES or s.lower() in skip_labels:
            continue
        s = _ICON_PREFIX_RE.sub("", s).strip()
        if s and s.lower() not in MATERIAL_ICON_NAMES and s.lower() not in skip_labels:
            lines.append(s)
    if not lines:
        return "", "", raw
    remote_type = ""
    geo = ""
    for line in lines:
        mode = normalize_remote_type(line)
        if mode and not remote_type:
            remote_type = mode
            continue
        if not geo and not _WORK_MODE_PHRASE_RE.search(line):
            geo = line
            continue
        if not geo and len(line) <= 80:
            geo = clean_location_geo(line) or line
    if not remote_type:
        remote_type = infer_remote_type("\n".join(lines))
    return geo, remote_type, raw


def canonicalize_btg_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    scheme = "https"
    netloc = parsed.netloc or "talent.businesstalentgroup.com"
    path = parsed.path.rstrip("/") or parsed.path
    q = parse_qs(parsed.query, keep_blank_values=False)
    cleaned = {k: v for k, v in q.items() if k.lower() not in TRACKING_QUERY_KEYS}
    query = urlencode(cleaned, doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def project_id_from_url(url: str):
    if not url:
        return None
    m = re.search(r"/projects?/([a-zA-Z0-9_-]+)", url)
    if m:
        pid = m.group(1)
        if pid.lower() not in ("projects", "project", "new", "search"):
            return pid
    return None


def resolve_btg_project_identity(
    *,
    url: str = "",
    data_attrs=None,
    embedded_id=None,
    title: str = "",
) -> dict:
    warnings = []
    canonical = canonicalize_btg_url(url) if url else ""
    pid = project_id_from_url(canonical or url)
    if pid:
        return {
            "project_id": pid,
            "source_url": canonical or f"{BTG_BASE}/projects/{pid}",
            "project_id_source": "canonical_url",
            "project_id_confidence": "HIGH",
            "extraction_metadata": {
                "project_id_source": "canonical_url",
                "project_id_confidence": "HIGH",
            },
            "extraction_warnings": warnings,
            "rejected": False,
        }

    data_attrs = data_attrs or {}
    for attr in ("data-id", "data-project-id", "data-opportunity-id", "id"):
        val = (data_attrs.get(attr) or "").strip()
        if val and re.match(r"^[a-zA-Z0-9_-]{4,}$", val) and val.lower() not in (
            "projects", "project", "card", "root",
        ):
            source_url = canonical or f"{BTG_BASE}/projects/{val}"
            return {
                "project_id": val,
                "source_url": source_url,
                "project_id_source": f"data_attribute:{attr}",
                "project_id_confidence": "MEDIUM",
                "extraction_metadata": {
                    "project_id_source": f"data_attribute:{attr}",
                    "project_id_confidence": "MEDIUM",
                },
                "extraction_warnings": warnings,
                "rejected": False,
            }

    if embedded_id and re.match(r"^[a-zA-Z0-9_-]{4,}$", str(embedded_id)):
        eid = str(embedded_id)
        return {
            "project_id": eid,
            "source_url": canonical or f"{BTG_BASE}/projects/{eid}",
            "project_id_source": "embedded_json",
            "project_id_confidence": "MEDIUM",
            "extraction_metadata": {
                "project_id_source": "embedded_json",
                "project_id_confidence": "MEDIUM",
            },
            "extraction_warnings": warnings,
            "rejected": False,
        }

    if canonical and "/projects/" in canonical:
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
        warnings.append("PROJECT_ID_FROM_URL_HASH")
        return {
            "project_id": f"urlhash_{digest}",
            "source_url": canonical,
            "project_id_source": "canonical_url_hash",
            "project_id_confidence": "LOW",
            "extraction_metadata": {
                "project_id_source": "canonical_url_hash",
                "project_id_confidence": "LOW",
            },
            "extraction_warnings": warnings,
            "rejected": False,
        }

    if title:
        warnings.append("TITLE_ONLY_IDENTITY_REJECTED")
    return {
        "project_id": None,
        "source_url": canonical or url or "",
        "project_id_source": None,
        "project_id_confidence": None,
        "extraction_metadata": {},
        "extraction_warnings": warnings + ["MISSING_STABLE_PROJECT_IDENTITY"],
        "rejected": True,
    }


def parse_btg_posted_at(time_str: str, scraped_at=None):
    scraped_at = scraped_at or datetime.now(timezone.utc)
    if scraped_at.tzinfo is None:
        scraped_at = scraped_at.replace(tzinfo=timezone.utc)
    s = normalize_visible_text(time_str)
    if not s or s.lower() == "unknown":
        return None, False
    rel_dt, _rel_est = parse_relative_posted_time(s, scraped_at=scraped_at)
    if rel_dt is not None:
        return rel_dt, True
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        try:
            dt = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), tzinfo=timezone.utc)
            return dt, True
        except ValueError:
            pass
    m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m2:
        try:
            dt = datetime(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)), tzinfo=timezone.utc)
            return dt, True
        except ValueError:
            pass
    return None, False


def apply_budget_fields(details: dict, candidate: str, *, source: str, confidence: str, project=None):
    ok, reason = validate_budget_candidate(candidate, project=project)
    if not ok:
        details.setdefault("extraction_warnings", []).append(f"BUDGET_CANDIDATE_REJECTED:{reason}")
        return details
    parsed = parse_budget(candidate)
    if reason == "ok_not_provided":
        details["budget_text"] = candidate.strip() if candidate else "Not provided"
        details["budget_source"] = source
        details["budget_confidence"] = confidence
        return details
    details.update({k: v for k, v in parsed.items() if v is not None})
    details["budget_source"] = source
    details["budget_confidence"] = confidence
    return details


def extract_btg_detail_fields_from_body(body_text: str, *, title: str = "", project=None) -> dict:
    project = project or {}
    warnings = list(project.get("extraction_warnings") or [])
    metadata = {
        "fields_visible_on_page": [],
        "fields_extracted": [],
        "fields_missing_but_visible": [],
        "fields_not_exposed": [],
    }
    details = {}
    body_text = (body_text or "").replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")

    m = re.search(
        r"date_range\s+[^\n]+\n([\s\S]+?)(?=\n(?:add\n)?Apply Now|\nDeadline:|\nNot for you)",
        body_text,
        re.IGNORECASE,
    )
    if m and validate_extracted_value(m.group(1), title=title):
        details["description"] = strip_material_icons(m.group(1).strip(), join_with="\n")
        metadata["fields_extracted"].append("description")
        metadata["fields_visible_on_page"].append("description")
    else:
        desc = extract_section_text(
            body_text,
            ["Description", "Overview", "Summary"],
            ["Project Details", "Budget", "Location", "Requirements", "Apply", "Timeline", "Deadline"],
        )
        if desc.get("value") and validate_extracted_value(desc["value"], title=title):
            details["description"] = strip_material_icons(desc["value"], join_with="\n")
            metadata["fields_extracted"].append("description")
            metadata["fields_visible_on_page"].append("description")
        elif re.search(r"(?i)Apply Now", body_text):
            metadata["fields_visible_on_page"].append("description")
            metadata["fields_missing_but_visible"].append("description")
            warnings.append("DESCRIPTION_CONTAINER_NOT_FOUND")
        else:
            metadata["fields_not_exposed"].append("description")

    if details.get("description"):
        details["description"] = re.sub(
            r"(?im)^\s*(?:add\s*)?Apply Now\s*$", "", details["description"]
        ).strip()

    _SEP = r"(?:[ \t]+|[ \t]*\n(?:[ \t]*\n)*[ \t]*)"
    patterns = {
        "start_date_text": rf"(?:Start Date|Starts)\s*:?{_SEP}(\d{{2}}/\d{{2}}/\d{{4}}[^\n]{{0,40}})",
        "timeline": rf"(?:Timeline){_SEP}([^\n]{{2,100}})",
        "level_of_support": rf"Level of Support{_SEP}([^\n]{{2,60}})",
        "industry": rf"(?:^|\n)(?:Industry|Desired Industry Background)\s*:?{_SEP}([^\n]{{2,100}})",
        "deadline": rf"Deadline:?{_SEP}([^\n]{{2,30}})",
    }
    for field, pattern in patterns.items():
        mapped_visible = {
            "start_date_text": "start_date_text",
            "timeline": "project_length",
            "level_of_support": "level_of_support",
            "industry": "industry",
            "deadline": "application_deadline",
        }[field]
        mm = re.search(pattern, body_text, re.IGNORECASE)
        if field == "timeline":
            # Timeline dates alone do not mean project_length is visible/required.
            if mm:
                val = (mm.group(1) if mm.lastindex else mm.group(0)).strip()
                details["raw_data"] = dict(details.get("raw_data") or {})
                details["raw_data"]["btg_timeline"] = val
                paren = re.search(r"\(([^)]*\b(?:month|week|day)s?\b[^)]*)\)", val, re.I)
                if paren:
                    details["project_length"] = paren.group(1).strip()
                    metadata["fields_visible_on_page"].append("project_length")
                    metadata["fields_extracted"].append("project_length")
                elif re.search(r"\b(?:month|week|day)s?\b", val, re.I) and not _is_non_duration_timeline(val):
                    details["project_length"] = val.strip()
                    metadata["fields_visible_on_page"].append("project_length")
                    metadata["fields_extracted"].append("project_length")
                else:
                    # Date-only timeline is informational, not a failed duration extract.
                    warnings.append("TIMELINE_NOT_A_DURATION")
            continue
        label_present = bool(
            mm
            or re.search(rf"(?i){re.escape(field.replace('_', ' '))}", body_text)
        )
        if label_present and mapped_visible not in metadata["fields_visible_on_page"]:
            metadata["fields_visible_on_page"].append(mapped_visible)
        if not mm:
            continue
        val = (mm.group(1) if mm.lastindex else mm.group(0)).strip()
        if field == "deadline":
            details["application_deadline"] = val
            metadata["fields_extracted"].append("application_deadline")
        elif field == "start_date_text":
            details["start_date_text"] = val
            details["source_start_date"] = parse_source_start_date(val)
            metadata["fields_extracted"].append("start_date_text")
        else:
            details[mapped_visible] = val
            metadata["fields_extracted"].append(mapped_visible)

    eng = re.search(r"(?:Full[- ]?time|Part[- ]?time|Fractional)", body_text, re.I)
    if eng:
        details["engagement_type"] = eng.group(0)
        metadata["fields_visible_on_page"].append("engagement_type")
        metadata["fields_extracted"].append("engagement_type")
    else:
        metadata["fields_not_exposed"].append("engagement_type")

    loc_geo, loc_mode, loc_block = parse_project_location_block(body_text)
    if loc_mode or loc_block:
        metadata["fields_visible_on_page"].append("remote_or_onsite")
    if loc_geo:
        metadata["fields_visible_on_page"].append("location")
        details["location"] = loc_geo
        metadata["fields_extracted"].append("location")
    elif loc_block and not loc_mode:
        # Location section present but geo not captured
        metadata["fields_visible_on_page"].append("location")
        metadata["fields_missing_but_visible"].append("location")
    remote = loc_mode or infer_remote_type(loc_block, details.get("description", ""), body_text)
    if remote:
        details["remote_or_onsite"] = remote
        details["location_preference"] = remote
        metadata["fields_extracted"].extend(["remote_or_onsite", "location_preference"])

    bud = re.search(rf"(?:Budget|savings){_SEP}(\$[^\n]{{2,80}})", body_text, re.I)
    if bud:
        metadata["fields_visible_on_page"].append("budget_text")
        apply_budget_fields(
            details, bud.group(1), source="detail_label", confidence="HIGH", project=project
        )
        if details.get("budget_text"):
            metadata["fields_extracted"].append("budget_text")
    elif re.search(r"(?i)\bBudget\b", body_text):
        metadata["fields_visible_on_page"].append("budget_text")
        metadata["fields_missing_but_visible"].append("budget_text")
        warnings.append("VISIBLE_FIELD_NOT_EXTRACTED:budget_text")
    else:
        metadata["fields_not_exposed"].append("budget_text")

    req = re.search(
        r"Requirements?\s*\n([\s\S]+?)(?=\n(?:Budget|Apply|Deadline|Not for you|\Z))",
        body_text,
        re.I,
    )
    if req:
        lines = [strip_material_icons(l.strip()) for l in req.group(1).splitlines() if l.strip()]
        lines = [l for l in lines if l]
        if lines:
            details.setdefault("raw_data", {})
            details["raw_data"]["btg_requirements"] = lines
            metadata["fields_extracted"].append("btg_requirements")
            metadata["fields_visible_on_page"].append("btg_requirements")

    # BTG does not expose category on the website — never treat as missing/failed.
    platform = (project.get("platform") or "btg").strip().lower() or "btg"
    cat = extract_category_from_body_text(body_text)
    if cat.get("platform_category") and platform_exposes_field(platform, "platform_category"):
        for k, v in cat.items():
            details[k] = v
        metadata["fields_extracted"].append("platform_category")
        metadata["fields_visible_on_page"].append("platform_category")
    else:
        apply_not_exposed_category(details, platform=platform, metadata=metadata)

    cp = extract_value_by_label(body_text, ["Contracting Process", "Contracting"])
    if cp.get("value"):
        details["contracting_process"] = cp["value"]
        metadata["fields_extracted"].append("contracting_process")
        metadata["fields_visible_on_page"].append("contracting_process")
    else:
        metadata["fields_not_exposed"].append("contracting_process")

    meaningful = bool(
        details.get("description")
        or details.get("budget_text")
        or details.get("location")
        or details.get("remote_or_onsite")
        or details.get("level_of_support")
        or details.get("industry")
        or (details.get("raw_data") or {}).get("btg_requirements")
    )
    status = calculate_detail_extraction_status(
        attempted=True,
        page_ok=True,
        fields_visible=metadata["fields_visible_on_page"],
        fields_extracted=metadata["fields_extracted"],
        fields_missing_but_visible=metadata["fields_missing_but_visible"],
        meaningful=meaningful,
        platform=platform,
    )
    details["detail_extraction_status"] = status
    details["extraction_warnings"] = warnings
    details["extraction_metadata"] = metadata
    details["missing_fields"] = list(metadata["fields_missing_but_visible"])
    details["missing_fields"], details["extraction_warnings"] = filter_category_from_missing_and_warnings(
        details["missing_fields"], details["extraction_warnings"], platform=platform
    )
    return details

# ---------------------------------------------------------------------------
# MoveMeOn identity + card/detail helpers
# ---------------------------------------------------------------------------

MOVEMEON_BASE = "https://portal.movemeon.com"
MOVEMEON_TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "source",
}

_ENGAGEMENT_TOKENS = (
    "permanent", "contract", "full-time", "full time", "part-time",
    "part time", "temporary", "fixed-term", "fixed term", "freelance",
    "interim", "fractional",
)

_DURATION_HINTS = (
    "month", "week", "year", "day engagement", "initial", "up to",
)


def canonicalize_movemeon_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(str(url).strip())
    query = parse_qs(parsed.query, keep_blank_values=False)
    cleaned = {
        k: v for k, v in query.items()
        if k.lower() not in MOVEMEON_TRACKING_QUERY_KEYS
    }
    new_query = urlencode(cleaned, doseq=True)
    path = parsed.path.rstrip("/") or parsed.path
    return urlunparse((parsed.scheme or "https", parsed.netloc or "portal.movemeon.com", path, "", new_query, ""))


def project_id_from_movemeon_url(url: str):
    canonical = canonicalize_movemeon_url(url)
    if not canonical:
        return None, "missing_url"
    path = urlparse(canonical).path.rstrip("/")
    parts = [p for p in path.split("/") if p]
    if "jobs" in parts:
        idx = parts.index("jobs")
        if idx + 1 < len(parts):
            slug = parts[idx + 1]
            if slug and slug.lower() not in ("new", "search", "curated"):
                return slug, "url_path"
    if parts:
        return parts[-1], "url_tail"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return digest, "url_hash"


def resolve_movemeon_project_identity(url: str = "", data_attrs: Optional[dict] = None, embedded_id: Optional[str] = None):
    """
    Identity priority:
    1) canonical job ID/slug from MoveMeOn URL
    2) verified data attribute
    3) verified embedded JSON id
    4) canonical URL SHA-256 hash (last resort)
    """
    meta = {"identity_source": None, "identity_confidence": "HIGH", "canonical_url": canonicalize_movemeon_url(url)}
    pid, source = project_id_from_movemeon_url(url)
    if pid and source in ("url_path", "url_tail"):
        meta["identity_source"] = source
        return pid, meta["canonical_url"], meta
    if data_attrs:
        for key in ("data-job-id", "data-id", "data-project-id", "id"):
            val = (data_attrs.get(key) or "").strip()
            if val and val.lower() not in ("null", "undefined"):
                meta["identity_source"] = f"data_attr:{key}"
                meta["identity_confidence"] = "MEDIUM"
                return val, meta["canonical_url"] or canonicalize_movemeon_url(url), meta
    if embedded_id and str(embedded_id).strip():
        meta["identity_source"] = "embedded_json"
        meta["identity_confidence"] = "MEDIUM"
        return str(embedded_id).strip(), meta["canonical_url"] or canonicalize_movemeon_url(url), meta
    if pid:
        meta["identity_source"] = "url_hash"
        meta["identity_confidence"] = "LOW"
        return pid, meta["canonical_url"], meta
    digest = hashlib.sha256((url or "unknown").encode("utf-8")).hexdigest()[:32]
    meta["identity_source"] = "url_hash"
    meta["identity_confidence"] = "LOW"
    return digest, meta["canonical_url"] or url, meta


def classify_engagement_or_duration(text: str) -> dict:
    """Map MoveMeOn card metadata to engagement_type vs duration_text."""
    value = (text or "").strip()
    if not value:
        return {}
    lower = value.lower()
    out = {}
    if any(tok in lower for tok in _ENGAGEMENT_TOKENS):
        out["engagement_type"] = value
        # Only also set project_type when clearly employment-like
        if any(tok in lower for tok in ("permanent", "full-time", "part-time", "temporary", "contract")):
            out["project_type"] = value
    if any(h in lower for h in _DURATION_HINTS) and not any(tok in lower for tok in _ENGAGEMENT_TOKENS):
        out["duration_text"] = value
        out["project_length"] = value
    elif any(h in lower for h in _DURATION_HINTS) and any(tok in lower for tok in _ENGAGEMENT_TOKENS):
        # Prefer engagement when both present in same token; keep duration empty
        pass
    return out


def normalize_movemeon_card_project(raw: dict) -> dict:
    """Normalize legacy/raw card dict into shared projects field names."""
    raw = dict(raw or {})
    title = (raw.get("title") or "").strip()
    company = (raw.get("company") or raw.get("movemeon_company") or "").strip()
    # Strip legacy "Title (Company)" suffix if present
    if company and title.endswith(f"({company})"):
        title = title[: -(len(company) + 2)].rstrip()
    url = raw.get("source_url") or raw.get("url") or ""
    pid, canonical, id_meta = resolve_movemeon_project_identity(url, embedded_id=raw.get("id") or raw.get("project_id"))
    engagement_src = raw.get("engagement_type") or raw.get("duration") or raw.get("project_type") or ""
    mapped = classify_engagement_or_duration(str(engagement_src))
    # Hard reject invented placeholders
    time_posted = raw.get("time_posted_text") or raw.get("time_posted")
    if isinstance(time_posted, str) and time_posted.strip().lower() in ("recently", "new", "unknown"):
        time_posted = None
    status = raw.get("status")
    if isinstance(status, str) and status.strip().lower() in ("new", "recently", "unknown"):
        status = None

    raw_data = dict(raw.get("raw_data") or {})
    if company:
        raw_data["movemeon_company"] = company
    if raw.get("card_text"):
        raw_data["movemeon_card_text"] = raw.get("card_text")
    if raw.get("metadata_items"):
        raw_data["movemeon_original_metadata"] = list(raw.get("metadata_items") or [])
    raw_data["identity"] = id_meta
    # Preserve any pre-seeded raw_data keys (e.g. language)
    for key, value in (raw.get("raw_data") or {}).items():
        if key not in raw_data and value:
            raw_data[key] = value

    location = raw.get("location") or None
    if isinstance(location, str):
        location = re.sub(r"\s*\+?\d+\s*more\s*$", "", location, flags=re.I)
        location = re.sub(r"\n\+?\d+\s*more\s*$", "", location, flags=re.I).strip() or None

    project = {
        "platform": "movemeon",
        "project_id": pid,
        "id": pid,
        "title": title,
        "source_url": canonical or url,
        "url": canonical or url,
        "short_description": raw.get("short_description") or raw.get("description") or None,
        "description": raw.get("description") if raw.get("detail_description") else None,
        "location": location,
        "budget_text": raw.get("budget_text") or raw.get("budget") or None,
        "engagement_type": mapped.get("engagement_type") or raw.get("engagement_type"),
        "project_type": mapped.get("project_type") or raw.get("project_type"),
        "duration_text": mapped.get("duration_text") or raw.get("duration_text"),
        "project_length": mapped.get("project_length") or raw.get("project_length"),
        "time_posted_text": time_posted,
        "source_posted_at": raw.get("source_posted_at"),
        "source_posted_at_is_estimated": bool(raw.get("source_posted_at_is_estimated") or False),
        "status": status,
        "remote_or_onsite": raw.get("remote_or_onsite"),
        "location_preference": raw.get("location_preference") or raw.get("remote_or_onsite"),
        "raw_data": raw_data,
        "extraction_metadata": dict(raw.get("extraction_metadata") or {}),
        "extraction_warnings": list(raw.get("extraction_warnings") or []),
    }
    if project.get("budget_text"):
        apply_budget_fields(
            project,
            str(project["budget_text"]),
            source="card_metadata",
            confidence="MEDIUM",
            project=project,
        )
    apply_not_exposed_category(project, platform="movemeon", metadata=project.setdefault("extraction_metadata", {}))
    project["card_extraction_status"] = calculate_card_extraction_status(project, platform="movemeon")
    project["detail_extraction_status"] = raw.get("detail_extraction_status") or "NOT_ATTEMPTED"
    project["missing_fields"] = compute_missing_fields(project, platform="movemeon")
    return project


def _parse_movemeon_label_value_blocks(body_text: str) -> dict[str, str]:
    """
    MoveMeOn Details panel uses consecutive lines:
        Salary
        £60K - £90K
        Location
        United Kingdom
    Also accepts 'Label: value' on one line.
    """
    labels = {
        "salary": "salary",
        "compensation": "salary",
        "day rate": "salary",
        "hourly rate": "salary",
        "location": "location",
        "industry": "industry",
        "function": "function",
        "language": "language",
        "flexibility": "flexibility",
        "job type": "engagement_type",
        "employment type": "engagement_type",
        "contract type": "engagement_type",
        "duration": "duration",
        "project length": "duration",
        "start date": "start_date",
        "deadline": "deadline",
        "application deadline": "deadline",
        "posted": "posted",
        "date posted": "posted",
        "total years of experience": "years_experience",
        "years of experience": "years_experience",
        "experience": "years_experience",
        "company": "company",
        "client": "company",
        "work mode": "flexibility",
        "working arrangement": "flexibility",
    }
    found: dict[str, str] = {}
    lines = [ln.strip() for ln in (body_text or "").splitlines()]
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line:
            i += 1
            continue
        # Same-line Label: value
        m = re.match(r"^([^:]{2,40})\s*:\s*(.+)$", line)
        if m:
            key = labels.get(m.group(1).strip().lower())
            val = normalize_visible_text(m.group(2))
            if key and val and key not in found:
                found[key] = val
            i += 1
            continue
        key = labels.get(line.lower())
        if key and key not in found:
            # Next non-empty line is the value (skip blank lines)
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                candidate = lines[j].strip()
                # Don't treat the next label as a value
                cand_label = candidate.lower()
                same_line = re.match(r"^([^:]{2,40})\s*:\s*(.*)$", candidate)
                if same_line and same_line.group(1).strip().lower() in labels:
                    # Next line is another Label: value — current label had no value
                    i += 1
                    continue
                if cand_label in labels or not candidate or re.match(
                    r"(?i)^(why apply|must-have|about company|job description|details|"
                    r"screening questions|our application|compensation breakdown|save job|apply|"
                    r"skills|requirements)$",
                    candidate,
                ):
                    i += 1
                    continue
                if candidate.lower() in labels or re.match(
                    r"(?i)^(why apply|must-have|about company|job description|details|"
                    r"screening questions|our application|compensation breakdown|save job|apply)$",
                    candidate,
                ):
                    i += 1
                    continue
                # Industry / function may span until next known label
                if key in ("industry", "function"):
                    parts = [candidate]
                    k = j + 1
                    while k < len(lines):
                        nxt = lines[k].strip()
                        if not nxt:
                            k += 1
                            continue
                        if nxt.lower() in labels or re.match(
                            r"(?i)^(why apply|must-have|about company|job description|details|"
                            r"screening|compensation breakdown|show less|show more|\+\d+\s*more)",
                            nxt,
                        ):
                            break
                        if nxt.lower() in ("show less", "show more") or re.match(r"^\+\d+\s*more$", nxt, re.I):
                            break
                        parts.append(nxt)
                        k += 1
                    found[key] = normalize_visible_text(", ".join(parts))
                else:
                    found[key] = normalize_visible_text(candidate)
        i += 1
    return found


def _parse_movemeon_header_chips(body_text: str, *, title: str = "") -> dict[str, str]:
    """
    Parse the chip row under the title:
      Exceptional Leadership Technology
      United Kingdom
      Permanent
      8+ Years Experience
      £60K - £90K
      English
      Remote
    """
    out: dict[str, str] = {}
    lines = [ln.strip() for ln in (body_text or "").splitlines() if ln.strip()]
    # Restrict to the header region before Why Apply / Job Description
    stop_idx = len(lines)
    for idx, ln in enumerate(lines):
        if ln.lower() in (
            "why apply", "must-have requirements", "about company",
            "job description", "details", "save job",
        ):
            stop_idx = idx
            break
    header = lines[:stop_idx]
    # Drop nav noise
    skip = {
        "hide job", "discover jobs", "apply", "save job", "jobs", "curated jobs",
        "saved jobs", "applications", "salary benchmark", "why movemeon?",
        "job preferences", "career coaching", "the movemeon group", "logo",
    }
    chips = []
    for ln in header:
        low = ln.lower()
        if low in skip or low == (title or "").strip().lower():
            continue
        if len(ln) > 160:
            continue
        chips.append(ln)

    language_tokens = {
        "english", "french", "german", "spanish", "dutch", "italian",
        "portuguese", "mandarin", "arabic", "japanese", "korean", "hindi",
    }
    engagement_tokens = (
        "permanent", "contract", "full-time", "full time", "part-time",
        "part time", "temporary", "interim", "freelance", "fractional",
    )

    for chip in chips:
        low = chip.lower()
        if re.search(r"[£$€]", chip) or re.search(r"(?i)\b\d+\s*[kK]\b.*[-–—].*\d+", chip):
            out.setdefault("salary", chip)
            continue
        if any(tok in low for tok in engagement_tokens) and "experience" not in low:
            out.setdefault("engagement_type", chip)
            continue
        if "experience" in low or re.search(r"(?i)\b\d+\+?\s*years?\b", low):
            out.setdefault("years_experience", chip)
            continue
        if low in language_tokens:
            out.setdefault("language", chip)
            continue
        if low in ("remote", "hybrid", "on-site", "onsite", "in office", "in-office") or "remote" in low:
            out.setdefault("flexibility", chip)
            continue
        # Likely location (geo) before company — prefer geo patterns
        if any(
            h in low
            for h in (
                "united kingdom", "uk", "london", "singapore", "germany", "france",
                "netherlands", "europe", "united states", "usa", "uae", "dubai",
                "australia", "ireland", "belgium", "switzerland", "hong kong",
            )
        ) or ("," in chip and len(chip) < 80):
            out.setdefault("location", chip)
            continue

    # Company: first remaining substantial chip that isn't already used
    used = {normalize_visible_text(v).lower() for v in out.values()}
    for chip in chips:
        low = chip.lower()
        if normalize_visible_text(chip).lower() in used:
            continue
        if low in language_tokens or low in skip:
            continue
        if any(tok in low for tok in engagement_tokens):
            continue
        if "experience" in low or re.search(r"[£$€]", chip):
            continue
        if low in ("remote", "hybrid", "on-site", "onsite"):
            continue
        if len(chip.split()) >= 1 and len(chip) >= 3:
            # Prefer multi-word company names
            if "location" in out and chip == out["location"]:
                continue
            out.setdefault("company", chip)
            break

    # Tagline / short description often sits after chips and before Save Job
    for idx, ln in enumerate(header):
        if ln.lower() == "apply" and idx > 0:
            prev = header[idx - 1]
            if len(prev) > 40 and prev.lower() not in used:
                out.setdefault("tagline", prev)
    if "tagline" not in out:
        for ln in header:
            if "|" in ln and len(ln) > 40:
                out["tagline"] = ln
                break
    return out


def extract_movemeon_detail_fields_from_body(body_text: str, *, title: str = "", project=None) -> dict:
    """Extract MoveMeOn detail fields from visible body text."""
    project = project or {}
    details = {"raw_data": dict(project.get("raw_data") or {})}
    warnings = []
    metadata = {
        "fields_visible_on_page": [],
        "fields_extracted": [],
        "fields_missing_but_visible": [],
        "fields_not_exposed": [],
        "platform_capabilities": {"category_exposed": False},
        "movemeon_sources": {},
    }
    body_text = (body_text or "").replace("\xa0", " ")

    def _mark_visible(field: str) -> None:
        if field not in metadata["fields_visible_on_page"]:
            metadata["fields_visible_on_page"].append(field)

    def _mark_extracted(field: str) -> None:
        if field not in metadata["fields_extracted"]:
            metadata["fields_extracted"].append(field)

    # Prefer Job Description section for description; keep Why Apply separate
    job_desc = extract_section_text(
        body_text,
        ["Job Description", "The Role", "About the Role", "About this Role", "Overview"],
        [
            "Details", "Must-Have", "Requirements", "About Company", "Why Apply",
            "Screening Questions", "Our Application", "Compensation breakdown",
            "Skills", "Benefits", "Apply",
        ],
    )
    why_apply = extract_section_text(
        body_text,
        ["Why Apply"],
        ["Must-Have", "Requirements", "About Company", "Job Description", "Details"],
    )
    about_company = extract_section_text(
        body_text,
        ["About Company"],
        ["Job Description", "Details", "Why Apply", "Must-Have"],
    )
    must_have = extract_section_text(
        body_text,
        ["Must-Have Requirements", "Must Have Requirements", "Requirements"],
        ["About Company", "Job Description", "Details", "Why Apply", "Nice to Have"],
    )
    screening = extract_section_text(
        body_text,
        ["Screening Questions"],
        ["Our Application Process", "Why Apply Through", "Save Job"],
    )
    compensation_notes = extract_section_text(
        body_text,
        ["Compensation breakdown and benefits", "Compensation breakdown"],
        ["Screening Questions", "Our Application Process", "Details"],
    )

    if job_desc.get("value") and len(str(job_desc["value"])) > 30:
        details["description"] = job_desc["value"]
        _mark_visible("description")
        _mark_extracted("description")
        metadata["movemeon_sources"]["description"] = "Job Description"
    elif why_apply.get("value") and len(str(why_apply["value"])) > 30:
        details["description"] = why_apply["value"]
        _mark_visible("description")
        _mark_extracted("description")
        metadata["movemeon_sources"]["description"] = "Why Apply"
        warnings.append("DESCRIPTION_FROM_WHY_APPLY_FALLBACK")
    elif re.search(r"(?i)\b(job description|description|the role|overview|why apply)\b", body_text):
        _mark_visible("description")
        metadata["fields_missing_but_visible"].append("description")
        warnings.append("VISIBLE_FIELD_NOT_EXTRACTED:description")

    if why_apply.get("value"):
        details["raw_data"]["movemeon_why_apply"] = why_apply["value"]
    if about_company.get("value"):
        details["raw_data"]["movemeon_about_company"] = about_company["value"]
    if must_have.get("value"):
        # Split bullets into requirements list — store under raw_data, not skills
        req_lines = [
            re.sub(r"^[•\-\*]\s*", "", ln).strip()
            for ln in str(must_have["value"]).splitlines()
            if ln.strip() and ln.strip() not in ("•", "-", "*")
        ]
        req_lines = [ln for ln in req_lines if ln]
        details["raw_data"]["movemeon_must_have_requirements"] = req_lines or must_have["value"]
        # Only promote short skill-like tokens into skills; long requirement prose stays in raw_data
        short_skills = [ln for ln in req_lines if 1 < len(ln) <= 60 and "," not in ln]
        if short_skills and not details.get("skills"):
            details["skills"] = short_skills[:20]
            _mark_visible("skills")
            _mark_extracted("skills")
    if screening.get("value"):
        q_lines = [
            re.sub(r"^[•\-\*]\s*", "", ln).strip()
            for ln in str(screening["value"]).splitlines()
            if ln.strip() and ln.strip() not in ("•", "-", "*")
        ]
        details["raw_data"]["movemeon_screening_questions"] = [q for q in q_lines if q]
    if compensation_notes.get("value"):
        details["raw_data"]["movemeon_compensation_notes"] = compensation_notes["value"]

    header = _parse_movemeon_header_chips(body_text, title=title)
    details_block = _parse_movemeon_label_value_blocks(body_text)

    # Merge with Details panel taking precedence over header chips
    merged_fields = dict(header)
    merged_fields.update({k: v for k, v in details_block.items() if v})

    if merged_fields.get("company"):
        details["raw_data"]["movemeon_company"] = merged_fields["company"]
        _mark_extracted("movemeon_company")
        metadata["movemeon_sources"]["company"] = "header_or_details"
    if merged_fields.get("language"):
        details["raw_data"]["movemeon_language"] = merged_fields["language"]
    if merged_fields.get("years_experience"):
        details["raw_data"]["movemeon_years_experience"] = merged_fields["years_experience"]
    if merged_fields.get("function"):
        details["raw_data"]["movemeon_function"] = merged_fields["function"]
        # Function is the closest MoveMeOn analogue to a category/workstream
        details["workstream"] = merged_fields["function"]
        _mark_visible("workstream")
        _mark_extracted("workstream")
    if merged_fields.get("tagline") and not details.get("short_description"):
        details["short_description"] = merged_fields["tagline"]

    if merged_fields.get("location"):
        _mark_visible("location")
        details["location"] = merged_fields["location"]
        _mark_extracted("location")
        metadata["movemeon_sources"]["location"] = "details_or_header"
    elif re.search(r"(?im)^\s*Location\s*$", body_text) or re.search(r"(?im)^\s*Location\s*:", body_text):
        _mark_visible("location")
        metadata["fields_missing_but_visible"].append("location")

    salary_val = merged_fields.get("salary")
    if salary_val or re.search(r"(?im)^\s*Salary\s*$", body_text) or re.search(r"[£$€]\s*\d", body_text):
        _mark_visible("budget_text")
    if salary_val:
        apply_budget_fields(details, salary_val, source="detail_label", confidence="HIGH", project=project)
        if not details.get("budget_text"):
            details["budget_text"] = salary_val
        if details.get("budget_text"):
            _mark_extracted("budget_text")
            metadata["movemeon_sources"]["budget_text"] = "details_or_header"
        else:
            metadata["fields_missing_but_visible"].append("budget_text")
    elif "budget_text" in metadata["fields_visible_on_page"]:
        # Visible Salary heading without extracted value
        if "budget_text" not in metadata["fields_missing_but_visible"]:
            metadata["fields_missing_but_visible"].append("budget_text")

    engagement_val = merged_fields.get("engagement_type")
    # Header chip Permanent/Contract often only in chips, not Details panel
    if not engagement_val:
        # Search chips / body for engagement tokens as dedicated values
        for tok in ("Permanent", "Contract", "Full-time", "Part-time", "Temporary", "Freelance", "Interim"):
            if re.search(rf"(?im)^\s*{re.escape(tok)}\s*$", body_text):
                engagement_val = tok
                break
    if engagement_val:
        _mark_visible("engagement_type")
        # Long prose from elsewhere should not overwrite a clean chip like Permanent
        if len(engagement_val) > 40:
            details["raw_data"]["movemeon_engagement_notes"] = engagement_val
            for tok in (
                "Permanent", "Contract", "Full-time", "Part-time", "Temporary",
                "Freelance", "Interim", "Fractional",
            ):
                if re.search(rf"(?i)\b{re.escape(tok)}\b", engagement_val):
                    engagement_val = tok
                    break
        mapped = classify_engagement_or_duration(engagement_val)
        details.update(mapped)
        if not details.get("engagement_type"):
            details["engagement_type"] = engagement_val
        _mark_extracted("engagement_type")
    elif re.search(r"(?im)^\s*(Job type|Employment type)\s*$", body_text):
        _mark_visible("engagement_type")
        metadata["fields_missing_but_visible"].append("engagement_type")

    if merged_fields.get("duration"):
        mapped = classify_engagement_or_duration(merged_fields["duration"])
        if mapped.get("duration_text") or mapped.get("project_length"):
            details.update(mapped)
            _mark_visible("duration_text")
            _mark_extracted("duration_text")
        elif not details.get("engagement_type"):
            details.update(mapped)

    if merged_fields.get("start_date"):
        details["start_date_text"] = merged_fields["start_date"]
        _mark_visible("start_date_text")
        _mark_extracted("start_date_text")

    if merged_fields.get("deadline"):
        details["application_deadline"] = merged_fields["deadline"]
        _mark_visible("application_deadline")
        _mark_extracted("application_deadline")

    if merged_fields.get("posted"):
        if str(merged_fields["posted"]).strip().lower() not in ("recently", "new", "unknown"):
            details["time_posted_text"] = merged_fields["posted"]
            parsed, estimated = parse_relative_posted_time(merged_fields["posted"])
            if parsed is not None:
                details["source_posted_at"] = parsed.isoformat()
                details["source_posted_at_is_estimated"] = estimated
            _mark_visible("time_posted_text")
            _mark_extracted("time_posted_text")

    flexibility = merged_fields.get("flexibility")
    if flexibility:
        remote = normalize_remote_type(flexibility) or flexibility
        details["remote_or_onsite"] = remote
        details["location_preference"] = remote
        _mark_visible("remote_or_onsite")
        _mark_extracted("remote_or_onsite")
        _mark_extracted("location_preference")

    if merged_fields.get("industry"):
        details["industry"] = merged_fields["industry"]
        _mark_visible("industry")
        _mark_extracted("industry")

    # Legacy same-line label extraction still fills any remaining gaps
    for label, field in (
        ("Location", "location"),
        ("Industry", "industry"),
        ("Salary", "budget_text"),
        ("Compensation", "budget_text"),
        ("Job type", "engagement_type"),
        ("Flexibility", "remote_or_onsite"),
    ):
        if details.get(field) or (field == "budget_text" and details.get("budget_text")):
            continue
        if field == "remote_or_onsite" and details.get("remote_or_onsite"):
            continue
        hit = extract_value_by_label(body_text, [label])
        if not hit.get("value"):
            continue
        _mark_visible(field)
        val = hit["value"]
        if field == "budget_text":
            apply_budget_fields(details, val, source="detail_label", confidence="HIGH", project=project)
            if details.get("budget_text"):
                _mark_extracted("budget_text")
        elif field == "engagement_type":
            mapped = classify_engagement_or_duration(val)
            details.update(mapped)
            _mark_extracted("engagement_type")
        elif field == "remote_or_onsite":
            details["remote_or_onsite"] = normalize_remote_type(val) or val
            details["location_preference"] = details["remote_or_onsite"]
            _mark_extracted("remote_or_onsite")
        else:
            details[field] = val
            _mark_extracted(field)

    skills = extract_list_values(body_text, ["Skills", "Key Skills", "Required Skills"])
    if skills.get("value") and not details.get("skills"):
        details["skills"] = skills["value"]
        _mark_visible("skills")
        _mark_extracted("skills")

    if not details.get("remote_or_onsite"):
        inferred = infer_remote_type(body_text, title)
        if inferred:
            details["remote_or_onsite"] = inferred
            details["location_preference"] = inferred

    # Function is exposed by MoveMeOn — do not mark workstream as not-exposed when found
    apply_not_exposed_category(details, platform="movemeon", metadata=metadata)
    for optional in (
        "platform_category", "contracting_process", "level_of_support",
        "estimated_hours", "weekly_commitment", "expertise", "deliverables",
    ):
        if optional not in metadata["fields_extracted"]:
            if optional not in metadata["fields_not_exposed"]:
                metadata["fields_not_exposed"].append(optional)
    if "workstream" not in metadata["fields_extracted"]:
        # MoveMeOn exposes Function when Details panel present; otherwise not exposed
        if re.search(r"(?im)^\s*Function\s*$", body_text):
            _mark_visible("workstream")
            if "workstream" not in metadata["fields_missing_but_visible"]:
                metadata["fields_missing_but_visible"].append("workstream")
        else:
            metadata["fields_not_exposed"].append("workstream")

    meaningful = bool(
        details.get("description")
        or details.get("budget_text")
        or details.get("location")
        or details.get("remote_or_onsite")
        or details.get("engagement_type")
        or details.get("industry")
        or details.get("skills")
        or (details.get("raw_data") or {}).get("movemeon_company")
    )
    # Deduplicate metadata lists
    for key in ("fields_visible_on_page", "fields_extracted", "fields_missing_but_visible", "fields_not_exposed"):
        seen = set()
        deduped = []
        for item in metadata[key]:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        metadata[key] = deduped

    extracted_set = set(metadata["fields_extracted"])
    metadata["fields_missing_but_visible"] = [
        f for f in metadata["fields_missing_but_visible"] if f not in extracted_set
    ]

    status = calculate_detail_extraction_status(
        attempted=True,
        page_ok=True,
        fields_visible=metadata["fields_visible_on_page"],
        fields_extracted=metadata["fields_extracted"],
        fields_missing_but_visible=metadata["fields_missing_but_visible"],
        meaningful=meaningful,
        platform="movemeon",
    )
    details["detail_extraction_status"] = status
    details["extraction_warnings"] = warnings
    details["extraction_metadata"] = metadata
    details["missing_fields"] = list(metadata["fields_missing_but_visible"])
    details["missing_fields"], details["extraction_warnings"] = filter_category_from_missing_and_warnings(
        details["missing_fields"], details["extraction_warnings"], platform="movemeon"
    )
    return details

