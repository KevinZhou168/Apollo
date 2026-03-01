"""
tools_builder.py — Generate a suite of Modal MCP servers in parallel from a high-level goal.

Usage:
    modal run tools_builder.py --goal "plan a trip to spain"
    modal run tools_builder.py  # prompts interactively if --goal is omitted

How it works:
    1. Claude breaks your goal into several specific MCP server descriptions.
    2. Each description is sent to a Modal worker in parallel.
    3. Every worker calls Claude to generate a complete MCP server AND its test script.
    4. Each server is deployed with `modal deploy`, then its tests are run locally.
    5. An LLM interprets the test results:
         - "valid"     → keep the deployment, save the file.
         - "transient" → external failure (API down, rate-limited); skip without retry.
         - "code_bug"  → stop the app, adjust the prompt, retry (up to MAX_DEPLOY_ATTEMPTS).
    6. After MAX_DEPLOY_ATTEMPTS failures the server is skipped entirely.

Prerequisites:
    - ANTHROPIC_API_KEY set locally (for planning and interpretation steps).
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
import tempfile
from pathlib import Path

import anthropic
import modal
import dotenv
import os

dotenv.load_dotenv()

# ── Live visualization (optional) ─────────────────────────────────────────────
try:
    from viz_server import start_viz_server, emit as viz_emit
    HAS_VIZ = True
except ImportError:
    HAS_VIZ = False
    def viz_emit(event_type: str, data: dict) -> None: pass      # noqa: E704
    def start_viz_server(port: int = 8765, open_browser: bool = True) -> None: pass  # noqa: E704

# ── Terminal styling ───────────────────────────────────────────────────────────
_R   = "\033[0m"   # reset
_B   = "\033[1m"   # bold
_DIM = "\033[2m"   # dim
_C   = "\033[96m"  # bright cyan
_G   = "\033[92m"  # bright green
_Y   = "\033[93m"  # bright yellow
_RE  = "\033[91m"  # bright red
_BL  = "\033[94m"  # bright blue
_M   = "\033[95m"  # bright magenta

def _sec(title: str) -> str:
    """Styled section header."""
    bar = "─" * (50 - len(title) - 1)
    return f"\n{_C}{_B}◆ {title}{_R}{_DIM} {bar}{_R}"

def _mcp_header(idx: int, total: int, slug: str) -> str:
    """Styled per-MCP box header."""
    label = f"  [{idx}/{total}]  {slug}"
    pad   = 50 - len(label)
    return (
        f"\n{_BL}┌{'─' * 52}┐\n"
        f"│{_B}{label}{_R}{_BL}{' ' * pad}│\n"
        f"└{'─' * 52}┘{_R}"
    )

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

# LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()
LLM_PROVIDER = "openai"
if LLM_PROVIDER not in ("anthropic", "openai"):
    print(f"Error: LLM_PROVIDER must be 'anthropic' or 'openai', got '{LLM_PROVIDER}'", file=sys.stderr)
    sys.exit(1)
print(f"{_DIM}  provider · {LLM_PROVIDER}{_R}")

# ── Paths ──────────────────────────────────────────────────────────────────────

HERE          = Path(__file__).parent
TEMPLATE_FILE = HERE / "mcp_template.py"
OUTPUT_DIR    = HERE / "generated_mcps"

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
             should do and which tools it needs. The prompt will be fed directly to an
             MCP code-generation AI, so be precise about tool names, parameters, return
             values, and which public APIs to use.

Rules:
-  ██ API KEY RULE — this is the most important rule after rule 1 ██ ALWAYS use free, public APIs that require NO API key unless the prompt explicitly says an API key is available or tells you to use a specific authenticated service.
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
def build_mcp_and_tests(prompt: str, template: str, api_context: str = "") -> tuple[str, str]:
    """
    Runs on a Modal worker. Generates one complete MCP server and its test script.
    If api_context is provided, it is injected into the system prompt so the LLM
    uses real, curated APIs from the public-apis reference instead of hallucinating.
    Returns (mcp_code, test_code).
    """
    import sys
    import os
    sys.path.insert(0, "/root")
    os.environ["LLM_PROVIDER"] = LLM_PROVIDER
    from mcp_builder import generate, generate_tests  # noqa: PLC0415
    code = generate(prompt, template, api_context=api_context if api_context else None)
    test_code = generate_tests(code)
    return code, test_code


# ── Syntax validation ──────────────────────────────────────────────────────────

MAX_RETRIES = 2  # syntax-error retry passes before the deploy loop

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
    print(f"\n{_Y}  ⚠  endpoint limit reached ({ENDPOINT_LIMIT} max) — "
          f"stopping {len(to_stop)} oldest app(s){_R}")
    for a in to_stop:
        name = a.get("Description", a.get("App ID", "?"))
        app_id = a.get("App ID", "")
        print(f"  {_DIM}  ↓ {name} ({app_id}){_R}")
        subprocess.run(["modal", "app", "stop", app_id], check=False)


# ── Deploy helper (runs locally) ───────────────────────────────────────────────

def deploy_file(filepath: Path) -> tuple[bool, str | None]:
    """Deploy a file with `modal deploy`. Returns (success, endpoint_url)."""
    if not shutil.which("modal"):
        print(f"  {_RE}  ✗  modal CLI not found — skipping {filepath.name}{_R}")
        return False, None
    print(f"  {_DIM}  deploying {filepath.name} …{_R}")
    result = subprocess.run(
        ["modal", "deploy", str(filepath)],
        capture_output=True, text=True,
    )
    output = result.stdout + result.stderr
    print(_DIM + output + _R)
    url = None
    if result.returncode == 0:
        match = re.search(r"https://[a-z0-9-]+--[a-z0-9-]+-web\.modal\.run", output)
        if match:
            url = match.group(0)
    return result.returncode == 0, url


# ── App name helpers (runs locally) ────────────────────────────────────────────

def extract_app_name(code: str) -> str | None:
    """Extract the modal.App name from generated source code."""
    match = re.search(r'modal\.App\(["\']([^"\']+)["\']', code)
    return match.group(1) if match else None


def stop_app_by_name(app_name: str) -> None:
    """Look up a deployed app by name and stop it."""
    for a in _deployed_apps():
        if a.get("Description", "") == app_name:
            app_id = a.get("App ID", "")
            if app_id:
                print(f"  {_DIM}  ↓ stopping {app_name} ({app_id}){_R}")
                subprocess.run(["modal", "app", "stop", app_id], check=False)
                return
    print(f"  {_Y}  ⚠  could not find '{app_name}' to stop{_R}")


# ── Test runner (runs locally) ─────────────────────────────────────────────────

def run_tests_locally(endpoint_url: str, test_code: str) -> str:
    """Write test_code to a temp file, run it with the endpoint URL, return output."""
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(test_code)
        test_file = Path(f.name)
    try:
        result = subprocess.run(
            [sys.executable, str(test_file), endpoint_url],
            capture_output=True, text=True, timeout=60,
        )
        output = result.stdout
        if result.stderr:
            output += f"\nSTDERR:\n{result.stderr}"
        return output
    except subprocess.TimeoutExpired:
        return "Tests timed out after 60 seconds."
    finally:
        test_file.unlink(missing_ok=True)


# ── Entrypoint ─────────────────────────────────────────────────────────────────

MAX_DEPLOY_ATTEMPTS = 2  # 1 retry per MCP (attempt 1 + attempt 2)

@app.local_entrypoint()
def main(goal: str = ""):
    # ── Viz node layout constants ───────────────────────────────────────────────
    _VIZ_PLANET_TYPES = ['mars', 'earth', 'venus', 'neptune', 'mercury', 'uranus']
    _VIZ_AGENT_COLORS = ['#c1440e', '#4488cc', '#d4a84b', '#4466aa', '#8a8a8a', '#88ccdd']
    _VIZ_SRV_PLANETS  = ['uranus', 'neptune', 'earth', 'jupiter', 'mars', 'saturn']
    _VIZ_SRV_COLORS   = ['#88ccdd', '#4361ee', '#4488cc', '#C88B3A', '#c1440e', '#7b2cbf']

    # ── 1. Get goal ────────────────────────────────────────────────────────────
    print(f"\n{_C}{_B}╔══════════════════════════════════════════════════╗{_R}")
    print(f"{_C}{_B}║  ⚡  ATLAS  ·  MCP Server Factory                ║{_R}")
    print(f"{_C}{_B}╚══════════════════════════════════════════════════╝{_R}")
    start_viz_server(port=8765)
    if not goal:
        print(f"\n{_B}  Describe your goal{_R} {_DIM}(e.g. 'plan a trip to spain'){_R}")
        goal = input(f"{_C}  › {_R}").strip()
    if not goal:
        print(f"{_RE}  ✗  no goal provided{_R}", file=sys.stderr)
        sys.exit(1)

    print(f"\n  {_B}goal{_R}  {goal}")
    viz_emit("phase", {"text": f"Goal: {goal}"})
    viz_emit("node", {
        "id": "prompt", "label": goal, "bt": "sun",
        "color": "#FDB813", "r": 32, "ix": 0, "iy": -240,
        "desc": "User prompt — the high-level goal",
        "info": goal,
    })
    viz_emit("phase", {"text": "Planning MCP servers…"})

    # ── 2. Plan tools ──────────────────────────────────────────────────────────
    print(_sec("PLANNING"))
    print(f"  decomposing goal into MCP servers…")
    tools = plan_tools(goal)
    N = len(tools)

    print(f"\n  {_B}{len(tools)} server(s){_R} to build:")
    for i, t in enumerate(tools, 1):
        print(f"  {_C}  {i}{_R}  {_B}{t['slug']}{_R}")
        print(f"  {_DIM}     {t['prompt'][:120]}{'…' if len(t['prompt']) > 120 else ''}{_R}")

    # Emit supervisor, builder, and agent nodes
    viz_emit("node", {
        "id": "supervisor", "label": "Supervisor", "bt": "planet", "pt": "saturn",
        "color": "#f0a500", "r": 26, "ix": 0, "iy": -160,
        "desc": "Orchestrates the pipeline and decomposes the goal",
        "info": "Calls plan_tools() to decompose the goal into MCP server specs.",
    })
    viz_emit("edge", {"source": "prompt", "target": "supervisor", "color": "#d4a53c", "width": 3})
    viz_emit("node", {
        "id": "builder", "label": "Tool Builder", "bt": "planet", "pt": "jupiter",
        "color": "#4A9EFF", "r": 24, "ix": 0, "iy": -80,
        "desc": "Generates MCP server code in parallel on Modal Cloud",
        "info": "Dispatches parallel code generation tasks to Modal Cloud workers.",
    })
    viz_emit("edge", {"source": "supervisor", "target": "builder", "color": "#d4a53c", "width": 2.5})
    for i, t in enumerate(tools):
        slug = t["slug"]
        ix   = (i - N / 2 + 0.5) * 160
        viz_emit("node", {
            "id": f"agent_{slug}", "label": f"MCP Builder {i + 1}", "bt": "planet",
            "pt": _VIZ_PLANET_TYPES[i % len(_VIZ_PLANET_TYPES)],
            "color": _VIZ_AGENT_COLORS[i % len(_VIZ_AGENT_COLORS)],
            "r": 15, "ix": ix, "iy": 20,
            "desc": f"Builds {slug} server",
            "info": t["prompt"][:200],
        })
        viz_emit("edge", {"source": "builder", "target": f"agent_{slug}", "color": "#d4a53c", "width": 1.8})

    # ── 3. Load template once ──────────────────────────────────────────────────
    if not TEMPLATE_FILE.exists():
        print(f"Error: template not found at {TEMPLATE_FILE}", file=sys.stderr)
        sys.exit(1)
    template = TEMPLATE_FILE.read_text()

    # ── 3b. Resolve curated API candidates for each tool ──────────────────────
    api_contexts: list[str] = []
    if HAS_API_REFERENCE:
        print(_sec("API LOOKUP"))
        for t in tools:
            candidates = get_best_apis(t["prompt"], top_n=5)
            if candidates:
                ctx = format_api_context(candidates)
                api_contexts.append(ctx)
                names = ", ".join(c["name"] for c in candidates[:3])
                print(f"  {_BL}  {t['slug']}{_R}  {_DIM}→  {len(candidates)} API(s)  ·  {names}{_R}")
            else:
                api_contexts.append("")
                print(f"  {_DIM}  {t['slug']}  →  no curated APIs found, using LLM knowledge{_R}")
    else:
        print(f"\n{_Y}  ⚠  api_reference not available — using LLM knowledge only{_R}")
        api_contexts = [""] * len(tools)

    # ── 4. Generate code + tests in parallel on Modal ─────────────────────────
    print(_sec("GENERATING"))
    print(f"  spinning up {_B}{len(tools)}{_R} Modal workers  {_DIM}(code + tests in parallel)…{_R}")
    viz_emit("phase", {"text": "Generating code on Modal…"})
    for t in tools:
        viz_emit("status", {"id": f"agent_{t['slug']}", "status": "building"})
    args = [(t["prompt"], template, ctx) for t, ctx in zip(tools, api_contexts)]
    generated = list(build_mcp_and_tests.starmap(args))  # list of (code, test_code)

    # Emit server nodes now that we have generated code
    for i, (t, _) in enumerate(zip(tools, generated)):
        slug = t["slug"]
        ix   = (i - N / 2 + 0.5) * 160
        viz_emit("node", {
            "id": f"srv_{slug}", "label": f"{slug}_mcp", "bt": "planet",
            "pt": _VIZ_SRV_PLANETS[i % len(_VIZ_SRV_PLANETS)],
            "color": _VIZ_SRV_COLORS[i % len(_VIZ_SRV_COLORS)],
            "r": 17, "ix": ix, "iy": 120,
            "desc": f"{slug} MCP server",
            "info": f"Generated MCP server for {slug}.",
        })
        viz_emit("edge", {"source": f"agent_{slug}", "target": f"srv_{slug}", "color": "#7b2cbf", "width": 1.5})
        viz_emit("status", {"id": f"agent_{slug}", "status": "deployed"})

    # ── 5. Batch syntax check + retry (fast pre-check before deploying) ────────
    print(_sec("SYNTAX CHECK"))
    viz_emit("phase", {"text": "Checking syntax…"})
    for attempt in range(1, MAX_RETRIES + 1):
        bad = [i for i, (code, _) in enumerate(generated) if not valid_syntax(code)]
        if not bad:
            break
        print(f"  {_Y}  ⚠  {len(bad)} server(s) had syntax errors — "
              f"retrying (pass {attempt}/{MAX_RETRIES})…{_R}")
        retry_results = list(build_mcp_and_tests.starmap([args[i] for i in bad]))
        for i, result in zip(bad, retry_results):
            generated[i] = result

    # report any still failing after all retries
    still_bad = [i for i, (code, _) in enumerate(generated) if not valid_syntax(code)]
    if still_bad:
        print(f"  {_RE}  ✗  {len(still_bad)} server(s) still failing after retries:{_R}")
        for i in still_bad:
            slug = tools[i]['slug']
            ix   = (i - N / 2 + 0.5) * 160
            print(f"  {_DIM}    · {slug}{_R}")
            viz_emit("node", {
                "id": f"v_{slug}_syntax", "label": "Syntax Error", "bt": "comet",
                "color": "#00E5FF", "r": 13, "ix": ix, "iy": 220,
                "desc": "ast.parse() — syntax validation failed",
                "info": f"Syntax error in {slug} after {MAX_RETRIES} retries.",
            })
            viz_emit("edge", {"source": f"srv_{slug}", "target": f"v_{slug}_syntax", "color": "#00E5FF", "width": 0.8})
            viz_emit("status", {"id": f"v_{slug}_syntax", "status": "failed"})
            viz_emit("status", {"id": f"srv_{slug}", "status": "failed"})
    else:
        print(f"  {_G}  ✓  all {len(generated)} servers passed{_R}")

    # ── 6. Per-MCP: deploy → test → interpret → retry ─────────────────────────
    OUTPUT_DIR.mkdir(exist_ok=True)
    make_room_for(len(tools))

    from mcp_builder import interpret_results  # runs locally

    summary: list[tuple[str, str]] = []

    for idx, (tool, (code, test_code)) in enumerate(zip(tools, generated), 1):
        slug      = tool["slug"]
        prompt    = tool["prompt"]
        filename  = f"{idx}_{slug}_mcp.py"
        filepath  = OUTPUT_DIR / filename

        print(_mcp_header(idx, len(tools), slug))

        i          = idx - 1
        agent_ix   = (i - N / 2 + 0.5) * 160

        current_code   = code
        current_tests  = test_code
        current_prompt = prompt
        final_status   = None

        viz_emit("phase", {"text": f"Deploying {slug}…"})
        # Create deploy validation node upfront (JS skips duplicates)
        viz_emit("node", {
            "id": f"v_{slug}_deploy", "label": "Deploy", "bt": "comet",
            "color": "#00E5FF", "r": 13, "ix": agent_ix - 20, "iy": 220,
            "desc": f"modal deploy — {slug}",
            "info": f"Deploying {slug} MCP server to Modal.",
        })
        viz_emit("edge", {"source": f"srv_{slug}", "target": f"v_{slug}_deploy", "color": "#00E5FF", "width": 0.8})

        for attempt in range(1, MAX_DEPLOY_ATTEMPTS + 1):
            print(f"\n  {_DIM}  ↳ attempt {attempt}/{MAX_DEPLOY_ATTEMPTS}{_R}")

            # Syntax guard on retried code
            if not valid_syntax(current_code):
                print(f"  {_RE}    ✗  syntax error — cannot deploy{_R}")
                if attempt < MAX_DEPLOY_ATTEMPTS:
                    print(f"  {_Y}    ↺  regenerating…{_R}")
                    current_code, current_tests = build_mcp_and_tests.remote(
                        current_prompt, template
                    )
                    continue
                viz_emit("status", {"id": f"v_{slug}_deploy", "status": "failed"})
                final_status = "skipped (failed after retries)"
                break

            filepath.write_text(current_code)
            ok, endpoint_url = deploy_file(filepath)

            if not ok or not endpoint_url:
                print(f"  {_RE}    ✗  deploy failed{_R}")
                viz_emit("status", {"id": f"v_{slug}_deploy", "status": "failed"})
                if attempt < MAX_DEPLOY_ATTEMPTS:
                    continue
                final_status = "skipped (failed after retries)"
                break

            print(f"  {_BL}    ⬡  endpoint  {endpoint_url}{_R}")
            print(f"  {_DIM}    running tests…{_R}")

            viz_emit("phase", {"text": f"Testing {slug}…"})
            # Create test validation node (JS skips duplicates)
            viz_emit("node", {
                "id": f"v_{slug}_test", "label": "Tests", "bt": "comet",
                "color": "#00E5FF", "r": 13, "ix": agent_ix + 20, "iy": 220,
                "desc": f"Integration tests — {slug}",
                "info": f"Running integration tests for {slug} MCP server.",
            })
            viz_emit("edge", {"source": f"srv_{slug}", "target": f"v_{slug}_test", "color": "#00E5FF", "width": 0.8})

            test_output = run_tests_locally(endpoint_url, current_tests)
            preview = test_output[:600] + ("…" if len(test_output) > 600 else "")
            print(f"{_DIM}{preview}{_R}")

            print(f"  {_DIM}    interpreting results…{_R}")
            interpretation = interpret_results(test_output, current_code, current_prompt)
            verdict  = interpretation.get("verdict", "code_bug")
            reason   = interpretation.get("reason", "")

            if verdict == "valid":
                print(f"  {_G}    ✓  valid{_R}  {_DIM}─  {reason}{_R}")
                viz_emit("status", {"id": f"v_{slug}_deploy", "status": "deployed"})
                viz_emit("status", {"id": f"v_{slug}_test",   "status": "deployed"})
                viz_emit("status", {"id": f"srv_{slug}",      "status": "deployed"})
                final_status = "deployed"
                break
            elif verdict == "transient":
                print(f"  {_Y}    ⚠  transient{_R}  {_DIM}─  {reason}{_R}")
                viz_emit("status", {"id": f"v_{slug}_test", "status": "failed"})
                viz_emit("status", {"id": f"srv_{slug}",    "status": "skipped"})
            else:
                print(f"  {_RE}    ✗  code_bug{_R}  {_DIM}─  {reason}{_R}")
                viz_emit("status", {"id": f"v_{slug}_deploy", "status": "failed"})
                viz_emit("status", {"id": f"v_{slug}_test",   "status": "failed"})

            # Not valid — tear down the app before deciding what to do
            app_name = extract_app_name(current_code)
            if app_name:
                stop_app_by_name(app_name)
            filepath.unlink(missing_ok=True)

            if verdict == "transient":
                final_status = "skipped (external failure)"
                break

            # code_bug
            if attempt < MAX_DEPLOY_ATTEMPTS:
                adjusted = interpretation.get("adjusted_prompt") or current_prompt
                print(f"  {_Y}    ↺  regenerating with adjusted prompt…{_R}")
                current_code, current_tests = build_mcp_and_tests.remote(
                    adjusted, template
                )
                current_prompt = adjusted
            else:
                viz_emit("status", {"id": f"srv_{slug}", "status": "failed"})
                final_status = "skipped (failed after retries)"

        if final_status is None:
            viz_emit("status", {"id": f"srv_{slug}", "status": "failed"})
            final_status = "skipped (failed after retries)"

        summary.append((slug, final_status))

    # ── 7. Summary ─────────────────────────────────────────────────────────────
    deployed  = [(n, s) for n, s in summary if s == "deployed"]
    transient = [(n, s) for n, s in summary if "external" in s]
    failed    = [(n, s) for n, s in summary if "retries" in s]

    print(f"\n{_C}{_B}{'━' * 54}{_R}")
    print(f"{_C}{_B}  RESULTS  ·  {len(summary)} servers processed{_R}")
    print(f"{_C}{_B}{'━' * 54}{_R}")
    for name, status in summary:
        if status == "deployed":
            icon, col = "✓", _G
        elif "external" in status:
            icon, col = "⚠", _Y
        else:
            icon, col = "✗", _RE
        label = status.replace("skipped (", "").rstrip(")")
        print(f"  {col}{_B}{icon}{_R}  {_B}{name}{_R}  {_DIM}{label}{_R}")
    print(f"{_C}{_B}{'━' * 54}{_R}")
    print(f"\n  {_DIM}files saved to  {OUTPUT_DIR}/{_R}")

    viz_emit("phase", {"text": "Pipeline complete ✓"})
    viz_emit("done", {})
