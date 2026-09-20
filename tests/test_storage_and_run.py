"""Archive provenance, export integrity, and the exit codes CI branches on."""

import json

import pytest

import run as run_module
from config import settings
from src import storage


@pytest.fixture
def temp_data_dirs(tmp_path, monkeypatch):
    """Redirect all output into tmp_path so tests never touch data/."""
    raw, cleaned = tmp_path / "raw", tmp_path / "cleaned"
    monkeypatch.setattr(settings, "RAW_DIR", raw)
    monkeypatch.setattr(settings, "CLEANED_DIR", cleaned)
    return raw, cleaned


# --------------------------------------------------------------------------- #
# Archive provenance
# --------------------------------------------------------------------------- #

def test_archive_records_the_source_the_rows_actually_came_from(temp_data_dirs):
    # Regression: this used to record settings.BASE_URL, which became "" the day
    # the pipeline moved to JSON providers — every archive claimed to come from
    # nowhere.
    raw, _ = temp_data_dirs
    path = storage.save_raw([{"source_url": "https://remotive.test/api"}])

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source"] == "https://remotive.test/api"
    assert payload["record_count"] == 1


def test_a_multi_source_archive_keeps_every_source(temp_data_dirs):
    path = storage.save_raw([
        {"source_url": "https://a.test"},
        {"source_url": "https://b.test"},
        {"source_url": "https://a.test"},
    ])
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source"] == ["https://a.test", "https://b.test"]


def test_round_trips_through_load_raw(temp_data_dirs):
    rows = [{"job_title": "Data Engineer", "source_url": "https://a.test"}]
    path = storage.save_raw(rows)
    assert storage.load_raw(path) == rows


def test_a_corrupt_archive_reads_back_as_empty_not_an_exception(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert storage.load_raw(bad) == []


# --------------------------------------------------------------------------- #
# Exit codes — the workflow branches on these, so they are a contract
# --------------------------------------------------------------------------- #

def test_sample_mode_exits_zero(temp_data_dirs):
    assert run_module.main(["--sample"]) == 0


def test_sample_api_mode_exits_zero(temp_data_dirs):
    assert run_module.main(["--sample-api"]) == 0


def test_a_run_that_finds_nothing_exits_one(temp_data_dirs, tmp_path):
    # Exit 1 is "ran fine, found nothing" and must stay distinct from exit 2:
    # the workflow treats the first as a warning and the second as a failure.
    assert run_module.main(["--from-raw", str(tmp_path / "missing.json")]) == 1


def test_an_unhandled_error_exits_two(temp_data_dirs, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(run_module.scraper, "scrape_local", explode)
    assert run_module.main(["--sample"]) == 2


def test_exports_land_and_are_readable(temp_data_dirs):
    _, cleaned = temp_data_dirs
    assert run_module.main(["--sample"]) == 0

    rows = json.loads((cleaned / settings.JSON_FILENAME).read_text(encoding="utf-8"))
    insights = json.loads((cleaned / settings.INSIGHTS_FILENAME).read_text(encoding="utf-8"))

    assert rows and insights["total_listings"] == len(rows)
    assert (cleaned / settings.CSV_FILENAME).exists()


def test_the_csv_holds_no_list_columns(temp_data_dirs):
    # A repr'd Python list in a CSV cell is hostile to parse back; skills travel
    # as skills_joined instead.
    _, cleaned = temp_data_dirs
    run_module.main(["--sample"])

    header = (cleaned / settings.CSV_FILENAME).read_text(encoding="utf-8-sig").splitlines()[0]
    columns = header.split(",")
    assert "skills_joined" in columns
    # Compare whole field names: "has_skills" contains "skills" as a substring.
    assert "skills" not in columns
    assert "skill_tags_raw" not in columns
