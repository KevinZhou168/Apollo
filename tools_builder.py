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

# ── Configuration ──────────────────────────────────────────────────────────────

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()
if LLM_PROVIDER not in ("anthropic", "openai"):
    print(f"Error: LLM_PROVIDER must be 'anthropic' or 'openai', got '{LLM_PROVIDER}'", file=sys.stderr)
    sys.exit(1)
print(f"Using LLM Provider: {LLM_PROVIDER}")

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
    if raw.startswith("```"):
        raw = re.sub(r"^```[^\n]*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)
    return json.loads(raw)


# ── Modal worker (runs in parallel on Modal) ───────────────────────────────────

@app.function(
    image=image,
    secrets=secrets_list,
    timeout=300,
)
def build_mcp_and_tests(prompt: str, template: str) -> tuple[str, str]:
    """
    Runs on a Modal worker. Generates one complete MCP server and its test script.
    Returns (mcp_code, test_code).
    """
    import sys
    import os
    sys.path.insert(0, "/root")
    os.environ["LLM_PROVIDER"] = LLM_PROVIDER
    from mcp_builder import generate, generate_tests  # noqa: PLC0415
    code = generate(prompt, template)
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
    print(f"\n  At the {ENDPOINT_LIMIT}-endpoint limit. "
          f"Stopping {len(to_stop)} oldest app(s) to make room:")
    for a in to_stop:
        name = a.get("Description", a.get("App ID", "?"))
        app_id = a.get("App ID", "")
        print(f"    Stopping: {name} ({app_id})")
        subprocess.run(["modal", "app", "stop", app_id], check=False)


# ── Deploy helper (runs locally) ───────────────────────────────────────────────

def deploy_file(filepath: Path) -> tuple[bool, str | None]:
    """Deploy a file with `modal deploy`. Returns (success, endpoint_url)."""
    if not shutil.which("modal"):
        print(f"    [!] `modal` CLI not found — skipping deploy of {filepath.name}")
        return False, None
    print(f"    Deploying {filepath.name} …")
    result = subprocess.run(
        ["modal", "deploy", str(filepath)],
        capture_output=True, text=True,
    )
    output = result.stdout + result.stderr
    print(output)
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
                print(f"    Stopping app '{app_name}' ({app_id}) …")
                subprocess.run(["modal", "app", "stop", app_id], check=False)
                return
    print(f"    [!] Could not find deployed app '{app_name}' to stop")


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

    # ── 3. Load template once ──────────────────────────────────────────────────
    if not TEMPLATE_FILE.exists():
        print(f"Error: template not found at {TEMPLATE_FILE}", file=sys.stderr)
        sys.exit(1)
    template = TEMPLATE_FILE.read_text()

    # ── 4. Generate code + tests in parallel on Modal ─────────────────────────
    print(f"\n Generating {len(tools)} MCP server(s) + tests in parallel on Modal …")
    args = [(t["prompt"], template) for t in tools]
    raw_results = list(build_mcp_and_tests.starmap(args))
    generated = list(raw_results)  # list of (code, test_code)

    # ── 5. Batch syntax check + retry (fast pre-check before deploying) ────────
    for attempt in range(1, MAX_RETRIES + 1):
        bad = [i for i, (code, _) in enumerate(generated) if not valid_syntax(code)]
        if not bad:
            break
        print(f"\n  {len(bad)} server(s) had syntax errors — retrying "
              f"(attempt {attempt}/{MAX_RETRIES}) …")
        retry_results = list(build_mcp_and_tests.starmap([args[i] for i in bad]))
        for i, result in zip(bad, retry_results):
            generated[i] = result

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

        print(f"\n {'─' * 52}")
        print(f"  [{idx}/{len(tools)}] {slug}")
        print(f" {'─' * 52}")

        current_code   = code
        current_tests  = test_code
        current_prompt = prompt
        final_status   = None

        for attempt in range(1, MAX_DEPLOY_ATTEMPTS + 1):
            print(f"\n   Attempt {attempt}/{MAX_DEPLOY_ATTEMPTS}")

            # Syntax guard on retried code
            if not valid_syntax(current_code):
                print(f"   Syntax error — cannot deploy.")
                if attempt < MAX_DEPLOY_ATTEMPTS:
                    print(f"   Regenerating …")
                    current_code, current_tests = build_mcp_and_tests.remote(
                        current_prompt, template
                    )
                    continue
                final_status = "skipped (failed after retries)"
                break

            filepath.write_text(current_code)
            ok, endpoint_url = deploy_file(filepath)

            if not ok or not endpoint_url:
                print(f"   Deploy failed.")
                if attempt < MAX_DEPLOY_ATTEMPTS:
                    continue
                final_status = "skipped (failed after retries)"
                break

            print(f"   Endpoint: {endpoint_url}")
            print(f"   Running tests …")
            test_output = run_tests_locally(endpoint_url, current_tests)
            preview = test_output[:600] + ("…" if len(test_output) > 600 else "")
            print(f"   Test output:\n{preview}")

            print(f"   Interpreting results …")
            interpretation = interpret_results(test_output, current_code, current_prompt)
            verdict  = interpretation.get("verdict", "code_bug")
            reason   = interpretation.get("reason", "")
            print(f"   Verdict: {verdict} — {reason}")

            if verdict == "valid":
                final_status = "deployed"
                break

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
                print(f"   Regenerating with adjusted prompt …")
                current_code, current_tests = build_mcp_and_tests.remote(
                    adjusted, template
                )
                current_prompt = adjusted
            else:
                final_status = "skipped (failed after retries)"

        if final_status is None:
            final_status = "skipped (failed after retries)"

        summary.append((slug, final_status))

    # ── 7. Summary ─────────────────────────────────────────────────────────────
    print("\n" + "━" * 56)
    print("  Summary")
    print("━" * 56)
    for name, status in summary:
        print(f"  [{status}] {name}")
    print("━" * 56)
    print(f"\n  Generated files: {OUTPUT_DIR}/")
