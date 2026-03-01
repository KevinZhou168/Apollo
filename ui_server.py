"""
ui_server.py — Simple web UI for the Atlas supervisor.

Usage:
    python ui_server.py            # starts on http://localhost:8080
    python ui_server.py --port 9000

The visualization page is still served separately by viz_server.py on port 8765.
"""

import asyncio
import sys
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.routing import Route
from sse_starlette.sse import EventSourceResponse

UI_DIR = Path(__file__).parent / "ui"
SUPERVISOR_PATH = Path(__file__).parent / "supervisor.py"


async def homepage(request: Request):
    return FileResponse(UI_DIR / "index.html")


async def run_stream(request: Request):
    prompt = request.query_params.get("prompt", "").strip()

    async def generate():
        if not prompt:
            yield {"event": "error_msg", "data": "No prompt provided"}
            return

        process = await asyncio.create_subprocess_exec(
            sys.executable, str(SUPERVISOR_PATH), "--prompt", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # State machine to extract the FINAL ANSWER block
        # Supervisor prints: sep → "FINAL ANSWER" → sep → <answer> → sep
        state = "normal"   # normal | seen_label | in_answer
        final_lines: list[str] = []

        def is_sep(s: str) -> bool:
            stripped = s.strip()
            return len(stripped) >= 20 and stripped == "=" * len(stripped)

        async for raw in process.stdout:
            line = raw.decode(errors="replace").rstrip()
            yield {"event": "output", "data": line}

            if state == "normal":
                if "FINAL ANSWER" in line:
                    state = "seen_label"
            elif state == "seen_label":
                if is_sep(line):
                    state = "in_answer"
            elif state == "in_answer":
                if is_sep(line):
                    # Strip the leading space the supervisor adds
                    text = "\n".join(
                        l[1:] if l.startswith(" ") else l for l in final_lines
                    )
                    yield {"event": "final_answer", "data": text}
                    state = "done"
                else:
                    final_lines.append(line)

        await process.wait()
        yield {"event": "done", "data": ""}

    return EventSourceResponse(generate())


app = Starlette(routes=[
    Route("/", homepage),
    Route("/run", run_stream),
])

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    print(f"\n  Atlas UI  →  http://localhost:{args.port}")
    print(f"  Visualization  →  http://localhost:8765  (start viz_server.py separately)\n")
    uvicorn.run(app, host="0.0.0.0", port=args.port)
