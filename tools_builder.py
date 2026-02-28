"""
tools_builder.py — Generate a suite of Modal MCP servers in parallel from a high-level goal.

Usage:
    modal run tools_builder.py --goal "plan a trip to spain"
    modal run tools_builder.py  # prompts interactively if --goal is omitted

How it works:
    1. Claude breaks your goal into several specific MCP server descriptions.
    2. Each description is sent to a Modal worker in parallel.
    3. Every worker calls Claude to generate a complete, ready-to-deploy MCP server.
    4. Files are saved to ./generated_mcps/ as  1_<slug>_mcp.py, 2_<slug>_mcp.py, …
    5. Each file is deployed with `modal deploy`.

Prerequisites:
    - ANTHROPIC_API_KEY set locally (for the planning step).
    - A Modal secret named "anthropic-secret" with ANTHROPIC_API_KEY
      (for the parallel generation workers).
      Create it once with:
          modal secret create anthropic-secret ANTHROPIC_API_KEY=sk-ant-…
"""

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import anthropic
import modal
import dotenv
import os

dotenv.load_dotenv()

# ── Public-APIs fallback reference ─────────────────────────────────────────────
try:
    from api_reference import (
        get_best_apis, format_api_context,
        scrape_docs_for_apis, format_api_context_with_docs,
    )
    HAS_API_REFERENCE = True
except ImportError:
    HAS_API_REFERENCE = False

# ── Confidence assessment (same heuristics as mcp_builder) ─────────────────────

_SUSPICIOUS_DOMAINS = ["example.com", "fakeapi", "placeholder", "dummyapi", "mockapi"]

def _assess_confidence(code: str) -> dict:
    """Quick confidence check on generated code (runs locally, no LLM)."""
    import re
    score, reasons = 1.0, []
    if not valid_syntax(code):
        score -= 0.5; reasons.append("syntax errors")
    if not any(re.search(p, code) for p in [r"requests\.(get|post|put|patch|delete)", r"httpx\.", r"aiohttp\."]):
        score -= 0.3; reasons.append("no API calls")
    if any(d in code.lower() for d in _SUSPICIOUS_DOMAINS):
        score -= 0.4; reasons.append("suspicious URLs")
    return {"confident": score >= 0.7 and len(reasons) == 0, "score": max(0.0, score), "reasons": reasons}

# ── Configuration ──────────────────────────────────────────────────────────────

LLM_PROVIDER = os.getenv("LLM_PROVIDER","openai").lower()
if LLM_PROVIDER not in ("anthropic", "openai"):
    print(f"Error: LLM_PROVIDER must be 'anthropic' or 'openai', got '{LLM_PROVIDER}'", file=sys.stderr)
    sys.exit(1)
print(f"Using LLM Provider: {LLM_PROVIDER}")

# ── Paths ──────────────────────────────────────────────────────────────────────

HERE         = Path(__file__).parent
TEMPLATE_FILE = HERE / "mcp_template.py"
OUTPUT_DIR   = HERE / "generated_mcps"

# ── Modal image & app ──────────────────────────────────────────────────────────

secrets_list = [modal.Secret.from_name("anthropic-secret")]
if LLM_PROVIDER == "openai":
    secrets_list = [modal.Secret.from_name("openai-secret")]

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("anthropic>=0.40.0", "openai>=1.0.0", "python-dotenv")
    .add_local_file(HERE / "mcp_builder.py", remote_path="/root/mcp_builder.py")
)

app = modal.App("tool-builder")

# ── Planner (runs locally) ─────────────────────────────────────────────────────

PLANNER_SYSTEM = """You are an expert at decomposing a high-level user goal into a set of focused MCP (Model Context Protocol) servers.

Given a goal, output a JSON array.  Each element is an object with exactly two keys:
  "slug"   — a short snake_case identifier for the server (e.g. "weather", "flight_search")
  "prompt" — a detailed, specific prompt (3-5 sentences) describing what that MCP server
             should do and which tools it needs.  The prompt will be fed directly to an
             MCP code-generation AI, so be precise about tool names, parameters, return
             values, and which public APIs to use (prefer free, key-less APIs where possible).

Rules:
- Output ONLY valid JSON — no markdown fences, no commentary.
- Produce between 2 and 6 servers; pick the most impactful ones for the goal.
- Each server should have a single, coherent responsibility.
- Do not repeat functionality across servers.
- ALWAYS specify a concrete, real public API for each server to use. Do NOT leave the API choice vague.
"""

def plan_tools(goal: str) -> list[dict]:
    """
    Ask Claude to decompose a high-level goal into [{slug, prompt}, …].
    Runs locally before any Modal work begins.
    """
    # client = anthropic.Anthropic()
    # message = client.messages.create(
    #     model="claude-opus-4-6",
    #     max_tokens=2048,
    #     system=PLANNER_SYSTEM,
    #     messages=[{"role": "user", "content": f"Goal: {goal}"}],
    # )
    # raw = message.content[0].text.strip()
    if LLM_PROVIDER == "anthropic":
        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2048,
            system=PLANNER_SYSTEM,
            messages=[{"role": "user", "content": f"Goal: {goal}"}],
        )
        raw = message.content[0].text.strip()
    else:  # openai
        import openai
        client = openai.OpenAI()
        message = client.chat.completions.create(
            model="gpt-4-turbo",
            max_tokens=2048,
            messages=[
                {"role": "system", "content": PLANNER_SYSTEM},
                {"role": "user", "content": f"Goal: {goal}"},
            ],
        )
        raw = message.choices[0].message.content.strip()

    # ── Robustly parse JSON from the LLM output ──────────────────────────
    # Strip markdown code fences
    if raw.startswith("```"):
        raw = re.sub(r"^```[^\n]*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)

    # Extract just the JSON array if there's extra text around it
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if match:
        raw = match.group(0)

    # Remove trailing commas before ] or } (common LLM mistake)
    raw = re.sub(r",\s*([}\]])", r"\1", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  [plan_tools] JSON parse failed: {e}")
        print(f"  [plan_tools] Raw output (first 500 chars): {raw[:500]}")
        raise


# ── Modal worker (runs in parallel on Modal) ───────────────────────────────────

@app.function(
    image=image,
    secrets=secrets_list,
    timeout=300,
)
def build_one_mcp(prompt: str, template: str, api_context: str = "") -> str:
    """
    Runs on a Modal worker.  Generates one complete MCP server and returns its source.
    If api_context is provided, it will be injected into the system prompt so the LLM
    uses real, curated APIs from the public-apis reference instead of hallucinating.
    """
    import sys
    import os
    sys.path.insert(0, "/root")
    os.environ["LLM_PROVIDER"] = LLM_PROVIDER
    from mcp_builder import generate  # noqa: PLC0415
    return generate(prompt, template, api_context=api_context if api_context else None)


# ── Syntax validation (runs locally) ──────────────────────────────────────────

MAX_RETRIES = 2

def valid_syntax(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


# ── Endpoint-limit management (runs locally) ──────────────────────────────────

ENDPOINT_LIMIT = 8

def _deployed_apps() -> list[dict]:
    """Return deployed apps sorted oldest-first via `modal app list --json`."""
    result = subprocess.run(
        ["modal", "app", "list", "--json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    try:
        apps = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    deployed = [a for a in apps if a.get("State") == "deployed"]
    deployed.sort(key=lambda a: a.get("Created at", ""))
    return deployed


def make_room_for(n: int) -> None:
    """
    Stop the oldest deployed apps if deploying `n` new ones would exceed
    the Modal free-tier web-endpoint limit.
    """
    deployed = _deployed_apps()
    excess = len(deployed) + n - ENDPOINT_LIMIT
    if excess <= 0:
        return

    to_stop = deployed[:excess]
    print(f"\n  At the {ENDPOINT_LIMIT}-endpoint limit. "
          f"Stopping {len(to_stop)} oldest app(s) to make room:")
    for app in to_stop:
        name = app.get("Description", app.get("App ID", "?"))
        app_id = app.get("App ID", "")
        print(f"    Stopping: {name} ({app_id})")
        subprocess.run(["modal", "app", "stop", app_id], check=False)


# ── Deploy helper (runs locally) ───────────────────────────────────────────────

def deploy_file(filepath: Path) -> bool:
    if not shutil.which("modal"):
        print(f"    [!] `modal` CLI not found — skipping deploy of {filepath.name}")
        return False
    print(f"    Deploying {filepath.name} …")
    result = subprocess.run(["modal", "deploy", str(filepath)])
    return result.returncode == 0


# ── Entrypoint ─────────────────────────────────────────────────────────────────

@app.local_entrypoint()
def main(goal: str = ""):
    # ── 1. Get goal ────────────────────────────────────────────────────────────
    if not goal:
        print("Describe your high-level goal (e.g. 'plan a trip to spain'):")
        goal = input("Goal: ").strip()
    if not goal:
        print("No goal provided.", file=sys.stderr)
        sys.exit(1)

    print(f"\n Goal: {goal}")

    # ── 2. Plan tools ──────────────────────────────────────────────────────────
    print("\n Planning MCP servers …")
    tools = plan_tools(goal)

    print(f"\n Will build {len(tools)} MCP server(s):")
    for i, t in enumerate(tools, 1):
        print(f"   {i}. {t['slug']}")
        print(f"      {t['prompt'][:120]}{'…' if len(t['prompt']) > 120 else ''}")

    # ── 3. Load template once; pass to all workers ─────────────────────────────
    if not TEMPLATE_FILE.exists():
        print(f"Error: template not found at {TEMPLATE_FILE}", file=sys.stderr)
        sys.exit(1)
    template = TEMPLATE_FILE.read_text()

    # ── 3b. Resolve curated API candidates for each tool ──────────────────────
    api_contexts: list[str] = []
    if HAS_API_REFERENCE:
        print("\n 🔍 Looking up curated APIs from public-apis reference …")
        for t in tools:
            candidates = get_best_apis(t["prompt"], top_n=5)
            if candidates:
                ctx = format_api_context(candidates)
                api_contexts.append(ctx)
                print(f"   {t['slug']}: found {len(candidates)} API(s) → "
                      f"{', '.join(c['name'] for c in candidates[:3])}")
            else:
                api_contexts.append("")
                print(f"   {t['slug']}: no curated APIs found, using LLM knowledge")
    else:
        print("\n ⚠ api_reference not available — using LLM knowledge only")
        api_contexts = [""] * len(tools)

    # ── 4. Generate in parallel on Modal (with syntax-error retry) ────────────
    print(f"\n Generating {len(tools)} MCP server(s) in parallel on Modal …")
    args = [(t["prompt"], template, ctx) for t, ctx in zip(tools, api_contexts)]
    generated_codes = list(build_one_mcp.starmap(args))

    for attempt in range(1, MAX_RETRIES + 1):
        bad = [i for i, code in enumerate(generated_codes) if not valid_syntax(code)]
        if not bad:
            break
        print(f"\n  {len(bad)} server(s) had syntax errors — retrying (attempt {attempt}/{MAX_RETRIES}) …")
        retry_results = list(build_one_mcp.starmap([args[i] for i in bad]))
        for i, code in zip(bad, retry_results):
            generated_codes[i] = code

    # report any that still fail after all retries
    still_bad = [i for i, code in enumerate(generated_codes) if not valid_syntax(code)]
    if still_bad:
        print(f"\n  WARNING: {len(still_bad)} server(s) still have syntax errors after retries:")
        for i in still_bad:
            print(f"    - {tools[i]['slug']}")

    # ── 4b. Confidence check + doc-scraping retry ─────────────────────────────
    if HAS_API_REFERENCE:
        print(f"\n 🔎 Checking confidence of generated code …")
        low_confidence = []
        for i, code in enumerate(generated_codes):
            assessment = _assess_confidence(code)
            if not assessment["confident"]:
                low_confidence.append(i)
                print(f"   ⚠ {tools[i]['slug']}: low confidence "
                      f"(score={assessment['score']:.2f}) — {', '.join(assessment['reasons'])}")
            else:
                print(f"   ✓ {tools[i]['slug']}: confident (score={assessment['score']:.2f})")

        if low_confidence:
            print(f"\n 📄 Scraping API docs for {len(low_confidence)} low-confidence tool(s) …")
            for i in low_confidence:
                # Get API candidates for this tool
                candidates = get_best_apis(tools[i]["prompt"], top_n=3)
                if not candidates:
                    print(f"   {tools[i]['slug']}: no API candidates, skipping")
                    continue

                # Scrape actual documentation
                docs = scrape_docs_for_apis(candidates, max_apis=2)
                if not docs:
                    print(f"   {tools[i]['slug']}: doc scraping failed, keeping original")
                    continue

                # Build enriched context with real docs
                docs_context = format_api_context_with_docs(candidates, docs)
                print(f"   {tools[i]['slug']}: got docs for {len(docs)} API(s), re-generating …")

                # Re-generate this single tool with doc context
                retry_code = build_one_mcp.remote(
                    tools[i]["prompt"], template, docs_context
                )

                retry_assessment = _assess_confidence(retry_code)
                if retry_assessment["score"] >= _assess_confidence(generated_codes[i])["score"]:
                    generated_codes[i] = retry_code
                    print(f"   ✓ {tools[i]['slug']}: improved to score={retry_assessment['score']:.2f}")
                else:
                    print(f"   {tools[i]['slug']}: doc retry didn't improve, keeping original")
        else:
            print(f"   All tools passed confidence check!")

    # ── 5. Save files ──────────────────────────────────────────────────────────
    OUTPUT_DIR.mkdir(exist_ok=True)
    saved_files: list[Path] = []

    print(f"\n Saving to {OUTPUT_DIR}/")
    for i, (tool, code) in enumerate(zip(tools, generated_codes), 1):
        filename = f"{i}_{tool['slug']}_mcp.py"
        filepath = OUTPUT_DIR / filename
        filepath.write_text(code)
        saved_files.append(filepath)
        lines = len(code.splitlines())
        print(f"   Saved: {filename}  ({lines} lines)")

    # ── 6. Deploy all (stop oldest apps if at endpoint limit) ─────────────────
    make_room_for(len(saved_files))
    print(f"\n Deploying all servers …")
    results: list[tuple[str, bool]] = []
    for filepath in saved_files:
        ok = deploy_file(filepath)
        results.append((filepath.name, ok))

    # ── 7. Summary ─────────────────────────────────────────────────────────────
    print("\n" + "━" * 56)
    print("  Summary")
    print("━" * 56)
    for name, ok in results:
        status = "deployed" if ok else "FAILED  "
        print(f"  [{status}] {name}")
    print("━" * 56)
    print(f"\n  Generated files: {OUTPUT_DIR}/")
