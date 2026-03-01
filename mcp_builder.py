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
import json
import os
import re
from pathlib import Path
import dotenv

dotenv.load_dotenv()

import anthropic

# ── Config ────────────────────────────────────────────────────────────────────

# LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()
LLM_PROVIDER = "anthropic"
TEMPLATE_FILE = Path(__file__).parent / "mcp_template.py"
CODE_MODEL    = "claude-opus-4-6"   if LLM_PROVIDER == "anthropic" else "gpt-5.2"
HELPER_MODEL  = "claude-sonnet-4-6" if LLM_PROVIDER == "anthropic" else "gpt-4o-mini"
MAX_TOKENS    = 8096  # code generation only

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
    Every string literal must fit on one line (< 120 chars); use triple-quoted strings
    only for docstrings, never for data payloads.

5.  ██ API KEY RULE — this is the most important rule after rule 1 ██
    ALWAYS use free, public APIs that require NO API key unless the prompt explicitly
    says an API key is available or tells you to use a specific authenticated service.

    - If the prompt does not mention an API key: you MUST pick a keyless public API.
    - NEVER use OpenWeatherMap, Google Maps, Yelp, Foursquare, Ticketmaster, or any
      other service that gates access behind a key registration, unless the prompt
      explicitly instructs you to.
    - Preferred keyless APIs (use these by default. if not, use other public apis that require no key):
        weather          → open-meteo.com  (no key, free forever)
        geocoding        → nominatim.openstreetmap.org  (no key)
        maps / places    → overpass-api.de  (OpenStreetMap, no key)
        exchange rates   → open.er-api.com  (no key for latest rates)
        public holidays  → date.nager.at  (no key)
        IP geolocation   → ip-api.com  (no key, HTTP only)
        astronomy        → api.open-notify.org  (no key)
        trivia / facts   → opentdb.com, uselessfacts.jsph.pl  (no key)
        news headlines   → gnews.io free tier requires a key — use rss feeds instead
    - When you choose an API, confirm in a comment that it requires no key.

6.  Do not add Modal secrets, volumes, GPU config, or scheduled functions unless the prompt specifically requires them.

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

def _call_llm(system: str, user_msg: str, model: str | None = None, max_tokens: int | None = None) -> str:
    """Make an LLM call with the given system prompt and user message.

    Args:
        system:     System prompt.
        user_msg:   User message.
        model:      Model to use. Defaults to CODE_MODEL.
        max_tokens: Max output tokens. Defaults to MAX_TOKENS.
    """
    model      = model      or CODE_MODEL
    max_tokens = max_tokens or MAX_TOKENS
    if LLM_PROVIDER == "anthropic":
        client = anthropic.Anthropic()
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        return strip_fences(message.content[0].text)
    else:  # openai
        import openai
        client = openai.OpenAI()
        message = client.chat.completions.create(
            model=model,
            max_completion_tokens=max_tokens,
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
    _y = "\033[93m"; _r = "\033[0m"; _d = "\033[2m"; _g = "\033[92m"; _b = "\033[94m"
    print(f"  {_y}⚠  low confidence{_r}  {_d}score={assessment['score']:.2f}  ·  "
          f"{', '.join(assessment['reasons'])}{_r}")
    print(f"  {_d}↳ consulting public-apis reference…{_r}")

    try:
        from api_reference import (
            get_best_apis, format_api_context,
            scrape_docs_for_apis, format_api_context_with_docs,
        )

        candidates = get_best_apis(prompt, top_n=5)
        if not candidates:
            print(f"  {_d}  no relevant APIs found — keeping original{_r}")
            return code

        # Level 1: retry with just API names/descriptions (no doc scraping)
        ref_context = format_api_context(candidates)
        system_with_ref = system + API_REFERENCE_ADDENDUM.format(api_context=ref_context)

        print(f"  {_b}  found {len(candidates)} API candidate(s) — re-generating…{_r}")
        for c in candidates:
            print(f"  {_d}    → {c['name']} ({c['category']})  {c['link']}{_r}")

        fallback_code = _call_llm(system_with_ref, user_msg)
        fallback_assessment = assess_confidence(fallback_code)

        if fallback_assessment["confident"]:
            print(f"  {_g}  ✓ confidence improved: "
                  f"{assessment['score']:.2f} → {fallback_assessment['score']:.2f}{_r}")
            return fallback_code

        # ── Phase 5: Fallback level 2 — scrape actual API docs ────────────
        print(f"  {_y}  still low confidence ({fallback_assessment['score']:.2f}) "
              f"— scraping API docs…{_r}")

        docs = scrape_docs_for_apis(candidates, max_apis=3)
        if docs:
            docs_context = format_api_context_with_docs(candidates, docs)
            system_with_docs = system + API_REFERENCE_ADDENDUM.format(api_context=docs_context)

            print(f"  {_b}  scraped docs for {len(docs)} API(s) — re-generating…{_r}")

            docs_code = _call_llm(system_with_docs, user_msg)
            docs_assessment = assess_confidence(docs_code)

            # Return the best result across all attempts
            best_code, best_score = code, assessment["score"]
            if fallback_assessment["score"] > best_score:
                best_code, best_score = fallback_code, fallback_assessment["score"]
            if docs_assessment["score"] > best_score:
                best_code, best_score = docs_code, docs_assessment["score"]

            print(f"  {_g}  ✓ best confidence across all attempts: {best_score:.2f}{_r}")
            return best_code
        else:
            print(f"  {_y}  doc scraping failed — keeping best previous result{_r}")
            if fallback_assessment["score"] >= assessment["score"]:
                return fallback_code
            return code

    except ImportError:
        print(f"  {_d}  api_reference not available — using original{_r}")
        return code
    except Exception as e:
        print(f"  {_y}  fallback error: {e} — using original{_r}")
        return code


# ── Test generation ───────────────────────────────────────────────────────────

TEST_SYSTEM_PROMPT = """You are an expert Python developer writing integration tests for a FastMCP server deployed on Modal.

Given the source code of an MCP server, produce a complete Python test script that validates every tool.
The script must only use the Python standard library plus `requests` — do NOT import fastmcp, asyncio, or any other package.

CALLING TOOLS — use this exact helper (copy it verbatim into every test script):

def call_tool(base_url, tool_name, arguments):
    import requests, json
    resp = requests.post(
        f"{base_url}/mcp/",
        json={"jsonrpc": "2.0", "method": "tools/call",
              "params": {"name": tool_name, "arguments": arguments}, "id": 1},
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        timeout=30,
    )
    resp.raise_for_status()
    ct = resp.headers.get("content-type", "")
    if "event-stream" in ct:
        for line in resp.text.splitlines():
            if line.startswith("data: ") and line.strip() != "data:":
                data = json.loads(line[6:])
                if "result" in data:
                    content = data["result"].get("content", [])
                    return " ".join(c.get("text", str(c)) for c in content)
        return None
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    content = data.get("result", {}).get("content", [])
    return " ".join(c.get("text", str(c)) for c in content)

REQUIREMENTS:
1. Accept the server's base URL as sys.argv[1].
2. Include the call_tool helper above verbatim.
3. Test every @mcp.tool() function at least once with realistic, non-trivial arguments.
4. Print each result clearly: print(f"[tool_name] result: {result}")
5. Catch ALL exceptions per-tool and print them: print(f"[tool_name] ERROR: {e}")
6. Never let a single tool failure crash the whole script — always continue to the next tool.
7. Print a final summary listing which tools passed and which raised errors.
8. Always exit with code 0 — the LLM will judge the output, not the exit code.

STRICT RULES:
- Output ONLY raw Python code. No markdown fences, no explanations.
- Synchronous code only — no async, no asyncio.
- All imports (requests, json, sys) at the top of the file.
- Use realistic test arguments (e.g. "Chicago" for cities, real near-future dates for dates, positive numbers for quantities).
- Never pass empty strings or None unless a parameter has a default value.
"""


def generate_tests(mcp_code: str) -> str:
    """
    Generate an integration test script for a given MCP server's source code.

    Args:
        mcp_code: The full Python source of the generated MCP server.

    Returns:
        A Python test script as a string.
    """
    user = f"Generate integration tests for this MCP server:\n\n{mcp_code}"
    return _call_llm(TEST_SYSTEM_PROMPT, user, model=HELPER_MODEL, max_tokens=4096)


# ── Result interpretation ─────────────────────────────────────────────────────

INTERPRETER_SYSTEM_PROMPT = """You are analyzing integration test output from a deployed MCP server to classify whether it is working correctly.

You will receive:
1. The original prompt used to generate the server.
2. The server's Python source code.
3. The stdout/stderr captured from running tests against the live endpoint.

Classify the outcome as exactly one of three verdicts:

"valid"     — Most tools returned real, sensible data. Minor issues (one flaky tool,
              slightly unexpected format) are acceptable. Mark valid if the server is
              genuinely useful.

"transient" — Failures are clearly due to external factors outside the code: network
              timeouts, third-party API rate limits or downtime, services requiring API
              keys that aren't provided. Retrying with different code won't help.

"code_bug"  — The code itself is broken: import errors, wrong API endpoints, logic
              errors, tools always returning empty/None/error, incorrect argument
              handling. A code fix would resolve this.

Respond with ONLY valid JSON — no markdown, no commentary:
{
  "verdict": "valid" | "transient" | "code_bug",
  "reason": "one or two sentences explaining the classification",
  "adjusted_prompt": "<original prompt with appended fix instructions>" or null
}

Set adjusted_prompt only when verdict is "code_bug": take the original prompt and
append a clear, specific instruction describing what failed and what to fix.
For "valid" and "transient", set adjusted_prompt to null.
"""


def interpret_results(test_output: str, mcp_code: str, original_prompt: str) -> dict:
    """
    Use an LLM to classify test output as valid / transient / code_bug.

    Args:
        test_output:     Captured stdout+stderr from the test script.
        mcp_code:        The MCP server source that was tested.
        original_prompt: The prompt originally used to generate the server.

    Returns:
        Dict with keys: verdict, reason, adjusted_prompt.
    """
    user = (
        f"Original prompt:\n{original_prompt}\n\n"
        f"MCP server code:\n{mcp_code}\n\n"
        f"Test output:\n{test_output}"
    )

    raw = _call_llm(INTERPRETER_SYSTEM_PROMPT, user, model=HELPER_MODEL, max_tokens=1024)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"verdict": "code_bug", "reason": f"Interpreter returned invalid JSON: {raw[:200]}", "adjusted_prompt": None}
