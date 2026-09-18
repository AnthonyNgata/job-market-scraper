"""
File reading, writing, and exports.

All writes go through `_atomic_write` / a temp-then-replace pattern: a run that
dies halfway (or a GitHub Actions job that hits its timeout) leaves the previous
good file intact rather than a truncated one that the next run would happily
read as valid input.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from config import settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def ensure_directories() -> None:
    """Create the data directories if they don't exist yet (idempotent)."""
    for directory in (settings.RAW_DIR, settings.CLEANED_DIR):
        directory.mkdir(parents=True, exist_ok=True)
        logger.debug("Ensured directory %s", directory)


def timestamp() -> str:
    """UTC stamp used to version raw snapshots: 20260918T140355Z."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _atomic_write(path: Path, write_fn) -> Path:
    """
    Write via a sibling temp file, then atomically replace the target.

    `write_fn` receives the temp Path and is responsible for the actual write.
    The temp file lives in the same directory so `os.replace` stays atomic
    (it is not across filesystems).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")

    try:
        write_fn(temp_path)
        os.replace(temp_path, path)
        return path
    except Exception:
        # Clean up the partial file so it can't be mistaken for real output.
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                logger.warning("Could not remove temp file %s", temp_path)
        raise


# --------------------------------------------------------------------------- #
# Raw archive
# --------------------------------------------------------------------------- #

def save_raw(listings: list[dict[str, Any]], label: str = "jobs") -> Path | None:
    """
    Archive the unmodified scraper output before any cleaning touches it.

    This is the single most useful habit in a scraping pipeline: when the
    cleaning logic turns out to be wrong three weeks from now, the raw payloads
    let you re-derive the cleaned set without re-scraping (which you often
    can't, because the listings are gone).
    """
    path = settings.RAW_DIR / f"{label}_{timestamp()}.json"

    payload = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(listings),
        "source": settings.BASE_URL,
        "listings": listings,
    }

    try:
        _atomic_write(
            path,
            lambda p: p.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            ),
        )
    except OSError as exc:
        logger.error("Failed to write raw archive %s: %s", path, exc)
        return None

    logger.info("Raw archive written: %s (%d record(s))", path, len(listings))
    return path


def load_raw(path: str | Path) -> list[dict[str, Any]]:
    """Read a raw archive back, e.g. to re-run cleaning without re-scraping."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.error("Raw archive not found: %s", path)
        return []
    except json.JSONDecodeError as exc:
        logger.error("Raw archive %s is not valid JSON: %s", path, exc)
        return []
    except OSError as exc:
        logger.error("Could not read %s: %s", path, exc)
        return []

    return payload.get("listings", [])


# --------------------------------------------------------------------------- #
# Cleaned exports
# --------------------------------------------------------------------------- #

# Columns held back from the CSV:
#   skills / skill_tags_raw — list columns. CSV has no array type and a repr'd
#     Python list ("['SQL', 'Python']") is hostile to parse back, so the CSV
#     carries `skills_joined` instead. Both survive intact in the JSON export.
#   description — long free text that makes the CSV unreadable in a spreadsheet.
CSV_EXCLUDE = ["skills", "skill_tags_raw", "description"]

CSV_COLUMN_ORDER = [
    "job_id", "job_title", "company", "location", "is_remote",
    "salary_raw", "salary_min", "salary_max",
    "skills_joined", "skill_count", "has_skills",
    "posted_raw", "url", "source_url", "scraped_at",
]


def export_csv(df: pd.DataFrame, filename: str = settings.CSV_FILENAME) -> Path | None:
    """
    Write the cleaned dataset to CSV.

    utf-8-sig encoding: the BOM makes Excel on Windows read accented company
    names correctly instead of mojibake, and every other tool ignores it.
    """
    path = settings.CLEANED_DIR / filename
    export = df.drop(columns=[c for c in CSV_EXCLUDE if c in df.columns], errors="ignore")

    # Order known columns first, then append anything unexpected.
    ordered = [c for c in CSV_COLUMN_ORDER if c in export.columns]
    ordered += [c for c in export.columns if c not in ordered]
    export = export[ordered]

    try:
        _atomic_write(
            path,
            lambda p: export.to_csv(p, index=False, encoding="utf-8-sig"),
        )
    except (OSError, PermissionError) as exc:
        # PermissionError on Windows usually means the CSV is open in Excel.
        logger.error("Failed to write CSV %s: %s", path, exc)
        return None

    logger.info("CSV written: %s (%d row(s), %d column(s))", path, len(export), len(export.columns))
    return path


def export_json(df: pd.DataFrame, filename: str = settings.JSON_FILENAME) -> Path | None:
    """
    Write the cleaned dataset to JSON.

    Uses records orientation (a list of objects) — the shape almost every API
    and JS consumer expects — and keeps `skills` as a real nested array, which
    is the one thing JSON does better than the CSV export.
    """
    path = settings.CLEANED_DIR / filename
    export = df.copy()

    # NaN is not valid JSON; convert to null explicitly rather than relying on
    # pandas' default, which emits a bare NaN token in some code paths.
    export = export.astype(object).where(pd.notna(export), None)

    records = export.to_dict(orient="records")

    try:
        _atomic_write(
            path,
            lambda p: p.write_text(
                json.dumps(records, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            ),
        )
    except (OSError, PermissionError) as exc:
        logger.error("Failed to write JSON %s: %s", path, exc)
        return None

    logger.info("JSON written: %s (%d record(s))", path, len(records))
    return path


def export_insights(
    insights: dict[str, Any], filename: str = settings.INSIGHTS_FILENAME
) -> Path | None:
    """Write the summary/insights object alongside the cleaned data."""
    path = settings.CLEANED_DIR / filename

    try:
        _atomic_write(
            path,
            lambda p: p.write_text(
                json.dumps(insights, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            ),
        )
    except (OSError, PermissionError) as exc:
        logger.error("Failed to write insights %s: %s", path, exc)
        return None

    logger.info("Insights written: %s", path)
    return path


def export_all(df: pd.DataFrame, insights: dict[str, Any]) -> dict[str, Path | None]:
    """Run every export and report which ones landed."""
    ensure_directories()
    return {
        "csv": export_csv(df),
        "json": export_json(df),
        "insights": export_insights(insights),
    }
