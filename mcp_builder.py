"""
mcp_builder.py — Programmatic MCP server generator (library interface).

Designed to be imported and called by other Python programs.
No interactive prompts. Raises exceptions on error instead of sys.exit.

Quick start:
    from mcp_builder import generate, load_template

    template = load_template()
    code = generate("build a weather MCP using the Open-Meteo API", template)
    print(code)
"""

import re
from pathlib import Path
import anthropic

# ── Config ────────────────────────────────────────────────────────────────────

TEMPLATE_FILE = Path(__file__).parent / "mcp_template.py"
MODEL         = "claude-opus-4-6"
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


def generate(prompt: str, template: str | None = None) -> str:
    """
    Generate MCP server code from a plain-English prompt.

    Args:
        prompt:   Description of what the MCP server should do.
        template: The mcp_template.py content as a string. If None,
                  loads from the default TEMPLATE_FILE path.

    Returns:
        Generated Python source code as a string.

    Raises:
        FileNotFoundError: If template is None and TEMPLATE_FILE is missing.
        anthropic.APIError: On API failures.
    """
    if template is None:
        template = load_template()

    client = anthropic.Anthropic()
    system = SYSTEM_PROMPT.format(template=template)

    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[
            {
                "role": "user",
                "content": (
                    "Build an MCP server for the following use case:\n\n"
                    f"{prompt}\n\n"
                    "Remember: output raw Python only, no markdown fences."
                ),
            }
        ],
    )

    return strip_fences(message.content[0].text)
