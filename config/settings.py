"""
Central configuration for the job market scraper.

Everything that is likely to change when you point the scraper at a different
job board lives here, so `src/` never needs editing for a new target: URLs,
HTTP behaviour, CSS selectors, and the controlled skill vocabulary.
"""

from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
CLEANED_DIR = DATA_DIR / "cleaned"
SAMPLES_DIR = PROJECT_ROOT / "samples"

# Offline fixture used by `python run.py --sample`. Lets the whole pipeline be
# exercised end to end without hitting the network (useful in CI and tests).
SAMPLE_HTML = SAMPLES_DIR / "sample_job_board.html"


# --------------------------------------------------------------------------- #
# Target site
# --------------------------------------------------------------------------- #

# Generic tech job board. Replace with your real target; the selectors below
# are the only other thing that needs to change.
BASE_URL = "https://example-jobs.dev"
SEARCH_PATH = "/jobs"

# Pagination is expressed as a template so boards using ?page=, ?offset= or
# /page/2/ styles can all be supported by editing one string.
PAGE_TEMPLATE = "{base}{path}?q={query}&page={page}"

DEFAULT_QUERY = "data engineer"
MAX_PAGES = 3


# --------------------------------------------------------------------------- #
# HTTP behaviour
# --------------------------------------------------------------------------- #

HEADERS = {
    # A descriptive UA is both polite and less likely to be blocked than a
    # spoofed browser string. The URL is how a site admin who sees this bot in
    # their access log works out who is behind it — so it must stay reachable.
    # A dead link here is worse than no link: it reads as feigned courtesy.
    "User-Agent": (
        "JobMarketScraper/1.0 (+https://github.com/AnthonyNgata/job-market-scraper)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# (connect timeout, read timeout) in seconds. Splitting them means a server
# that accepts the socket but stalls on the body still fails fast.
REQUEST_TIMEOUT = (5, 15)

MAX_RETRIES = 3          # attempts per URL before giving up on that page
BACKOFF_FACTOR = 1.5     # exponential: 1.5s, 3.0s, 6.0s ...
REQUEST_DELAY = 1.0      # polite pause between successful requests (seconds)

# Status codes worth retrying: transient rate limits and gateway errors.
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- #
# CSS selectors
# --------------------------------------------------------------------------- #
#
# Each field lists FALLBACK selectors tried in order. Job boards change markup
# often and A/B test layouts, so a single selector is fragile; the first match
# wins and a miss degrades to None rather than raising.

SELECTORS = {
    # Selector for the repeating container that wraps one job listing.
    "job_card": [
        "div.job-card",
        "li.job-listing",
        "article[data-job-id]",
    ],
    "title": ["h2.job-title", "h3.title", "a.job-link"],
    "company": ["span.company-name", "div.company", "a[data-company]"],
    "location": ["span.job-location", "div.location", "span[data-location]"],
    "salary": ["span.salary", "div.compensation"],
    "posted": ["time", "span.posted-date"],
    # Container holding discrete skill tags, if the board provides them.
    "skills_container": ["ul.skills", "div.tags", "div.skill-list"],
    "skill_item": ["li", "span.tag", "a.skill"],
    # Free-text blurb; skills are mined from here when there are no tags.
    "description": ["p.job-summary", "div.description", "p.snippet"],
    # Link to the detail page; used to build a stable deduplication key.
    "url": ["a.job-link", "h2.job-title a", "a[href]"],
}

# Attribute on the job card holding the board's own identifier, if present.
JOB_ID_ATTRS = ["data-job-id", "data-id", "id"]


# --------------------------------------------------------------------------- #
# Skill extraction vocabulary
# --------------------------------------------------------------------------- #
#
# A controlled vocabulary beats free-form tag collection: it normalises
# "postgres"/"PostgreSQL"/"psql" into one canonical label so the frequency
# counts in the insights step are actually comparable.
#
# Mapping is {canonical name: [aliases matched case-insensitively]}.

SKILL_VOCABULARY = {
    "Python": ["python", "python3", "py3"],
    "SQL": ["sql", "t-sql", "ansi sql"],
    "PostgreSQL": ["postgresql", "postgres", "psql"],
    "MySQL": ["mysql", "mariadb"],
    "MongoDB": ["mongodb", "mongo"],
    "Apache Spark": ["spark", "pyspark", "apache spark"],
    "Apache Airflow": ["airflow", "apache airflow"],
    "dbt": ["dbt"],
    "Kafka": ["kafka", "apache kafka"],
    "Snowflake": ["snowflake"],
    "BigQuery": ["bigquery", "big query"],
    "Redshift": ["redshift"],
    "AWS": ["aws", "amazon web services"],
    "Azure": ["azure", "microsoft azure"],
    "GCP": ["gcp", "google cloud", "google cloud platform"],
    "Docker": ["docker"],
    "Kubernetes": ["kubernetes", "k8s"],
    "Terraform": ["terraform"],
    "Git": ["git", "github", "gitlab"],
    "Linux": ["linux", "unix", "bash"],
    "Pandas": ["pandas"],
    "NumPy": ["numpy"],
    "scikit-learn": ["scikit-learn", "sklearn", "scikit learn"],
    "TensorFlow": ["tensorflow"],
    "PyTorch": ["pytorch", "torch"],
    "Java": ["java"],
    "Scala": ["scala"],
    "Go": ["golang", "go lang"],
    "JavaScript": ["javascript", "js"],
    "TypeScript": ["typescript", "ts"],
    "React": ["react", "react.js", "reactjs"],
    "Node.js": ["node.js", "nodejs", "node"],
    "Tableau": ["tableau"],
    "Power BI": ["power bi", "powerbi"],
    "Looker": ["looker"],
    "Excel": ["excel"],
    "ETL": ["etl", "elt"],
    "CI/CD": ["ci/cd", "cicd", "continuous integration"],
    "Machine Learning": ["machine learning", "ml"],
}


# --------------------------------------------------------------------------- #
# Cleaning / output
# --------------------------------------------------------------------------- #

# Rows missing any of these are unusable as a listing and get dropped.
REQUIRED_FIELDS = ["job_title", "company"]

# Placeholder written into non-critical text columns that came back empty.
MISSING_PLACEHOLDER = "Unknown"

# Columns whose combination identifies a logically duplicate posting when the
# board gives no stable id (the same role cross-posted, or seen on two pages).
DEDUPE_KEYS = ["job_title_key", "company_key", "location_key"]

TOP_N_SKILLS = 5

# Tokens that mark a listing as remote, checked against title + location.
REMOTE_MARKERS = ["remote", "work from home", "wfh", "distributed", "anywhere"]

CSV_FILENAME = "jobs_cleaned.csv"
JSON_FILENAME = "jobs_cleaned.json"
INSIGHTS_FILENAME = "insights.json"
