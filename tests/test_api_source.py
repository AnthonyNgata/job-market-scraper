"""
API adapters and the provider fallback chain.

The normalisers are what the `--sample-api` smoke test exercises end to end;
what it can't check is the *shape* each one promises the pipeline, or the
failover behaviour that only shows up when a provider misbehaves.
"""

import pytest

from config import settings
from src import api_source


# --------------------------------------------------------------------------- #
# Query matching
# --------------------------------------------------------------------------- #

def test_every_term_must_be_present():
    assert api_source._matches_query("data engineer", "Senior Data Engineer")
    # "data" alone is not a match for "data engineer" — otherwise a client-side
    # filtered board returns every listing that merely says "data".
    assert not api_source._matches_query("data engineer", "Data Analyst")


def test_terms_may_be_spread_across_fields():
    assert api_source._matches_query("data engineer", "Engineer", "works on data")


def test_an_empty_query_matches_everything():
    assert api_source._matches_query("", "anything at all")


# --------------------------------------------------------------------------- #
# Normalisers
# --------------------------------------------------------------------------- #

def test_remotive_record_maps_onto_the_pipeline_shape():
    record = {
        "id": 42,
        "title": "Data Engineer",
        "company_name": "Acme",
        "candidate_required_location": "Europe",
        "salary": "$90,000 - $120,000",
        "publication_date": "2026-09-19T10:00:00",
        "description": "<p>We use <b>Python</b> and Kafka.</p>",
        "tags": ["python", "kafka"],
        "category": "Data",
        "url": "https://remotive.test/42",
    }
    out = api_source._normalise_remotive(record, "https://remotive.test", "NOW")

    assert out["job_id"] == "remotive-42"          # namespaced against collisions
    assert out["company"] == "Acme"
    assert out["description"] == "We use Python and Kafka."   # HTML stripped
    assert {"Python", "Kafka"} <= set(out["skills"])
    assert out["scraped_at"] == "NOW"


def test_arbeitnow_unix_timestamp_becomes_an_iso_date():
    record = {
        "slug": "data-engineer-berlin",
        "title": "Data Engineer",
        "company_name": "Globex",
        "location": "Berlin",
        "description": "<p>SQL and dbt.</p>",
        "tags": ["sql"],
        "job_types": ["full-time"],
        "created_at": 1_758_326_400,      # 2025-09-20T00:00:00Z
        "remote": True,
        "url": "https://arbeitnow.test/1",
    }
    out = api_source._normalise_arbeitnow(record, "https://arbeitnow.test", "NOW")

    assert out["job_id"] == "arbeitnow-data-engineer-berlin"
    assert out["posted_raw"] == "2025-09-20"
    assert out["is_remote"] is True
    assert out["salary_raw"] is None            # this board publishes none


def test_an_unreadable_timestamp_degrades_to_none_rather_than_raising():
    record = {"slug": "x", "title": "T", "company_name": "C", "created_at": "not-a-number"}
    assert api_source._normalise_arbeitnow(record, "s", "NOW")["posted_raw"] is None


def test_all_remote_providers_have_the_flag_asserted():
    payload = {"jobs": [{"id": 1, "title": "Data Engineer", "company_name": "Acme"}]}
    out = api_source._normalise_all("remotive", payload, "https://remotive.test")
    assert out[0]["is_remote"] is True


def test_one_malformed_record_does_not_take_the_page_down(monkeypatch):
    def explode(record, source_url, scraped_at):
        if record.get("id") == 2:
            raise ValueError("malformed")
        return {"job_title": record.get("title"), "skill_tags_raw": [], "description": None}

    monkeypatch.setitem(api_source.ADAPTERS, "remotive", (api_source._records_remotive, explode))
    payload = {"jobs": [{"id": 1, "title": "A"}, {"id": 2}, {"id": 3, "title": "C"}]}

    out = api_source._normalise_all("remotive", payload, "s")
    assert [row["job_title"] for row in out] == ["A", "C"]


def test_an_unknown_provider_is_rejected_by_name():
    with pytest.raises(ValueError, match="nope"):
        api_source._provider_config("nope")


# --------------------------------------------------------------------------- #
# Fallback chain
# --------------------------------------------------------------------------- #

def test_the_first_provider_that_returns_rows_wins(monkeypatch):
    calls = []

    def fake_fetch(query, max_pages, provider):
        calls.append(provider)
        return [{"job_title": provider}]

    monkeypatch.setattr(api_source, "fetch", fake_fetch)
    out = api_source.fetch_any("q", 1, providers=["first", "second"])

    assert out == [{"job_title": "first"}]
    assert calls == ["first"]          # the fallback is never touched


def test_an_empty_provider_falls_through_to_the_next(monkeypatch):
    def fake_fetch(query, max_pages, provider):
        return [] if provider == "first" else [{"job_title": "from-second"}]

    monkeypatch.setattr(api_source, "fetch", fake_fetch)
    out = api_source.fetch_any("q", 1, providers=["first", "second"])
    assert out == [{"job_title": "from-second"}]


def test_a_raising_provider_falls_through_rather_than_killing_the_run(monkeypatch):
    def fake_fetch(query, max_pages, provider):
        if provider == "first":
            raise ConnectionError("blocked")
        return [{"job_title": "from-second"}]

    monkeypatch.setattr(api_source, "fetch", fake_fetch)
    out = api_source.fetch_any("q", 1, providers=["first", "second"])
    assert out == [{"job_title": "from-second"}]


def test_an_exhausted_chain_returns_empty_not_an_exception(monkeypatch):
    monkeypatch.setattr(api_source, "fetch", lambda query, max_pages, provider: [])
    assert api_source.fetch_any("q", 1, providers=["first", "second"]) == []


def test_every_provider_in_the_configured_chain_has_an_adapter():
    # Catches a chain entry that was renamed in settings but not in ADAPTERS —
    # which would only surface at 06:15 on the day the primary went down.
    for provider in settings.API_PROVIDER_CHAIN:
        assert provider in api_source.ADAPTERS
        assert provider in settings.API_PROVIDERS


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def test_the_bundled_fixture_exercises_every_adapter():
    listings = api_source.load_sample()
    assert listings
    sources = {row["job_id"].split("-")[0] for row in listings if row["job_id"]}
    assert sources == set(api_source.ADAPTERS)


def test_a_missing_fixture_is_reported_not_raised(tmp_path):
    assert api_source.load_sample(tmp_path / "nope.json") == []
