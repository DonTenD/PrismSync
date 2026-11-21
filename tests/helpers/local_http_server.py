"""Minimal local HTTP server used by the controlled integration tests.

Only the standard library is used.  The server binds ``127.0.0.1`` on an
ephemeral port so tests never collide with a fixed port or reach the network.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class RecordedRequest:
    """One request the server observed."""

    path: str
    headers: dict[str, str]
    method: str


@dataclass
class Route:
    """How to answer one path.

    ``body`` is the response payload.  ``status`` is the HTTP status code.
    ``delay_gate`` blocks the handler until the event is set, which lets tests
    hold a response mid-flight and cancel the download deterministically.
    """

    body: bytes
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    delay_gate: threading.Event | None = None


class LocalHttpServer:
    """A tiny threaded HTTP server serving a path-to-route table."""

    def __init__(self, routes: dict[str, Route]) -> None:
        self._routes = routes
        self.requests: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                """Silence the default stderr logging."""

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                with server._lock:
                    server.requests.append(
                        RecordedRequest(
                            path=self.path,
                            headers={k.lower(): v for k, v in self.headers.items()},
                            method="GET",
                        )
                    )
                route = server._routes.get(path)
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if route.delay_gate is not None:
                    route.delay_gate.wait(timeout=10)
                    # If the client already gave up, the write below raises;
                    # that is expected and handled by the base class.

                self.send_response(route.status)
                self.send_header("Content-Length", str(len(route.body)))
                for key, value in route.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(route.body)

        return Handler

    @property
    def base_url(self) -> str:
        """Return ``http://127.0.0.1:<port>``."""
        host, port = self._httpd.server_address[:2]
        host_str = host.decode() if isinstance(host, bytes) else host
        return f"http://{host_str}:{port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def paths(self) -> list[str]:
        """Return every recorded request path in arrival order."""
        with self._lock:
            return [item.path for item in self.requests]

    def count(self, predicate: Any = None) -> int:
        """Count recorded requests, optionally filtered by a predicate."""
        with self._lock:
            if predicate is None:
                return len(self.requests)
            return sum(1 for item in self.requests if predicate(item.path))

    def clear(self) -> None:
        """Forget previously recorded requests."""
        with self._lock:
            self.requests.clear()

    def set_route(self, path: str, route: Route) -> None:
        """Install or replace the route for ``path``."""
        self._routes[path] = route


@contextmanager
def serve(routes: dict[str, Route]) -> Iterator[LocalHttpServer]:
    """Run a local server for the duration of the ``with`` block."""
    server = LocalHttpServer(routes)
    server.start()
    try:
        yield server
    finally:
        server.stop()
