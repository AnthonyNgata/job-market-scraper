"""
JSON API sources.

Why this exists alongside `scraper.py`
--------------------------------------
HTML scraping breaks *silently*: a board renames one CSS class and the run
keeps exiting 0 with zero rows until someone notices the dataset went flat. A
documented JSON contract breaks *loudly* and rarely, which is the behaviour you
want from something running unattended at 06:15 every morning.

Each provider gets a small normaliser rather than a config-driven field map,
because the differences between them are structural (unix epoch vs ISO 8601,
an explicit ``remote`` boolean vs none at all) and would turn any declarative
mapping into a config file full of lambdas. Endpoints and paging live in
``config/settings.py``; only the shape translation lives here.

Every normaliser emits exactly the dict shape `src.pipeline` expects, so the
cleaning and export stages are unchanged and source-agnostic.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config import settings
from src import scraper

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _html_to_text(html: str | None) -> str | None:
    """
    Flatten an HTML job description to plain text and cap its length.

    API descriptions are full HTML documents, routinely 10-20kB. They are
    reduced here for two reasons: the skill matcher would otherwise match
    tokens inside markup (a ``<a href=".../go/">`` is not the Go language),
    and an untruncated blob makes the exported CSV unreadable.
    """
    if not html:
        return None

    text = scraper._make_soup(html).get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None

    limit = settings.MAX_DESCRIPTION_CHARS
    if len(text) > limit:
        # Cut on a word boundary so the truncation doesn't invent a half-word
        # that then fails to match a real skill alias.
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def _clean_tags(*groups: Any) -> list[str]:
    """Flatten provider tag lists into de-duplicated, order-stable strings."""
    tags: list[str] = []
    for group in groups:
        for tag in group or []:
            value = str(tag).strip()
            if value and value not in tags:
                tags.append(value)
    return tags


def _matches_query(query: str, *texts: Any) -> bool:
    """
    Client-side search for providers with no server-side query parameter.

    Every whitespace-separated term must appear somewhere in the supplied text,
    so "data engineer" doesn't match every listing that merely says "data".
    """
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return True

    haystack = " ".join(str(t) for t in texts if t).lower()
    return all(term in haystack for term in terms)


# --------------------------------------------------------------------------- #
# Provider normalisers
# --------------------------------------------------------------------------- #

def _records_remotive(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload.get("jobs") or []


def _normalise_remotive(
    record: dict[str, Any], source_url: str, scraped_at: str
) -> dict[str, Any]:
    tags = _clean_tags(record.get("tags"), [record.get("category")])
    title = record.get("title")
    description = _html_to_text(record.get("description"))
    job_id = record.get("id")

    return {
        # Namespaced so datasets from two providers can be concatenated without
        # their integer ids colliding in the dedupe pass.
        "job_id": f"remotive-{job_id}" if job_id else None,
        "job_title": title,
        "company": record.get("company_name"),
        "location": record.get("candidate_required_location"),
        "salary_raw": record.get("salary"),
        "posted_raw": record.get("publication_date"),
        "description": description,
        "skills": scraper.extract_skills(" ".join(tags), description, title),
        "skill_tags_raw": tags,
        "url": record.get("url"),
        "source_url": source_url,
        "scraped_at": scraped_at,
    }


def _records_arbeitnow(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload.get("data") or []


def _normalise_arbeitnow(
    record: dict[str, Any], source_url: str, scraped_at: str
) -> dict[str, Any]:
    tags = _clean_tags(record.get("tags"), record.get("job_types"))
    title = record.get("title")
    description = _html_to_text(record.get("description"))
    slug = record.get("slug")

    # created_at is a unix timestamp; the rest of the pipeline treats
    # posted_raw as free text, so an ISO date is the most useful rendering.
    posted_raw: str | None = None
    created_at = record.get("created_at")
    if isinstance(created_at, (int, float)):
        try:
            posted_raw = datetime.fromtimestamp(created_at, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            logger.debug("Unreadable created_at %r on %s", created_at, slug)

    return {
        "job_id": f"arbeitnow-{slug}" if slug else None,
        "job_title": title,
        "company": record.get("company_name"),
        "location": record.get("location"),
        "salary_raw": None,          # this board does not publish salaries
        "posted_raw": posted_raw,
        "description": description,
        "skills": scraper.extract_skills(" ".join(tags), description, title),
        "skill_tags_raw": tags,
        "url": record.get("url"),
        "source_url": source_url,
        "scraped_at": scraped_at,
        # Stated outright by the API, so the pipeline should trust it instead of
        # guessing from keywords in the location string.
        "is_remote": bool(record.get("remote")),
    }


# name -> (records extractor, record normaliser)
ADAPTERS: dict[str, tuple[Callable[..., list], Callable[..., dict]]] = {
    "remotive": (_records_remotive, _normalise_remotive),
    "arbeitnow": (_records_arbeitnow, _normalise_arbeitnow),
}


def _provider_config(provider: str) -> dict[str, Any]:
    try:
        return settings.API_PROVIDERS[provider]
    except KeyError:
        raise ValueError(
            f"Unknown API provider {provider!r}. "
            f"Configured: {', '.join(sorted(settings.API_PROVIDERS))}"
        ) from None


def _normalise_all(
    provider: str,
    payload: dict[str, Any],
    source_url: str,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Turn one raw API payload into pipeline-shaped listing dicts."""
    config = _provider_config(provider)
    extract_records, normalise = ADAPTERS[provider]

    scraped_at = datetime.now(timezone.utc).isoformat()
    listings: list[dict[str, Any]] = []

    for index, record in enumerate(extract_records(payload)):
        try:
            listing = normalise(record, source_url, scraped_at)
        except Exception as exc:
            # One malformed record must not take the page down with it.
            logger.warning("Skipping record %d from %s: %s", index, provider, exc)
            continue

        if config["all_remote"]:
            listing["is_remote"] = True

        # Only filter here for providers that can't filter server-side.
        if query and not config["query_param"]:
            if not _matches_query(
                query, listing["job_title"], listing["description"],
                " ".join(listing["skill_tags_raw"]),
            ):
                continue

        listings.append(listing)

    return listings


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def _fetch_payload(
    url: str, params: dict[str, Any], session
) -> dict[str, Any] | None:
    """GET one URL and decode JSON, returning None on any unrecoverable error."""
    response = scraper.fetch(url, session=session, params=params)
    if response is None:
        return None

    try:
        payload = response.json()
    except ValueError as exc:
        # A provider serving an HTML error page with a 200 lands here.
        logger.error("Response from %s was not valid JSON: %s", response.url, exc)
        return None

    if not isinstance(payload, dict):
        logger.error("Unexpected JSON shape from %s: %s", response.url, type(payload).__name__)
        return None

    return payload


def fetch(
    query: str = settings.DEFAULT_QUERY,
    max_pages: int = settings.MAX_PAGES,
    provider: str | None = None,
) -> list[dict[str, Any]]:
    """
    Pull listings from the configured JSON provider.

    `max_pages` means whichever of the two things the provider supports: real
    pages for a paginating API, or a row budget of ``max_pages * per_page`` for
    one that returns a single capped response.
    """
    provider = provider or settings.API_PROVIDER
    config = _provider_config(provider)
    session = scraper.build_session()

    url = config["url"]
    query_param = config["query_param"]
    all_listings: list[dict[str, Any]] = []

    if not config["paginates"]:
        limit = max(1, max_pages) * config["per_page"]
        params: dict[str, Any] = {"limit": limit}
        if query_param and query:
            params[query_param] = query

        logger.info("Querying %s (%s, limit %d)", provider, url, limit)
        payload = _fetch_payload(url, params, session)
        if payload is None:
            logger.error("Provider %s returned nothing usable", provider)
            return []

        all_listings = _normalise_all(provider, payload, url, query)
        logger.info("%s: %d listing(s)", provider, len(all_listings))
        return all_listings

    for page in range(1, max_pages + 1):
        params = {"page": page}
        if query_param and query:
            params[query_param] = query

        payload = _fetch_payload(url, params, session)
        if payload is None:
            logger.warning("Page %d unavailable; continuing with next page", page)
            continue

        listings = _normalise_all(provider, payload, url, query)
        records_seen = len(ADAPTERS[provider][0](payload))
        if not records_seen:
            logger.info("No records on page %d — assuming end of results", page)
            break

        all_listings.extend(listings)
        logger.info(
            "%s page %d: %d record(s), %d matched", provider, page, records_seen, len(listings)
        )

        if page < max_pages:
            time.sleep(settings.REQUEST_DELAY)

    logger.info("API fetch complete: %d listing(s) from %s", len(all_listings), provider)
    return all_listings


# --------------------------------------------------------------------------- #
# Offline fixture
# --------------------------------------------------------------------------- #

def load_sample(path=None, query: str | None = None) -> list[dict[str, Any]]:
    """
    Normalise the bundled sample payloads without making a request.

    The fixture holds one captured response per provider, keyed by name, so a
    single offline run exercises *every* adapter. That is what makes the CI
    smoke test meaningful: a normaliser broken by a refactor fails in seconds
    on the fixture rather than silently emptying tomorrow's dataset.
    """
    path = Path(path or settings.SAMPLE_API_JSON)
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.error("Sample API payload not found at %s", path)
        return []
    except (OSError, ValueError) as exc:
        logger.error("Could not read %s: %s", path, exc)
        return []

    listings: list[dict[str, Any]] = []
    for provider, payload in fixture.items():
        if provider not in ADAPTERS:
            logger.warning("Fixture has no adapter for provider %r — skipping", provider)
            continue
        found = _normalise_all(provider, payload, path.as_uri(), query)
        logger.info("Fixture %s: %d listing(s)", provider, len(found))
        listings.extend(found)

    return listings
