"""
ui_server.py — Simple web UI for the Atlas supervisor.

Usage:
    python ui_server.py            # starts on http://localhost:8080
    python ui_server.py --port 9000

The visualization page is still served separately by viz_server.py on port 8765.
"""

import asyncio
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.routing import Route
from sse_starlette.sse import EventSourceResponse

UI_DIR = Path(__file__).parent / "ui"
SUPERVISOR_PATH = Path(__file__).parent / "supervisor.py"
SENTINEL = "ATLAS_DONE_SENTINEL_XYZ"


async def homepage(request: Request):
    return FileResponse(UI_DIR / "index.html")


async def run_stream(request: Request):
    prompt = request.query_params.get("prompt", "").strip()

    async def generate():
        if not prompt:
            yield {"event": "error_msg", "data": "No prompt provided"}
            return

        # Create temp dir for this run's output file
        tmp_dir = tempfile.mkdtemp(prefix="atlas_")
        tmpfile = os.path.join(tmp_dir, "output.txt")

        # Run supervisor with stdout/stderr inherited by the VS Code terminal,
        # while tee-ing everything to a temp file so SSE can stream it.
        cmd = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(SUPERVISOR_PATH))}"
            f" --prompt {shlex.quote(prompt)} 2>&1 | tee {shlex.quote(tmpfile)}"
            f"; echo {shlex.quote(SENTINEL)} >> {shlex.quote(tmpfile)}"
        )
        process = await asyncio.create_subprocess_shell(cmd)
        asyncio.ensure_future(process.wait())  # reap when done

        await asyncio.sleep(0.5)

        state = "normal"   # normal | seen_label | in_answer | done
        final_lines: list[str] = []

        def is_sep(s: str) -> bool:
            stripped = s.strip()
            return len(stripped) >= 20 and stripped == "=" * len(stripped)

        seen = 0
        while True:
            await asyncio.sleep(0.1)
            try:
                with open(tmpfile, "r", errors="replace") as f:
                    all_lines = f.readlines()
            except FileNotFoundError:
                await asyncio.sleep(0.2)
                continue

            new_lines = all_lines[seen:]
            seen += len(new_lines)

            for raw in new_lines:
                line = raw.rstrip("\n")

                if line == SENTINEL:
                    yield {"event": "done", "data": ""}
                    try:
                        shutil.rmtree(tmp_dir)
                    except Exception:
                        pass
                    return

                yield {"event": "output", "data": line}

                if state == "normal":
                    if "FINAL ANSWER" in line:
                        state = "seen_label"
                elif state == "seen_label":
                    if is_sep(line):
                        state = "in_answer"
                elif state == "in_answer":
                    if is_sep(line):
                        text = "\n".join(
                            l[1:] if l.startswith(" ") else l for l in final_lines
                        )
                        yield {"event": "final_answer", "data": text}
                        state = "done"
                    else:
                        final_lines.append(line)

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
