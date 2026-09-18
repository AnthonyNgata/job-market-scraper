"""
Network requests and HTML parsing engine.

Design notes
------------
* `requests` + BeautifulSoup rather than Scrapy: the crawl is shallow (a handful
  of paginated list pages), so Scrapy's twisted reactor and project scaffolding
  buy us nothing while making the pipeline harder to run inside GitHub Actions.
* Every extraction is *defensive*. A job board that silently renames a class
  should cost us one field on one row, never the whole run — so missing tags
  resolve to ``None`` and get handled downstream in the Pandas pipeline.
* Anything that leaves this module is a plain dict of primitives, so it can be
  serialised straight to the raw JSON archive before any cleaning happens.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

from config import settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Skill matching
# --------------------------------------------------------------------------- #

def _build_skill_patterns() -> list[tuple[str, re.Pattern]]:
    """
    Pre-compile one regex per canonical skill, OR-ing its aliases together.

    Compiled once at import so the per-listing matching stays cheap. Aliases are
    escaped and wrapped in boundaries that tolerate the punctuation in names
    like ``Node.js``, ``CI/CD`` and ``C++`` — ``\\b`` alone fails on those
    because ``+`` and ``.`` are not word characters.
    """
    patterns: list[tuple[str, re.Pattern]] = []
    for canonical, aliases in settings.SKILL_VOCABULARY.items():
        # Longest alias first so "google cloud platform" wins over "google cloud".
        escaped = [re.escape(a) for a in sorted(aliases, key=len, reverse=True)]
        body = "|".join(escaped)
        # (?<![\w.+#]) / (?![\w.+#]) = "not glued to another token character".
        pattern = re.compile(rf"(?<![\w.+#])(?:{body})(?![\w.+#])", re.IGNORECASE)
        patterns.append((canonical, pattern))
    return patterns


SKILL_PATTERNS = _build_skill_patterns()


def extract_skills(*texts: str | None) -> list[str]:
    """
    Map arbitrary text onto the canonical skill vocabulary.

    Accepts several text fragments (tag list, description, title) and returns a
    de-duplicated, alphabetically stable list of canonical skill names. Returns
    an empty list rather than None so downstream `.explode()` behaves predictably.
    """
    haystack = " ".join(t for t in texts if t)
    if not haystack.strip():
        return []

    found = {canonical for canonical, pattern in SKILL_PATTERNS if pattern.search(haystack)}
    return sorted(found)


# --------------------------------------------------------------------------- #
# Defensive DOM helpers
# --------------------------------------------------------------------------- #

def _first_match(node: Tag, selectors: Iterable[str]) -> Tag | None:
    """Return the first element matching any selector in `selectors`, else None."""
    for selector in selectors:
        try:
            found = node.select_one(selector)
        except Exception as exc:  # malformed selector in config — log, don't crash
            logger.warning("Invalid selector %r skipped: %s", selector, exc)
            continue
        if found is not None:
            return found
    return None


def _text_of(node: Tag, field: str) -> str | None:
    """
    Pull normalised text for a configured field.

    Returns None when the tag is absent OR present-but-empty; both are "missing"
    as far as the cleaning pipeline is concerned, and collapsing them here means
    one code path handles both.
    """
    element = _first_match(node, settings.SELECTORS.get(field, []))
    if element is None:
        logger.debug("Field %r not found on card", field)
        return None

    text = element.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _link_of(node: Tag, base_url: str) -> str | None:
    """Resolve the listing's detail-page URL to an absolute address."""
    element = _first_match(node, settings.SELECTORS.get("url", []))
    if element is None:
        return None

    href = element.get("href")
    if not href or href.startswith(("#", "javascript:")):
        return None

    return urljoin(base_url, href)


def _job_id_of(node: Tag) -> str | None:
    """Read the board's own listing id from whichever attribute it uses."""
    for attr in settings.JOB_ID_ATTRS:
        value = node.get(attr)
        if value:
            # Some boards render id="job-12345"; keep the whole thing, it is
            # only ever used as an opaque dedupe key.
            return str(value).strip()
    return None


def _skill_tags_of(node: Tag) -> list[str]:
    """Collect discrete skill tags when the board renders them as a list."""
    container = _first_match(node, settings.SELECTORS.get("skills_container", []))
    if container is None:
        return []

    for selector in settings.SELECTORS.get("skill_item", []):
        items = container.select(selector)
        if items:
            return [i.get_text(strip=True) for i in items if i.get_text(strip=True)]
    return []


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def build_session() -> requests.Session:
    """A single Session reuses the TCP connection across paginated requests."""
    session = requests.Session()
    session.headers.update(settings.HEADERS)
    return session


def fetch_page(url: str, session: requests.Session | None = None) -> str | None:
    """
    Retrieve one page of HTML, retrying transient failures with exponential backoff.

    Returns the response body, or None when the page is unrecoverable. Returning
    None instead of raising is deliberate: one dead page out of ten should not
    abort a scheduled run that would otherwise collect nine pages of data.
    """
    session = session or build_session()

    for attempt in range(1, settings.MAX_RETRIES + 1):
        try:
            response = session.get(url, timeout=settings.REQUEST_TIMEOUT)

            # Retry only the codes that are plausibly transient; a 404 will
            # never succeed on attempt two, so fail it immediately.
            if response.status_code in settings.RETRY_STATUS_CODES:
                raise requests.HTTPError(
                    f"Retryable status {response.status_code}", response=response
                )

            response.raise_for_status()
            logger.info("Fetched %s (%d bytes)", url, len(response.content))
            return response.text

        except requests.Timeout:
            logger.warning("Timeout on %s (attempt %d/%d)", url, attempt, settings.MAX_RETRIES)

        except requests.ConnectionError as exc:
            logger.warning(
                "Connection error on %s (attempt %d/%d): %s",
                url, attempt, settings.MAX_RETRIES, exc,
            )

        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status not in settings.RETRY_STATUS_CODES:
                # Permanent: 404 (listing gone), 403 (blocked), 401 (auth needed).
                logger.error("Permanent HTTP %s on %s — not retrying", status, url)
                return None
            logger.warning(
                "HTTP %s on %s (attempt %d/%d)", status, url, attempt, settings.MAX_RETRIES
            )

        except requests.RequestException as exc:
            # Catch-all for anything else requests can raise (TooManyRedirects,
            # InvalidURL, SSLError...). Treated as retryable-then-fatal.
            logger.warning(
                "Request failed on %s (attempt %d/%d): %s",
                url, attempt, settings.MAX_RETRIES, exc,
            )

        if attempt < settings.MAX_RETRIES:
            sleep_for = settings.BACKOFF_FACTOR ** attempt
            logger.debug("Backing off %.1fs before retry", sleep_for)
            time.sleep(sleep_for)

    logger.error("Giving up on %s after %d attempts", url, settings.MAX_RETRIES)
    return None


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _make_soup(html: str) -> BeautifulSoup:
    """Parse with lxml, falling back to the stdlib parser if lxml is absent."""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        logger.debug("lxml unavailable; falling back to html.parser")
        return BeautifulSoup(html, "html.parser")


def parse_listings(html: str, source_url: str = settings.BASE_URL) -> list[dict[str, Any]]:
    """
    Turn one page of HTML into a list of raw listing dicts.

    Never raises on malformed markup: an unparseable page yields [], and a
    card that blows up mid-extraction is logged and skipped so the remaining
    cards on that page still make it through.
    """
    if not html:
        return []

    try:
        soup = _make_soup(html)
    except Exception as exc:
        logger.error("Could not parse HTML from %s: %s", source_url, exc)
        return []

    # Try each configured card selector until one actually matches something.
    cards: list[Tag] = []
    for selector in settings.SELECTORS["job_card"]:
        cards = soup.select(selector)
        if cards:
            logger.debug("Matched %d cards with selector %r", len(cards), selector)
            break

    if not cards:
        # Almost always means the board changed its markup — loud enough to
        # notice in the Actions log, but not fatal to the run.
        logger.error(
            "No job cards found at %s. Selectors in config/settings.py are "
            "probably stale.", source_url,
        )
        return []

    scraped_at = datetime.now(timezone.utc).isoformat()
    listings: list[dict[str, Any]] = []

    for index, card in enumerate(cards):
        try:
            description = _text_of(card, "description")
            title = _text_of(card, "title")
            tags = _skill_tags_of(card)

            # Skills come from tags when available and from the blurb otherwise;
            # feeding both plus the title into one matcher covers boards that do
            # a bit of each ("Senior Python Engineer" with no tag list).
            skills = extract_skills(" ".join(tags), description, title)

            listings.append(
                {
                    "job_id": _job_id_of(card),
                    "job_title": title,
                    "company": _text_of(card, "company"),
                    "location": _text_of(card, "location"),
                    "salary_raw": _text_of(card, "salary"),
                    "posted_raw": _text_of(card, "posted"),
                    "description": description,
                    "skills": skills,
                    "skill_tags_raw": tags,
                    "url": _link_of(card, source_url),
                    "source_url": source_url,
                    "scraped_at": scraped_at,
                }
            )

        except Exception as exc:
            # One bad card must not take the page down with it.
            logger.warning("Skipping card %d on %s: %s", index, source_url, exc)
            continue

    logger.info("Parsed %d listings from %s", len(listings), source_url)
    return listings


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def build_page_url(query: str, page: int) -> str:
    """Compose a paginated search URL from the template in settings."""
    return settings.PAGE_TEMPLATE.format(
        base=settings.BASE_URL,
        path=settings.SEARCH_PATH,
        query=requests.utils.quote(query),
        page=page,
    )


def scrape(
    query: str = settings.DEFAULT_QUERY,
    max_pages: int = settings.MAX_PAGES,
) -> list[dict[str, Any]]:
    """
    Walk the paginated result set and return every listing found.

    Stops early on the first page that yields no listings, which is how most
    boards signal "past the end" without a distinct status code.
    """
    session = build_session()
    all_listings: list[dict[str, Any]] = []
    page = 0  # defined up front so the closing log line is safe if max_pages < 1

    for page in range(1, max_pages + 1):
        url = build_page_url(query, page)
        html = fetch_page(url, session=session)

        if html is None:
            logger.warning("Page %d unavailable; continuing with next page", page)
            continue

        listings = parse_listings(html, source_url=url)
        if not listings:
            logger.info("No listings on page %d — assuming end of results", page)
            break

        all_listings.extend(listings)

        # Politeness pause; skipped after the final page so we don't sleep for
        # no reason at the end of the run.
        if page < max_pages:
            time.sleep(settings.REQUEST_DELAY)

    logger.info("Scrape complete: %d listings across %d page(s)", len(all_listings), page)
    return all_listings


def scrape_local(path) -> list[dict[str, Any]]:
    """
    Parse a saved HTML file instead of making a request.

    Used by `run.py --sample` so the full pipeline is runnable offline, and by
    tests that need deterministic input.
    """
    from pathlib import Path

    path = Path(path)
    try:
        html = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("Sample HTML not found at %s", path)
        return []
    except OSError as exc:
        logger.error("Could not read %s: %s", path, exc)
        return []

    return parse_listings(html, source_url=path.as_uri())
