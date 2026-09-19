# Automated Job Market Scraper

Scrapes a tech job board, cleans the listings with Pandas, and exports a
structured dataset plus a skills-demand summary. Runs daily via GitHub Actions.

```
scrape  ->  archive raw JSON  ->  clean  ->  export CSV + JSON + insights
```

## Quick start

```bash
pip install -r requirements.txt
python run.py --sample      # full pipeline, offline, no network
```

`--sample` parses `samples/sample_job_board.html`, a fixture that deliberately
contains the defects real boards produce (missing tags, duplicate postings,
ragged whitespace, unparseable salaries), so every branch of the cleaning code
gets exercised without touching a live site.

## Usage

```bash
python run.py --query "data engineer" --pages 3   # live run
python run.py --sample                            # offline HTML fixture
python run.py --sample-api                        # offline API fixture
python run.py --from-raw data/raw/jobs_<ts>.json  # re-clean, no re-scrape
python run.py --sample --verbose                  # debug logging
```

| Exit code | Meaning |
|---|---|
| `0` | Success |
| `1` | Ran cleanly but collected no listings |
| `2` | Unrecoverable error (traceback in the log) |

## Where the data comes from

`settings.SOURCE` picks the live path:

| `SOURCE` | Module | Notes |
|---|---|---|
| `"api"` *(default)* | [src/api_source.py](src/api_source.py) | Public JSON APIs with a documented contract |
| `"html"` | [src/scraper.py](src/scraper.py) | CSS-selector scraping; you own the selector churn |

The API path is the default because HTML scraping fails *silently* — a board
renames one class and the run keeps exiting 0 with zero rows until someone
notices the dataset went flat. A JSON contract fails loudly and rarely, which
is what you want from something running unattended at 06:15 every morning.

### API providers

Configured in `settings.API_PROVIDERS`, with a normaliser each in
`src/api_source.py`:

| Provider | Search | Paging | Notes |
|---|---|---|---|
| `remotive` *(default)* | server-side | single capped response | Remote-only; has salary + tags |
| `arbeitnow` | client-side | 250/page | General board, Germany-heavy; explicit `remote` flag |

`--pages` means whichever the provider supports: real pages for a paginating
API, or a row budget of `pages × per_page` for one that returns a single
capped response.

Adding a provider is an entry in `API_PROVIDERS` plus a normaliser registered
in `ADAPTERS`. Normalisers are code, not config, because the differences are
structural — unix epochs vs ISO 8601, an explicit `remote` boolean vs none —
and a declarative field map would just become a config file full of lambdas.

### Pointing the HTML path at a real board

`BASE_URL` ships **empty on purpose**: a placeholder domain here is what made
the nightly run fail silently for two days, so an unset target now raises
immediately instead of burning three minutes on retries at 06:15 UTC.

1. Set `SOURCE = "html"`, then `BASE_URL`, `SEARCH_PATH` and `PAGE_TEMPLATE`.
2. Update `SELECTORS`. Each field takes a **list** of fallback selectors tried
   in order, because boards A/B test their markup and a single selector is
   fragile. A miss degrades to `None`; it doesn't raise.
3. Extend `SKILL_VOCABULARY` for your domain.
4. Repoint `HEADERS["User-Agent"]` at a URL **you** control if you fork this.
   It currently identifies the bot as
   `github.com/AnthonyNgata/job-market-scraper`, which is how a site admin who
   spots the scraper in their logs finds out who is running it. Keep it
   reachable — a dead link is worse than none.

Check the target's `robots.txt` and terms of service first, and leave
`REQUEST_DELAY` at a polite value.

## How the pipeline works

### 1. Scraping — [src/scraper.py](src/scraper.py)

`requests` + BeautifulSoup rather than Scrapy: the crawl is shallow (a few
paginated list pages), so Scrapy's reactor and project scaffolding add weight
without buying anything, and this runs cleanly inside a GitHub Actions job.

**Error handling.** `fetch_page` retries with exponential backoff, and
distinguishes failures that are worth retrying from ones that aren't:

- **Transient** — timeouts, connection errors, and `429/500/502/503/504` are
  retried up to `MAX_RETRIES` with a `1.5 ** attempt` backoff.
- **Permanent** — `404`, `403`, `401` fail immediately; a second attempt will
  never succeed.
- **Exhausted** — returns `None` rather than raising, so one dead page out of
  ten doesn't abort a scheduled run that would otherwise collect nine.

Parsing is defensive at three levels: an unparseable page yields `[]`, a card
that raises mid-extraction is logged and skipped so its neighbours still land,
and a missing tag resolves to `None`. If *no* cards match, that's logged as an
error — it almost always means the board changed its markup.

**Skill extraction** maps text onto a controlled vocabulary, so
`postgres` / `PostgreSQL` / `psql` all count as one skill and the frequency
ranking is actually comparable. Skills come from the board's tag list when it
has one and from the description prose when it doesn't. The matcher uses
`(?<![\w.+#])` boundaries instead of `\b` so `Node.js`, `CI/CD` and `C++` match
correctly while `nodes` doesn't match `Node`.

### 2. Cleaning — [src/pipeline.py](src/pipeline.py)

Ordered transforms; the order is load-bearing:

1. **Normalise text** — collapse whitespace, map `"N/A"`, `"-"`, `""` to null.
2. **Handle missing values** — two tiers. A row missing `job_title` or
   `company` isn't a listing, so it's dropped and counted. Everything else is
   filled with a visible `"Unknown"`: a job with no stated location is still a
   valid data point for skill demand, and an explicit placeholder is more
   honest in a CSV than a blank cell readers mistake for an empty string.
3. **Derive fields** — `is_remote`, parsed `salary_min`/`salary_max`,
   `skill_count`.
4. **Deduplicate** — three passes in descending order of confidence:
   `job_id`, then `url`, then a normalised `(title, company, location)`
   composite. The composite pass catches the same role re-listed under a fresh
   id and the overlap you get when a board reflows results between page 1 and
   page 2 mid-scrape.
5. **Insights** — top skills, counted **per listing** rather than per mention,
   so a blurb saying "Python" three times still contributes 1.

Normalisation must precede deduplication (or `"Acme  Corp "` and `"acme corp"`
both survive), and deduplication must precede the skill counts (or a
cross-posted listing double-counts every skill it names and skews the top 5).

Empty input is safe at every step: you get an empty-but-correctly-shaped frame
and a zeroed insights object, not an exception.

### 3. Export — [src/storage.py](src/storage.py)

| File | Contents |
|---|---|
| `data/raw/jobs_<timestamp>.json` | Unmodified scraper output, archived **before** cleaning |
| `data/cleaned/jobs_cleaned.csv` | Flat table; `utf-8-sig` so Excel on Windows reads accented names correctly |
| `data/cleaned/jobs_cleaned.json` | Records orientation, `skills` preserved as a real nested array |
| `data/cleaned/insights.json` | Top 5 skills, counts, salary coverage, top locations |

The raw archive is the most useful habit here: when the cleaning logic turns
out to be wrong three weeks from now, you can re-derive the cleaned set with
`--from-raw` instead of re-scraping listings that are already gone.

CSV drops the list-valued columns in favour of `skills_joined` — CSV has no
array type, and a repr'd Python list is hostile to parse back. JSON keeps them
intact.

All writes are **atomic** (temp file, then `os.replace`), so a run that dies
halfway — or an Actions job that hits its timeout — leaves the previous good
file intact rather than a truncated one the next run would read as valid.

## Automation

[.github/workflows/scrape_cron.yml](.github/workflows/scrape_cron.yml) runs
daily at 06:15 UTC and on manual dispatch. It smoke-tests against the offline
fixture first, so broken parsing code fails in seconds without hitting the live
site; then scrapes, uploads both datasets as build artifacts, and commits the
refreshed `data/cleaned/` back to the repo only when something actually changed.

## Sample output

From `python run.py --sample` — 9 scraped, 1 dropped (no company), 2 duplicates
removed, 6 retained:

```
  TOP 5 SKILLS IN DEMAND
  1. Python                   3 jobs  (50.0%)
  2. SQL                      3 jobs  (50.0%)
  3. AWS                      3 jobs  (50.0%)
  4. Kafka                    3 jobs  (50.0%)
  5. dbt                      2 jobs  (33.3%)
```

## Project layout

```
├── run.py                   # pipeline entry point + CLI
├── config/settings.py       # providers, URLs, headers, selectors, vocabulary
├── src/
│   ├── api_source.py        # JSON provider adapters (default live path)
│   ├── scraper.py           # requests + BeautifulSoup engine, shared retries
│   ├── pipeline.py          # Pandas cleaning and insights
│   └── storage.py           # atomic reads/writes and exports
├── samples/                 # offline HTML + API fixtures
└── data/{raw,cleaned}/      # archives and outputs
```
