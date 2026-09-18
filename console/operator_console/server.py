"""A local, read-only web view of one run.

``http.server`` from the standard library, on the loopback interface, with
``do_GET`` and nothing else. POST, PUT, PATCH and DELETE are answered 405 with
an explanation rather than left to the base class, so the refusal is a decision
this module makes and a test can assert.

Every request re-reads the journal, the checkpoint and the manifest from disk.
There is no cache to go stale and no writable state anywhere in the process.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .actions import ALLOWED_HREFS
from .model import build_view
from .outcome import JournalOutcomeReader, OutcomeReader
from .render import render_page
from .sources import load_state_directory

REFUSAL = (
    "This operator screen performs reads only. It has no write, send, retry or "
    "resolve endpoint, so there is nothing here for this method to reach.\n"
)


@dataclass
class ConsoleConfig:
    state_dir: Path
    manifest: Path | None = None
    reader: OutcomeReader | None = None

    def read(self):
        """One fresh read of every artifact, and the view built from it."""
        state = load_state_directory(self.state_dir, self.manifest)
        return build_view(state, self.reader or JournalOutcomeReader())


def render_run(config: ConsoleConfig) -> str:
    """One fresh read, drawn. The name avoids shadowing the ``render`` module."""
    return render_page(config.read())


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "OperatorScreen/0.1"
    config: ConsoleConfig

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        route = urlsplit(self.path)
        path = route.path
        if self.path in ALLOWED_HREFS or path == "/":
            self._send(200, "text/html; charset=utf-8", render_run(self.config).encode())
            return
        self._send(404, "text/plain; charset=utf-8", b"No such view. This console serves one page at /.\n")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _refuse(self) -> None:
        """Anything that is not a read is refused, and says why."""
        self._send(405, "text/plain; charset=utf-8", REFUSAL.encode(), allow="GET, HEAD")

    do_POST = _refuse  # noqa: N815 - http.server's naming
    do_PUT = _refuse  # noqa: N815
    do_PATCH = _refuse  # noqa: N815
    do_DELETE = _refuse  # noqa: N815

    def _send(self, code: int, content_type: str, body: bytes, *, allow: str = "",
              include_body: bool = True) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if allow:
            self.send_header("Allow", allow)
        self.end_headers()
        if include_body and body and self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        return


def loopback_host(host: str) -> str:
    """Canonical IPv4 127/8 literals, or exact localhost without DNS."""
    if host == "localhost":
        return "127.0.0.1"
    try:
        address = IPv4Address(host)
    except (ValueError, TypeError):
        address = None
    if address is None or str(address) != host or not address.is_loopback:
        raise ValueError(
            "host must be a canonical IPv4 loopback literal in 127.0.0.0/8 "
            "or localhost (127.0.0.1); hostnames and IPv6 are unsupported"
        )
    return str(address)


def make_server(config: ConsoleConfig, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Bind a loopback server. Nothing is written and nothing leaves the machine."""
    host = loopback_host(host)
    handler = type("BoundConsoleHandler", (ConsoleHandler,), {"config": config})
    return ThreadingHTTPServer((host, port), handler)
