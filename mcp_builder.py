"""
mcp_builder.py — Programmatic MCP server generator (library interface).

Designed to be imported and called by other Python programs.
No interactive prompts. Raises exceptions on error instead of sys.exit.

Quick start:
    from mcp_builder import generate, load_template

    template = load_template()
    code = generate("build a weather MCP using the Open-Meteo API", template)
    print(code)

Fallback mode:
    If the LLM's initial generation doesn't reference a real API
    (or fails validation), the builder automatically consults the
    public-apis reference to find curated, real-world APIs and
    re-generates with that context injected into the prompt.
"""

import ast
import os
import re
from pathlib import Path
import dotenv

dotenv.load_dotenv()

import anthropic

# ── Config ────────────────────────────────────────────────────────────────────

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()
TEMPLATE_FILE = Path(__file__).parent / "mcp_template.py"
MODEL         = "claude-opus-4-6" if LLM_PROVIDER == "anthropic" else "gpt-5.2"
MAX_TOKENS    = 8096

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert Python developer specialising in Modal deployments and MCP (Model Context Protocol) servers built with fastmcp.

Your job: given a description of what an MCP server should do, produce a complete, ready-to-deploy Python file that follows the template below EXACTLY.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRICT RULES — never break these
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1.  Output ONLY raw Python code. No markdown, no code fences, no explanation.
    The very first character of your response must be a Python comment or import.

2.  Follow the template structure exactly:
      - modal.App("app-name")               ← hyphens only, no underscores/spaces
      - modal.Image.debian_slim(python_version="3.12").uv_pip_install(...)
      - Required packages always present: fastapi==0.115.14, fastmcp==2.10.6, pydantic==2.11.10
      - All additional packages pinned to a specific version (e.g. "requests==2.31.0")
      - make_mcp_server() function containing ALL tool definitions and ALL imports
      - web() function IDENTICAL to the template — never modify it

3.  Tool rules:
      - Every tool is an async function decorated with @mcp.tool()
      - Every parameter has a type annotation (str, int, float, bool only)
      - Every tool has a return type annotation (str, int, float, bool, list, dict)
      - Every tool has a complete Google-style docstring (Args + Returns sections)
      - All imports used in a tool live INSIDE that tool's function body
      - Raise ValueError with a clear message for invalid inputs
      - For tools that call external APIs: use `requests` (or another pinned package) and handle HTTP errors explicitly

4.  NEVER hardcode large datasets, lookup tables, or reference data inside a tool body.
    Every tool must fetch live data from an external API or compute values dynamically.
    If no suitable free API exists, make the tool call a well-known public API and note
    which one in the docstring — do not substitute hardcoded dict/list literals.
    Every string literal must fit on one line (< 120 chars); use triple-quoted strings
    only for docstrings, never for data payloads.

5.  Do not add Modal secrets, volumes, GPU config, or scheduled functions unless the prompt specifically requires them.

6.  The make_mcp_server() function must end with `return mcp`.

7.  The web() function must be copied verbatim from the template — do not alter it.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TEMPLATE (follow this structure)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{template}
"""

# Appended to the system prompt when fallback API references are available
API_REFERENCE_ADDENDUM = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURATED API REFERENCE (use these!)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{api_context}

IMPORTANT: You MUST use one of the APIs listed above. These are real, verified APIs.
Do NOT invent or hallucinate API endpoints. Use the exact base URL from the link provided
and consult the API's documentation to construct correct requests.
"""

# ── Public API ────────────────────────────────────────────────────────────────

def load_template(template_file: Path = TEMPLATE_FILE) -> str:
    """Load the MCP template file. Raises FileNotFoundError if missing."""
    path = Path(template_file)
    if not path.exists():
        raise FileNotFoundError(f"Template file not found: {path}")
    return path.read_text()


def strip_fences(code: str) -> str:
    """Remove markdown code fences if the model accidentally included them."""
    code = code.strip()
    if code.startswith("```"):
        code = re.sub(r"^```[^\n]*\n", "", code)
        code = re.sub(r"\n```\s*$", "", code)
    return code.strip()


# ── Confidence checking ──────────────────────────────────────────────────────

# Known suspicious patterns that suggest hallucinated APIs
_SUSPICIOUS_DOMAINS = [
    "example.com",
    "api.example",
    "placeholder",
    "fakeapi",
    "jsonplaceholder",
    "dummyapi",
    "mockapi",
]

# Known real API domains (expanded as we learn more)
_KNOWN_GOOD_DOMAINS = [
    "openweathermap.org",
    "open-meteo.com",
    "api.met.no",
    "weather.gov",
    "restcountries.com",
    "newsapi.org",
    "exchangerate",
    "github.com",
    "api.github.com",
    "googleapis.com",
]


def _has_valid_syntax(code: str) -> bool:
    """Check whether generated code parses as valid Python."""
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _has_api_calls(code: str) -> bool:
    """Check whether the code contains HTTP request calls."""
    api_patterns = [
        r"requests\.(get|post|put|patch|delete)",
        r"httpx\.",
        r"aiohttp\.",
        r"urllib\.request",
        r"fetch\(",
    ]
    return any(re.search(p, code) for p in api_patterns)


def _has_suspicious_urls(code: str) -> bool:
    """Check for hallucinated or placeholder URLs."""
    code_lower = code.lower()
    return any(domain in code_lower for domain in _SUSPICIOUS_DOMAINS)


def _has_hardcoded_data(code: str) -> bool:
    """Detect large hardcoded data structures (dicts/lists > 5 lines)."""
    # Look for dict/list literals spanning many lines
    in_data = False
    data_lines = 0
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            in_data = True
            data_lines = 0
        if in_data:
            data_lines += 1
            if stripped.endswith("}") or stripped.endswith("]"):
                if data_lines > 10:
                    return True
                in_data = False
    return False


def assess_confidence(code: str) -> dict:
    """
    Assess confidence in the generated MCP server code.

    Returns a dict with:
        confident: bool — overall confidence verdict
        score: float — 0.0 (no confidence) to 1.0 (high confidence)
        reasons: list[str] — explanations for low confidence
    """
    score = 1.0
    reasons = []

    if not _has_valid_syntax(code):
        score -= 0.5
        reasons.append("Code has syntax errors")

    if not _has_api_calls(code):
        score -= 0.3
        reasons.append("No HTTP API calls detected — may use hardcoded data")

    if _has_suspicious_urls(code):
        score -= 0.4
        reasons.append("Contains suspicious/placeholder URLs")

    if _has_hardcoded_data(code):
        score -= 0.3
        reasons.append("Contains large hardcoded data structures")

    confident = score >= 0.7 and len(reasons) == 0
    return {
        "confident": confident,
        "score": max(0.0, score),
        "reasons": reasons,
    }


# ── LLM call helpers ─────────────────────────────────────────────────────────

def _call_llm(system: str, user_msg: str) -> str:
    """Make an LLM call with the given system prompt and user message."""
    if LLM_PROVIDER == "anthropic":
        client = anthropic.Anthropic()
        message = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        return strip_fences(message.content[0].text)
    else:  # openai
        import openai
        client = openai.OpenAI()
        message = client.chat.completions.create(
            model=MODEL,
            max_completion_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
        )
        return strip_fences(message.choices[0].message.content)


# ── Main generation with fallback ─────────────────────────────────────────────

def generate(
    prompt: str,
    template: str | None = None,
    use_fallback: bool = True,
    api_context: str | None = None,
) -> str:
    """
    Generate MCP server code from a plain-English prompt.

    Two-phase generation:
      1. Ask the LLM to generate code using its own knowledge.
      2. If confidence is low, look up curated APIs from the public-apis
         repo and re-generate with that context injected.

    Args:
        prompt:       Description of what the MCP server should do.
        template:     The mcp_template.py content as a string. If None,
                      loads from the default TEMPLATE_FILE path.
        use_fallback: Whether to use the public-apis fallback on low confidence.
                      Defaults to True.
        api_context:  Pre-formatted API reference context string. If provided,
                      skips the automatic lookup and uses this directly.
                      Useful when the caller (e.g. tools_builder) has already
                      resolved the relevant APIs.

    Returns:
        Generated Python source code as a string.

    Raises:
        FileNotFoundError: If template is None and TEMPLATE_FILE is missing.
        anthropic.APIError: On API failures.
    """
    if template is None:
        template = load_template()

    user_msg = (
        "Build an MCP server for the following use case:\n\n"
        f"{prompt}\n\n"
        "Remember: output raw Python only, no markdown fences."
    )

    # ── Phase 1: If caller provided API context, use it directly ──────────
    if api_context:
        system = SYSTEM_PROMPT.format(template=template)
        system += API_REFERENCE_ADDENDUM.format(api_context=api_context)
        return _call_llm(system, user_msg)

    # ── Phase 2: Try native LLM generation first ─────────────────────────
    system = SYSTEM_PROMPT.format(template=template)
    code = _call_llm(system, user_msg)

    if not use_fallback:
        return code

    # ── Phase 3: Assess confidence ────────────────────────────────────────
    assessment = assess_confidence(code)

    if assessment["confident"]:
        return code

    # ── Phase 4: Fallback level 1 — API names + links from reference ────────
    print(f"  [mcp_builder] Low confidence (score={assessment['score']:.2f}): "
          f"{', '.join(assessment['reasons'])}")
    print(f"  [mcp_builder] Falling back to public-apis reference …")

    try:
        from api_reference import (
            get_best_apis, format_api_context,
            scrape_docs_for_apis, format_api_context_with_docs,
        )

        candidates = get_best_apis(prompt, top_n=5)
        if not candidates:
            print(f"  [mcp_builder] No relevant APIs found in reference. Using original.")
            return code

        # Level 1: retry with just API names/descriptions (no doc scraping)
        ref_context = format_api_context(candidates)
        system_with_ref = system + API_REFERENCE_ADDENDUM.format(api_context=ref_context)

        print(f"  [mcp_builder] Found {len(candidates)} API candidate(s). Re-generating …")
        for c in candidates:
            print(f"    → {c['name']} ({c['category']}) — {c['link']}")

        fallback_code = _call_llm(system_with_ref, user_msg)
        fallback_assessment = assess_confidence(fallback_code)

        if fallback_assessment["confident"]:
            print(f"  [mcp_builder] Fallback improved confidence: "
                  f"{assessment['score']:.2f} → {fallback_assessment['score']:.2f}")
            return fallback_code

        # ── Phase 5: Fallback level 2 — scrape actual API docs ────────────
        print(f"  [mcp_builder] Level 1 fallback still low confidence "
              f"(score={fallback_assessment['score']:.2f}). Scraping API docs …")

        docs = scrape_docs_for_apis(candidates, max_apis=3)
        if docs:
            docs_context = format_api_context_with_docs(candidates, docs)
            system_with_docs = system + API_REFERENCE_ADDENDUM.format(api_context=docs_context)

            print(f"  [mcp_builder] Scraped docs for {len(docs)} API(s). "
                  f"Re-generating with documentation …")

            docs_code = _call_llm(system_with_docs, user_msg)
            docs_assessment = assess_confidence(docs_code)

            # Return the best result across all attempts
            best_code, best_score = code, assessment["score"]
            if fallback_assessment["score"] > best_score:
                best_code, best_score = fallback_code, fallback_assessment["score"]
            if docs_assessment["score"] > best_score:
                best_code, best_score = docs_code, docs_assessment["score"]

            print(f"  [mcp_builder] Best confidence across all attempts: {best_score:.2f}")
            return best_code
        else:
            print(f"  [mcp_builder] Doc scraping failed. Using best previous result.")
            if fallback_assessment["score"] >= assessment["score"]:
                return fallback_code
            return code

    except ImportError:
        print(f"  [mcp_builder] api_reference module not available. Using original.")
        return code
    except Exception as e:
        print(f"  [mcp_builder] Fallback error: {e}. Using original.")
        return code

