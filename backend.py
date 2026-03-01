"""
backend.py — Apollo backend + UI server.

Usage:
    python backend.py            # http://localhost:8080
    python backend.py --port 9000

All supervisor output is printed directly to this terminal.
"""

import asyncio
import os
import pty
import re
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

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mGKHFJA-Z]')

def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub('', s)


async def homepage(request: Request):
    return FileResponse(UI_DIR / "index.html")


async def run_stream(request: Request):
    prompt = request.query_params.get("prompt", "").strip()

    async def generate():
        if not prompt:
            yield {"event": "error_msg", "data": "No prompt provided"}
            return

        # PTY makes child processes think they're writing to a real terminal,
        # which forces line-buffered output instead of block-buffered.
        master_fd, slave_fd = pty.openpty()

        process = await asyncio.create_subprocess_exec(
            sys.executable, str(SUPERVISOR_PATH), "--prompt", prompt,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=slave_fd,
            stderr=slave_fd,
        )
        os.close(slave_fd)  # parent doesn't need the slave end

        loop = asyncio.get_running_loop()
        read_queue: asyncio.Queue[bytes] = asyncio.Queue()

        def on_readable():
            try:
                data = os.read(master_fd, 4096)
                read_queue.put_nowait(data)
            except OSError:
                # PTY slave closed (process exited)
                read_queue.put_nowait(b"")
                loop.remove_reader(master_fd)

        loop.add_reader(master_fd, on_readable)

        state = "normal"   # normal | seen_label | in_answer | done
        final_lines: list[str] = []

        def is_sep(s: str) -> bool:
            stripped = s.strip()
            return len(stripped) >= 20 and stripped == "=" * len(stripped)

        def process_line(line: str):
            nonlocal state
            clean = strip_ansi(line)
            print(line, flush=True)               # raw (with colors) to terminal
            # yield happens outside — we return the event dict
            if state == "normal":
                if "FINAL ANSWER" in clean:
                    state = "seen_label"
            elif state == "seen_label":
                if is_sep(clean):
                    state = "in_answer"
            elif state == "in_answer":
                if is_sep(clean):
                    text = "\n".join(
                        l[1:] if l.startswith(" ") else l for l in final_lines
                    )
                    return clean, text   # signal final answer
                else:
                    final_lines.append(clean)
            return clean, None

        buf = b""
        while True:
            chunk = await read_queue.get()
            if not chunk:
                break
            buf += chunk

            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode(errors="replace").rstrip("\r")
                clean, final_answer = process_line(line)
                yield {"event": "output", "data": clean}
                if final_answer is not None:
                    yield {"event": "final_answer", "data": final_answer}

        # flush any remaining partial line (no trailing newline)
        if buf:
            line = buf.decode(errors="replace").rstrip("\r")
            clean, final_answer = process_line(line)
            yield {"event": "output", "data": clean}
            if final_answer is not None:
                yield {"event": "final_answer", "data": final_answer}

        try:
            os.close(master_fd)
        except OSError:
            pass

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

    print(f"\n  Apollo  →  http://localhost:{args.port}\n")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")
