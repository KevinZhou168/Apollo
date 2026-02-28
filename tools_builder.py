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

dotenv.load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────────────

HERE         = Path(__file__).parent
TEMPLATE_FILE = HERE / "mcp_template.py"
OUTPUT_DIR   = HERE / "generated_mcps"

# ── Modal image & app ──────────────────────────────────────────────────────────

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("anthropic>=0.40.0", "python-dotenv")
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
"""

def plan_tools(goal: str) -> list[dict]:
    """
    Ask Claude to decompose a high-level goal into [{slug, prompt}, …].
    Runs locally before any Modal work begins.
    """
    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        system=PLANNER_SYSTEM,
        messages=[{"role": "user", "content": f"Goal: {goal}"}],
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[^\n]*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)
    return json.loads(raw)


# ── Modal worker (runs in parallel on Modal) ───────────────────────────────────

@app.function(
    image=image,
    secrets=[modal.Secret.from_name("anthropic-secret")],
    timeout=300,
)
def build_one_mcp(prompt: str, template: str) -> str:
    """
    Runs on a Modal worker.  Generates one complete MCP server and returns its source.
    """
    import sys
    sys.path.insert(0, "/root")
    from mcp_builder import generate  # noqa: PLC0415
    return generate(prompt, template)


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

    # ── 4. Generate in parallel on Modal (with syntax-error retry) ────────────
    print(f"\n Generating {len(tools)} MCP server(s) in parallel on Modal …")
    args = [(t["prompt"], template) for t in tools]
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
