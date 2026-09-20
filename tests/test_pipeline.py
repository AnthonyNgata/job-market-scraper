"""Cleaning, deduplication and derived fields."""

import pandas as pd

from config import settings
from src import pipeline


def listing(**overrides):
    """A minimally valid listing; override only what a test cares about."""
    base = {
        "job_id": None,
        "job_title": "Data Engineer",
        "company": "Acme Corp",
        "location": "Berlin",
        "salary_raw": None,
        "posted_raw": None,
        "description": "We use Python and SQL.",
        "skills": ["Python", "SQL"],
        "url": None,
        "source_url": "https://example.test/jobs",
        "scraped_at": "2026-09-20T00:00:00+00:00",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

def test_empty_input_still_produces_the_full_schema():
    df = pipeline.to_dataframe([])
    assert list(df.columns) == pipeline.EXPECTED_COLUMNS
    assert df.empty


def test_extra_columns_are_kept_after_the_known_ones():
    df = pipeline.to_dataframe([listing(is_remote=True)])
    assert list(df.columns)[: len(pipeline.EXPECTED_COLUMNS)] == pipeline.EXPECTED_COLUMNS
    assert "is_remote" in df.columns


# --------------------------------------------------------------------------- #
# Required fields
# --------------------------------------------------------------------------- #

def test_rows_missing_a_required_field_are_dropped():
    df, _ = pipeline.clean([listing(), listing(company=None), listing(job_title=None)])
    assert len(df) == 1


def test_placeholder_strings_count_as_missing():
    # A board writing "N/A" into the company cell must not survive as a company.
    df, _ = pipeline.clean([listing(company="N/A")])
    assert df.empty


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #

def test_deduplicates_on_job_id():
    df, _ = pipeline.clean([
        listing(job_id="abc", company="One"),
        listing(job_id="abc", company="Two"),
    ])
    assert len(df) == 1


def test_deduplicates_on_url():
    df, _ = pipeline.clean([
        listing(url="https://example.test/1", company="One"),
        listing(url="https://example.test/1", company="Two"),
    ])
    assert len(df) == 1


def test_deduplicates_on_normalised_title_company_location():
    df, _ = pipeline.clean([
        listing(job_title="Data Engineer", company="Acme, Inc."),
        listing(job_title="  data   engineer ", company="acme inc"),
    ])
    assert len(df) == 1


def test_rows_without_identifiers_are_not_collapsed_together():
    # Every id here is None; they must not all group into one NaN bucket.
    df, _ = pipeline.clean([
        listing(company="Acme"),
        listing(company="Globex"),
        listing(company="Initech"),
    ])
    assert len(df) == 3


# --------------------------------------------------------------------------- #
# Derived fields
# --------------------------------------------------------------------------- #

def test_remote_is_inferred_from_text_when_not_stated():
    df, _ = pipeline.clean([listing(location="Remote (EU)")])
    assert bool(df.loc[0, "is_remote"]) is True


def test_a_stated_remote_flag_beats_the_keyword_heuristic():
    # A remote-only board whose location reads "Europe, APAC" would otherwise
    # be scored 0% remote.
    df, _ = pipeline.clean([listing(location="Europe, APAC", is_remote=True)])
    assert bool(df.loc[0, "is_remote"]) is True


def test_stated_not_remote_is_respected():
    df, _ = pipeline.clean([listing(location="Berlin", is_remote=False)])
    assert bool(df.loc[0, "is_remote"]) is False


# --------------------------------------------------------------------------- #
# Salary parsing
# --------------------------------------------------------------------------- #

def test_parses_a_plain_currency_range():
    assert pipeline._parse_salary("$90,000 - $120,000") == (90_000.0, 120_000.0)


def test_parses_a_k_suffixed_range():
    assert pipeline._parse_salary("£70k–£85k") == (70_000.0, 85_000.0)


def test_parses_a_single_value():
    assert pipeline._parse_salary("120k") == (120_000.0, 120_000.0)


def test_unparseable_salary_yields_no_numbers():
    assert pipeline._parse_salary("Competitive") == (None, None)


def test_small_stray_numbers_are_not_salaries():
    assert pipeline._parse_salary("4 day week") == (None, None)


def test_the_missing_placeholder_is_not_parsed():
    assert pipeline._parse_salary(settings.MISSING_PLACEHOLDER) == (None, None)


# --------------------------------------------------------------------------- #
# Insights
# --------------------------------------------------------------------------- #

def test_skills_are_counted_once_per_listing_not_per_mention():
    df, insights = pipeline.clean([
        listing(description="Python Python Python", skills=["Python"]),
        listing(company="Globex", skills=["Python"]),
    ])
    top = insights[f"top_{settings.TOP_N_SKILLS}_skills"]
    python = next(row for row in top if row["skill"] == "Python")
    assert python["job_count"] == 2
    assert python["pct_of_listings"] == 100.0


def test_insights_survive_a_dataset_with_no_recognised_skills():
    _, insights = pipeline.clean([listing(skills=[])])
    assert insights[f"top_{settings.TOP_N_SKILLS}_skills"] == []
    assert insights["listings_with_skills"] == 0


def test_counts_are_plain_ints_so_the_json_export_is_valid():
    _, insights = pipeline.clean([listing()])
    for key in ("total_listings", "unique_companies", "remote_listings"):
        assert isinstance(insights[key], int)
        assert not isinstance(insights[key], (pd.Int64Dtype, bool))
