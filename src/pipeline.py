"""
Pandas data-cleaning routines.

The pipeline is a sequence of small, independently testable transforms applied
in a fixed order:

    normalise text -> drop unusable rows -> fill missing -> derive fields
                   -> deduplicate -> compute insights

Order matters. Normalisation must precede deduplication (otherwise
"Acme  Corp " and "acme corp" survive as two rows), and deduplication must
precede the skill counts (otherwise a cross-posted listing double-counts every
skill it mentions and skews the top 5).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pandas as pd

from config import settings

logger = logging.getLogger(__name__)


# Schema every cleaned frame is guaranteed to have, so downstream consumers
# (and the CSV header) stay stable even on an empty or partial scrape.
EXPECTED_COLUMNS = [
    "job_id",
    "job_title",
    "company",
    "location",
    "salary_raw",
    "posted_raw",
    "description",
    "skills",
    "url",
    "source_url",
    "scraped_at",
]


# --------------------------------------------------------------------------- #
# Frame construction
# --------------------------------------------------------------------------- #

def to_dataframe(listings: list[dict[str, Any]]) -> pd.DataFrame:
    """
    Build a DataFrame with a guaranteed schema from raw scraper output.

    An empty scrape returns an empty frame *with the right columns* rather than
    a shapeless one, so every later step can assume the columns exist.
    """
    df = pd.DataFrame(listings)

    for column in EXPECTED_COLUMNS:
        if column not in df.columns:
            logger.debug("Column %r absent from scrape; adding as empty", column)
            df[column] = pd.Series(dtype="object")

    # Keep any extra columns the scraper produced, but lead with the known ones.
    extras = [c for c in df.columns if c not in EXPECTED_COLUMNS]
    return df[EXPECTED_COLUMNS + extras]


# --------------------------------------------------------------------------- #
# Step 1 — text normalisation
# --------------------------------------------------------------------------- #

def _clean_text(value: Any) -> str | None:
    """Collapse whitespace, strip junk, and map empty-ish values to None."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None

    text = re.sub(r"\s+", " ", str(value)).strip()
    # Boards frequently emit these as literal placeholder strings.
    if text.lower() in {"", "n/a", "na", "none", "null", "-", "--"}:
        return None
    return text


def normalise_text(df: pd.DataFrame) -> pd.DataFrame:
    """Apply `_clean_text` to every free-text column."""
    df = df.copy()
    text_columns = [
        "job_id", "job_title", "company", "location",
        "salary_raw", "posted_raw", "description", "url",
    ]
    for column in text_columns:
        df[column] = df[column].map(_clean_text)

    # Ensure `skills` is always a list — a missing value here would break the
    # explode() in the insights step.
    df["skills"] = df["skills"].map(
        lambda v: list(v) if isinstance(v, (list, tuple, set)) else []
    )
    return df


# --------------------------------------------------------------------------- #
# Step 2 — missing values
# --------------------------------------------------------------------------- #

def handle_missing(df: pd.DataFrame) -> pd.DataFrame:
    """
    Two-tier policy for nulls.

    * REQUIRED_FIELDS (title, company) — a listing without these is not a
      listing, so the row is dropped and counted in the log.
    * Everything else — filled with a visible placeholder. Filling beats
      dropping here because a job with no stated location is still a real
      data point for skill demand, and an explicit "Unknown" is more honest
      in a CSV than a blank cell that readers will mistake for an empty string.
    """
    df = df.copy()
    before = len(df)

    df = df.dropna(subset=settings.REQUIRED_FIELDS, how="any")
    dropped = before - len(df)
    if dropped:
        logger.info(
            "Dropped %d row(s) missing required field(s): %s",
            dropped, ", ".join(settings.REQUIRED_FIELDS),
        )

    fill_columns = ["location", "salary_raw", "posted_raw", "description"]
    for column in fill_columns:
        missing = int(df[column].isna().sum())
        if missing:
            logger.debug("Filled %d missing value(s) in %r", missing, column)
        df[column] = df[column].fillna(settings.MISSING_PLACEHOLDER)

    # A listing with no recognised skills is meaningful (it just didn't mention
    # any), so flag it rather than dropping or filling it.
    df["has_skills"] = df["skills"].map(lambda s: len(s) > 0)

    return df


# --------------------------------------------------------------------------- #
# Step 3 — derived fields
# --------------------------------------------------------------------------- #

_SALARY_NUMBER = re.compile(r"(\d[\d,.]*)\s*(k|K)?")


def _parse_salary(value: str) -> tuple[float | None, float | None]:
    """
    Pull a (min, max) numeric range out of a free-text salary string.

    Handles the common shapes — "$90,000 - $120,000", "£70k–£85k", "120k" —
    and returns (None, None) for anything it can't read, e.g. "Competitive".
    Values written with a `k` suffix are scaled to whole units.
    """
    if not value or value == settings.MISSING_PLACEHOLDER:
        return None, None

    numbers: list[float] = []
    for raw, suffix in _SALARY_NUMBER.findall(value):
        try:
            amount = float(raw.replace(",", ""))
        except ValueError:
            continue
        if suffix:
            amount *= 1_000
        # Filter out stray numbers that are clearly not salaries (e.g. the "4"
        # in "4 day week", or a year like 2026 appearing in the same string).
        if amount >= 1_000:
            numbers.append(amount)

    if not numbers:
        return None, None
    return min(numbers), max(numbers)


def add_derived_fields(df: pd.DataFrame) -> pd.DataFrame:
    """Add analysis-friendly columns derived from the cleaned text."""
    df = df.copy()

    # Remote flag: checked across title and location because boards put the
    # signal in either one.
    haystack = (df["job_title"].fillna("") + " " + df["location"].fillna("")).str.lower()
    pattern = "|".join(re.escape(m) for m in settings.REMOTE_MARKERS)
    inferred = haystack.str.contains(pattern, regex=True, na=False)

    if "is_remote" in df.columns:
        # A source that states remoteness outright (an API boolean) is
        # authoritative; the keyword heuristic only fills the gaps. Without
        # this, a remote-only board whose location reads "Europe, APAC" would
        # be scored 0% remote.
        stated = df["is_remote"]
        df["is_remote"] = stated.where(stated.notna(), inferred).astype(bool)
    else:
        df["is_remote"] = inferred

    parsed = df["salary_raw"].map(_parse_salary)
    df["salary_min"] = parsed.map(lambda p: p[0])
    df["salary_max"] = parsed.map(lambda p: p[1])

    df["skill_count"] = df["skills"].map(len)

    # Comma-joined copy of the skill list: CSV has no native array type, and a
    # stringified Python list ("['SQL', 'Python']") is painful to parse back.
    df["skills_joined"] = df["skills"].map(lambda s: ", ".join(s))

    return df


# --------------------------------------------------------------------------- #
# Step 4 — deduplication
# --------------------------------------------------------------------------- #

def _normalise_key(value: Any) -> str:
    """Lowercase, strip punctuation and collapse spaces to form a match key."""
    text = str(value or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)   # "Acme, Inc." -> "acme inc"
    return re.sub(r"\s+", " ", text).strip()


def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove repeated postings using the strongest identifier available.

    Passes, in order of confidence:
      1. `job_id`   — the board's own id; authoritative when present.
      2. `url`      — a distinct detail page is a distinct posting.
      3. normalised (title, company, location) — catches the same role
         re-listed with a fresh id, and the overlap you get when a board
         reflows results between page 1 and page 2 mid-scrape.

    The first occurrence is kept, which is the earliest-seen (and so
    highest-ranked) copy given pages are scraped in order.
    """
    df = df.copy()
    before = len(df)

    # Pass 1 & 2 — only applied to rows that actually have the identifier,
    # otherwise every id-less row would collapse into a single NaN group.
    for column in ("job_id", "url"):
        has_value = df[column].notna()
        duplicated = has_value & df.duplicated(subset=[column], keep="first")
        if duplicated.any():
            logger.info("Removed %d duplicate(s) by %r", int(duplicated.sum()), column)
            df = df[~duplicated]

    # Pass 3 — fuzzy composite key.
    df["job_title_key"] = df["job_title"].map(_normalise_key)
    df["company_key"] = df["company"].map(_normalise_key)
    df["location_key"] = df["location"].map(_normalise_key)

    composite_dupes = df.duplicated(subset=settings.DEDUPE_KEYS, keep="first")
    if composite_dupes.any():
        logger.info(
            "Removed %d duplicate(s) by title/company/location", int(composite_dupes.sum())
        )
        df = df[~composite_dupes]

    df = df.drop(columns=settings.DEDUPE_KEYS)

    logger.info("Deduplication: %d -> %d row(s)", before, len(df))
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Step 5 — insights
# --------------------------------------------------------------------------- #

def top_skills(df: pd.DataFrame, top_n: int = settings.TOP_N_SKILLS) -> pd.DataFrame:
    """
    Rank skills by how many distinct listings demand them.

    Counted per *listing*, not per mention: a job that says "Python" three
    times in its blurb still contributes 1. `explode` turns the list column
    into one row per (listing, skill), which is exactly the grain needed.
    """
    if df.empty or not df["skills"].map(len).any():
        logger.warning("No skills found in dataset — top skills will be empty")
        return pd.DataFrame(columns=["skill", "job_count", "pct_of_listings"])

    exploded = df[["skills"]].explode("skills").dropna(subset=["skills"])

    counts = (
        exploded["skills"]
        .value_counts()
        .head(top_n)
        .rename_axis("skill")
        .reset_index(name="job_count")
    )
    counts["pct_of_listings"] = (counts["job_count"] / len(df) * 100).round(1)
    return counts


def build_insights(df: pd.DataFrame) -> dict[str, Any]:
    """Assemble the summary object that gets written to insights.json."""
    skills = top_skills(df)

    insights: dict[str, Any] = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "total_listings": int(len(df)),
        "unique_companies": int(df["company"].nunique()) if not df.empty else 0,
        "remote_listings": int(df["is_remote"].sum()) if not df.empty else 0,
        "listings_with_skills": int(df["has_skills"].sum()) if not df.empty else 0,
        f"top_{settings.TOP_N_SKILLS}_skills": skills.to_dict(orient="records"),
    }

    if not df.empty:
        # Only meaningful over rows where a salary was actually parseable.
        salaries = df["salary_min"].dropna()
        insights["salary_coverage"] = {
            "listings_with_salary": int(len(salaries)),
            "median_min_salary": float(salaries.median()) if len(salaries) else None,
        }
        insights["top_locations"] = (
            df["location"].value_counts().head(5).to_dict()
        )

    return insights


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def clean(listings: list[dict[str, Any]]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Run the full cleaning pipeline.

    Returns (cleaned DataFrame, insights dict). Safe on empty input: you get an
    empty-but-correctly-shaped frame and a zeroed insights object rather than
    an exception, so a scrape that found nothing still produces valid outputs.
    """
    logger.info("Cleaning %d raw listing(s)", len(listings))

    df = to_dataframe(listings)
    df = normalise_text(df)
    df = handle_missing(df)
    df = add_derived_fields(df)
    df = deduplicate(df)

    insights = build_insights(df)
    logger.info(
        "Cleaning complete: %d listing(s) retained from %d raw",
        len(df), len(listings),
    )
    return df, insights
