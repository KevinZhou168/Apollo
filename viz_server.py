"""viz_server.py — Tiny SSE visualization server for the Atlas pipeline.

Serves demo/index.html at GET / and a live Server-Sent Events stream at
GET /events.  Call emit() from tools_builder.py to push events to any
connected browser.  All past events are replayed to late-joining connections.
"""

import asyncio
import json
import queue
import threading
import webbrowser
from pathlib import Path

HERE = Path(__file__).parent

# ── Shared state ──────────────────────────────────────────────────────────────
_connections: list[queue.Queue] = []
_events_lock  = threading.Lock()
_past_events: list[tuple[str, dict]] = []   # replayed to late-joining connections


# ── Public API ────────────────────────────────────────────────────────────────

def emit(event_type: str, data: dict) -> None:
    """Push a named SSE event to all connected browsers (thread-safe)."""
    with _events_lock:
        _past_events.append((event_type, data))
        for q in _connections:
            q.put((event_type, data))


def start_viz_server(port: int = 8765, open_browser: bool = True) -> None:
    """Start the visualization server in a daemon thread, then open the browser."""
    import time

    def _run() -> None:
        import uvicorn
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import HTMLResponse
        from starlette.routing import Route
        from sse_starlette.sse import EventSourceResponse

        async def _index(request: Request) -> HTMLResponse:
            html = (HERE / "demo" / "index.html").read_text()
            return HTMLResponse(html)

        async def _events(request: Request) -> EventSourceResponse:
            conn_q: queue.Queue = queue.Queue()
            with _events_lock:
                # Replay all past events to this new connection
                for et, d in _past_events:
                    conn_q.put((et, d))
                _connections.append(conn_q)

            loop = asyncio.get_event_loop()

            async def _gen():
                try:
                    while True:
                        try:
                            event_type, data = await loop.run_in_executor(
                                None, lambda: conn_q.get(timeout=30)
                            )
                        except queue.Empty:
                            yield {"data": ""}   # keepalive ping
                            continue
                        yield {"event": event_type, "data": json.dumps(data)}
                        if event_type == "done":
                            break
                finally:
                    with _events_lock:
                        if conn_q in _connections:
                            _connections.remove(conn_q)

            return EventSourceResponse(_gen())

        starlette_app = Starlette(routes=[
            Route("/", _index),
            Route("/events", _events),
        ])
        uvicorn.run(starlette_app, host="127.0.0.1", port=port, log_level="error")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(1.4)          # give uvicorn time to bind
    if open_browser:
        webbrowser.open(f"http://localhost:{port}/")
