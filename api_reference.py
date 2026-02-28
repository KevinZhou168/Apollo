"""
api_reference.py — Public-APIs fallback knowledge base.

Fetches, parses, and caches the public-apis/public-apis GitHub repo
(https://github.com/public-apis/public-apis) as structured JSON.

Provides search and ranking functions so the MCP Builder can consult
real, curated APIs when the LLM doesn't confidently generate one.

Usage:
    from api_reference import search_apis, get_best_apis, refresh_cache

    # Search by keyword (fuzzy category + description match)
    results = search_apis("weather forecast")

    # Get ranked candidates for a capability
    ranked = get_best_apis("weather forecast", top_n=5)

    # Force-refresh the local cache from GitHub
    refresh_cache()
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional
from difflib import SequenceMatcher

# ── Config ────────────────────────────────────────────────────────────────────

HERE = Path(__file__).parent
CACHE_DIR = HERE / "api_reference_data"
CACHE_FILE = CACHE_DIR / "public_apis.json"
CATEGORIES_FILE = CACHE_DIR / "categories.json"
RAW_MD_FILE = CACHE_DIR / "README.md"

# Cache expires after 7 days (seconds)
CACHE_TTL = 7 * 24 * 60 * 60

GITHUB_RAW_URL = (
    "https://raw.githubusercontent.com/public-apis/public-apis/master/README.md"
)

# ── Data model ────────────────────────────────────────────────────────────────

class APIEntry:
    """Structured representation of one API from the public-apis repo."""

    def __init__(
        self,
        name: str,
        description: str,
        auth: str,
        https: bool,
        cors: str,
        link: str,
        category: str,
    ):
        self.name = name
        self.description = description
        self.auth = auth          # "No", "apiKey", "OAuth", "User-Agent", "X-Mashape-Key"
        self.https = https
        self.cors = cors          # "Yes", "No", "Unknown"
        self.link = link
        self.category = category

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "auth": self.auth,
            "https": self.https,
            "cors": self.cors,
            "link": self.link,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "APIEntry":
        return cls(**d)

    def __repr__(self) -> str:
        return f"APIEntry({self.name!r}, category={self.category!r})"


# ── Markdown parser ──────────────────────────────────────────────────────────

def _parse_markdown(md_text: str) -> list[APIEntry]:
    """
    Parse the public-apis README.md markdown into a list of APIEntry objects.

    The format is:
        ### <Category>
        API | Description | Auth | HTTPS | CORS |
        |:---|:---|:---|:---|:---|
        | [Name](link) | Description | Auth | Yes/No | Yes/No/Unknown |
    """
    entries: list[APIEntry] = []
    current_category = ""

    for line in md_text.splitlines():
        line = line.strip()

        # Detect category headers: ### Category Name
        if line.startswith("### "):
            current_category = line[4:].strip()
            continue

        # Skip non-table lines or header/separator rows
        if not line.startswith("|") or not current_category:
            continue

        # Skip table header rows
        if "API" in line and "Description" in line and "Auth" in line:
            continue
        if re.match(r"^\|[\s:|-]+\|$", line):
            continue

        # Parse table row: | [Name](link) | Description | Auth | HTTPS | CORS |
        cells = [c.strip() for c in line.split("|")]
        # Remove empty leading/trailing cells from the split
        cells = [c for c in cells if c]

        if len(cells) < 5:
            continue

        # Extract name and link from first cell: [Name](link)
        name_match = re.match(r"\[(.+?)\]\((.+?)\)", cells[0])
        if not name_match:
            continue

        name = name_match.group(1)
        link = name_match.group(2)
        description = cells[1]
        auth = cells[2].strip("`").strip()
        https_raw = cells[3].strip()
        cors = cells[4].strip()

        entries.append(
            APIEntry(
                name=name,
                description=description,
                auth=auth if auth != "No" else "None",
                https=https_raw.lower() == "yes",
                cors=cors,
                link=link,
                category=current_category,
            )
        )

    return entries


# ── Cache management ─────────────────────────────────────────────────────────

def _cache_is_fresh() -> bool:
    """Check if the local cache exists and isn't expired."""
    if not CACHE_FILE.exists():
        return False
    age = time.time() - CACHE_FILE.stat().st_mtime
    return age < CACHE_TTL


def _fetch_readme() -> str:
    """Fetch the README.md from GitHub."""
    import requests
    resp = requests.get(GITHUB_RAW_URL, timeout=30)
    resp.raise_for_status()
    return resp.text


def refresh_cache() -> list[APIEntry]:
    """
    Fetch the latest public-apis README from GitHub, parse it,
    and save the structured data locally.

    Returns the parsed list of APIEntry objects.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    print("  [api_reference] Fetching public-apis README from GitHub …")
    md_text = _fetch_readme()
    RAW_MD_FILE.write_text(md_text)

    print("  [api_reference] Parsing markdown …")
    entries = _parse_markdown(md_text)

    # Save structured JSON
    data = [e.to_dict() for e in entries]
    CACHE_FILE.write_text(json.dumps(data, indent=2))

    # Save category index
    categories: dict[str, int] = {}
    for e in entries:
        categories[e.category] = categories.get(e.category, 0) + 1
    CATEGORIES_FILE.write_text(json.dumps(categories, indent=2, sort_keys=True))

    print(f"  [api_reference] Cached {len(entries)} APIs across {len(categories)} categories.")
    return entries


def load_apis() -> list[APIEntry]:
    """Load APIs from cache, refreshing if stale or missing."""
    if _cache_is_fresh():
        data = json.loads(CACHE_FILE.read_text())
        return [APIEntry.from_dict(d) for d in data]
    return refresh_cache()


def list_categories() -> dict[str, int]:
    """Return a dict of {category: count} from the cached data."""
    apis = load_apis()
    cats: dict[str, int] = {}
    for a in apis:
        cats[a.category] = cats.get(a.category, 0) + 1
    return cats


# ── Search & ranking ─────────────────────────────────────────────────────────

def _similarity(a: str, b: str) -> float:
    """Case-insensitive similarity ratio between two strings."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _keyword_match_score(query: str, entry: APIEntry) -> float:
    """
    Score an API entry against a search query.
    Checks category match, name match, and description keyword overlap.
    """
    query_lower = query.lower()
    query_words = set(query_lower.split())

    score = 0.0

    # Category match (strongest signal)
    cat_sim = _similarity(query_lower, entry.category.lower())
    if cat_sim > 0.6:
        score += 5.0 * cat_sim

    # Any query word appears in category name
    cat_lower = entry.category.lower()
    for w in query_words:
        if w in cat_lower:
            score += 3.0

    # Name match
    name_sim = _similarity(query_lower, entry.name.lower())
    score += 2.0 * name_sim

    # Description keyword overlap
    desc_lower = entry.description.lower()
    desc_words = set(desc_lower.split())
    overlap = query_words & desc_words
    score += 1.5 * len(overlap)

    # Substring match in description
    if query_lower in desc_lower:
        score += 2.0

    return score


def _authority_score(entry: APIEntry) -> float:
    """
    Rank by 'authority' heuristics:
      - HTTPS support
      - Auth type (explicit apiKey > OAuth > none for reliability)
      - .gov / .int / .edu domains
      - CORS support
    """
    score = 0.0

    # HTTPS
    if entry.https:
        score += 2.0

    # Auth type preferences (free/easy to use preferred for hackathon)
    auth_scores = {
        "None": 3.0,       # No auth = easiest to use
        "apiKey": 2.0,     # apiKey = still easy, more reliable
        "User-Agent": 2.5, # Just needs a header
        "OAuth": 0.5,      # OAuth = complex setup
        "X-Mashape-Key": 1.0,
    }
    score += auth_scores.get(entry.auth, 1.0)

    # Official / authoritative domains
    link_lower = entry.link.lower()
    if ".gov" in link_lower or ".gov." in link_lower:
        score += 3.0
    if ".int" in link_lower:
        score += 3.0
    if ".edu" in link_lower:
        score += 2.0
    if ".org" in link_lower:
        score += 1.0

    # CORS support
    if entry.cors == "Yes":
        score += 1.0

    return score


def search_apis(
    query: str,
    category: Optional[str] = None,
    require_https: bool = False,
    auth_filter: Optional[str] = None,
    top_n: int = 20,
) -> list[APIEntry]:
    """
    Search the public-apis database by keyword.

    Args:
        query:         Free-text search (matched against category, name, description).
        category:      Optional exact category filter (e.g. "Weather").
        require_https: If True, only return APIs with HTTPS support.
        auth_filter:   If set, only return APIs with this auth type ("None", "apiKey", etc).
        top_n:         Max results to return. Defaults to 20.

    Returns:
        List of APIEntry objects sorted by relevance.
    """
    apis = load_apis()

    # Apply hard filters
    if category:
        apis = [a for a in apis if a.category.lower() == category.lower()]
    if require_https:
        apis = [a for a in apis if a.https]
    if auth_filter:
        apis = [a for a in apis if a.auth.lower() == auth_filter.lower()]

    # Score and rank
    scored = []
    for a in apis:
        relevance = _keyword_match_score(query, a)
        authority = _authority_score(a)
        total = relevance + authority
        if relevance > 0.5:  # filter out completely irrelevant
            scored.append((total, a))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [a for _, a in scored[:top_n]]


def get_best_apis(
    capability: str,
    top_n: int = 5,
    prefer_no_auth: bool = True,
) -> list[dict]:
    """
    Get the best API candidates for a given capability description.
    Returns structured dicts ready for the MCP Builder to consume.

    Args:
        capability: Description of what you need (e.g. "weather forecast data").
        top_n:      Number of candidates to return.
        prefer_no_auth: Boost APIs that don't require authentication.

    Returns:
        List of dicts with keys: name, description, auth, https, link, category, score.
    """
    apis = load_apis()

    scored = []
    for a in apis:
        relevance = _keyword_match_score(capability, a)
        authority = _authority_score(a)

        # Boost no-auth APIs if preferred
        if prefer_no_auth and a.auth == "None":
            authority += 2.0

        total = relevance + authority
        if relevance > 0.5:
            scored.append((total, a))

    scored.sort(key=lambda x: x[0], reverse=True)

    results = []
    for total_score, a in scored[:top_n]:
        d = a.to_dict()
        d["score"] = round(total_score, 2)
        results.append(d)

    return results


def format_api_context(apis: list[dict]) -> str:
    """
    Format a list of API dicts into a string that can be injected into
    an LLM prompt as additional context about available APIs.
    """
    if not apis:
        return ""

    lines = [
        "The following real, curated APIs are available and known to work.",
        "Use one of these instead of inventing endpoints:\n",
    ]
    for i, api in enumerate(apis, 1):
        auth_info = f"Auth: {api['auth']}" if api['auth'] != 'None' else "No auth required"
        lines.append(
            f"  {i}. {api['name']} — {api['description']}\n"
            f"     Link: {api['link']}\n"
            f"     Category: {api['category']} | {auth_info} | "
            f"HTTPS: {'Yes' if api['https'] else 'No'}"
        )

    lines.append(
        "\nPrefer the top-ranked API unless a lower-ranked one fits the use case better."
    )
    return "\n".join(lines)


# ── Doc scraping (fallback level 2) ──────────────────────────────────────────

# Max characters of doc text to include in the LLM prompt per API
DOC_MAX_CHARS = 3000

# Jina Reader converts any URL to clean markdown (free, no API key)
JINA_READER_PREFIX = "https://r.jina.ai/"

# Common paths where OpenAPI / Swagger specs live
OPENAPI_PATHS = [
    "/openapi.json",
    "/swagger.json",
    "/api/openapi.json",
    "/v1/openapi.json",
    "/api/v1/openapi.json",
    "/api-docs",
    "/docs/openapi.json",
]


def _try_openapi_spec(base_url: str, timeout: int = 8) -> str:
    """
    Try to find an OpenAPI/Swagger spec at common paths.
    Returns the spec text if found, empty string otherwise.
    """
    import requests
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    for path in OPENAPI_PATHS:
        try:
            resp = requests.get(
                origin + path,
                timeout=timeout,
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 200 and ("json" in resp.headers.get("Content-Type", "")):
                print(f"  [api_reference]   ✓ Found OpenAPI spec at {origin + path}")
                return resp.text[:DOC_MAX_CHARS]
        except Exception:
            continue
    return ""


def _fetch_via_jina(url: str, timeout: int = 15) -> str:
    """
    Use Jina Reader to convert a URL into clean markdown.
    Free, no API key, returns well-structured text.
    """
    import requests

    jina_url = JINA_READER_PREFIX + url
    try:
        resp = requests.get(jina_url, timeout=timeout, headers={
            "Accept": "text/markdown",
        })
        resp.raise_for_status()
        return resp.text[:DOC_MAX_CHARS]
    except Exception:
        return ""


def _html_to_text(html: str) -> str:
    """Fallback: strip HTML tags and collapse whitespace into plain text."""
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                          ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]:
        text = text.replace(entity, char)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n", "\n\n", text)
    return text.strip()


def scrape_api_docs(url: str, timeout: int = 15) -> str:
    """
    Fetch an API's documentation using a 3-step strategy:

    1. Try to find an OpenAPI/Swagger spec (exact endpoint info)
    2. Use Jina Reader for clean markdown (best for HTML docs)
    3. Fall back to raw HTTP fetch + HTML stripping

    Args:
        url:     The API documentation URL.
        timeout: Request timeout in seconds.

    Returns:
        Documentation text, truncated to DOC_MAX_CHARS.
        Returns empty string on total failure.
    """
    import requests

    # Step 1: Try OpenAPI spec
    spec = _try_openapi_spec(url, timeout=timeout)
    if spec:
        return spec

    # Step 2: Try Jina Reader (clean markdown)
    jina_text = _fetch_via_jina(url, timeout=timeout)
    if jina_text and len(jina_text) > 100:
        return jina_text

    # Step 3: Raw fallback
    try:
        resp = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (compatible; AtlasMCPBuilder/1.0)"
        })
        resp.raise_for_status()
        text = _html_to_text(resp.text)
        return text[:DOC_MAX_CHARS]
    except Exception:
        return ""


def scrape_docs_for_apis(apis: list[dict], max_apis: int = 3) -> dict[str, str]:
    """
    Scrape documentation for the top N API candidates.

    Args:
        apis:     List of API dicts (from get_best_apis).
        max_apis: Max number of APIs to scrape (to limit latency).

    Returns:
        Dict mapping API name -> scraped doc text.
        Only includes APIs where scraping succeeded.
    """
    docs: dict[str, str] = {}
    for api in apis[:max_apis]:
        print(f"  [api_reference] Scraping docs for {api['name']} → {api['link']} …")
        text = scrape_api_docs(api["link"])
        if text and len(text) > 100:
            docs[api["name"]] = text
            print(f"  [api_reference]   ✓ Got {len(text)} chars of documentation")
        else:
            print(f"  [api_reference]   ✗ No useful content")
    return docs


def format_api_context_with_docs(apis: list[dict], docs: dict[str, str]) -> str:
    """
    Format API candidates WITH their scraped documentation into a string
    for injection into the LLM prompt. This gives the LLM actual endpoint
    info instead of relying on training data.

    Args:
        apis: List of API dicts (from get_best_apis).
        docs: Dict mapping API name -> scraped doc text.

    Returns:
        Formatted string with API info and documentation excerpts.
    """
    if not apis:
        return ""

    lines = [
        "The following real, curated APIs are available and known to work.",
        "Use one of these instead of inventing endpoints.",
        "Documentation excerpts are provided — use them to construct correct API calls.\n",
    ]
    for i, api in enumerate(apis, 1):
        auth_info = f"Auth: {api['auth']}" if api['auth'] != 'None' else "No auth required"
        lines.append(
            f"  {i}. {api['name']} — {api['description']}\n"
            f"     Link: {api['link']}\n"
            f"     Category: {api['category']} | {auth_info} | "
            f"HTTPS: {'Yes' if api['https'] else 'No'}"
        )
        # Append scraped docs if available
        if api["name"] in docs:
            doc_text = docs[api["name"]]
            lines.append(f"\n     --- Documentation excerpt for {api['name']} ---")
            # Indent doc text for readability
            for doc_line in doc_text.splitlines()[:60]:  # max 60 lines
                lines.append(f"     {doc_line}")
            lines.append(f"     --- End documentation ---\n")

    lines.append(
        "\nUse the documentation above to construct correct API requests. "
        "Prefer the top-ranked API unless a lower-ranked one fits better."
    )
    return "\n".join(lines)


# ── CLI for testing ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = "weather"

    print(f"\n🔍 Searching for: {query!r}\n")

    results = get_best_apis(query, top_n=10)
    if not results:
        print("  No results found.")
    else:
        for r in results:
            print(f"  [{r['score']:5.1f}]  {r['name']:25s}  {r['category']:20s}  "
                  f"auth={r['auth']:10s}  {r['link']}")

    print(f"\n📊 Total categories: {len(list_categories())}")
    print(f"📊 Total APIs cached: {len(load_apis())}")
