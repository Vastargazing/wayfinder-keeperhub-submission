"""The screen reads. It cannot write the journal, the checkpoint, or anything else."""

import http.client
import socket
import sqlite3
import threading

import pytest

from operator_console.audit import audit_source
from operator_console.server import ConsoleConfig, ConsoleHandler
from operator_console.sources import read_journal

#: Every module the screen loads to read and draw a run.
#:
#: ``audit.py`` is excluded because it is where the forbidden names are listed,
#: and ``fixture.py`` because it is the fixture *writer* — it creates a new,
#: marked directory and is never on the path that reads somebody's run.
READ_PATH_MODULES = (
    "__init__.py", "__main__.py", "actions.py", "model.py",
    "outcome.py", "provenance.py", "render.py", "server.py", "sources.py",
)


def test_journal_and_checkpoint_are_unchanged_by_a_render(run_dir):
    from operator_console.server import render_run

    journal = run_dir / "sdk-journal.sqlite"
    checkpoint = run_dir / "run-plan.json"
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (journal, checkpoint)}
    config = ConsoleConfig(state_dir=run_dir, manifest=run_dir / "run-manifest.json")
    for _ in range(3):
        render_run(config)
    for path, (data, mtime) in before.items():
        assert path.read_bytes() == data, path
        assert path.stat().st_mtime_ns == mtime, path
    assert not (run_dir / "run-plan.lock").exists(), "reading must not take RunPlan's writer lock"


def test_the_journal_is_opened_read_only(run_dir, monkeypatch):
    """SQLite itself refuses a write; it is not our own care not to call one."""
    captured = {}
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        captured.setdefault("dsn", args[0])
        captured.setdefault("kwargs", kwargs)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    read_journal(run_dir / "sdk-journal.sqlite")
    assert "mode=ro" in captured["dsn"]
    assert captured["kwargs"].get("uri") is True

    # The same connection string, opened here, refuses the write.
    conn = real_connect(captured["dsn"], uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly|not authorized"):
            conn.execute("UPDATE operations SET state='submitted'")
        with pytest.raises(sqlite3.OperationalError, match="readonly|not authorized"):
            conn.execute("DELETE FROM operations")
    finally:
        conn.close()


def test_a_missing_journal_is_reported_not_created(tmp_path):
    from operator_console.model import build_view
    from operator_console.render import render_page
    from operator_console.sources import load_state_directory

    empty = tmp_path / "nothing"
    empty.mkdir()
    view = build_view(load_state_directory(empty))
    assert view.available is False
    assert not view.operations and not view.steps
    assert list(empty.iterdir()) == [], "probing must not create a journal"
    page = render_page(view)
    assert "No journal" in page
    assert "sdk-journal.sqlite" in page


def test_the_read_path_holds_no_way_to_send_or_write():
    """No HTTP client, no signer call, no journal mutator on the path that reads."""
    from pathlib import Path

    from operator_console import audit as audit_module

    package = Path(audit_module.__file__).parent
    for name in READ_PATH_MODULES:
        found = audit_source((package / name).read_text())
        assert found == [], (name, found)


def test_the_read_path_never_reaches_the_fixture_writer():
    from pathlib import Path

    from operator_console import audit as audit_module

    package = Path(audit_module.__file__).parent
    for name in ("sources.py", "model.py", "render.py", "server.py", "__init__.py"):
        text = (package / name).read_text()
        assert "from .fixture" not in text, name
        assert "import fixture" not in text, name


@pytest.fixture
def server(run_dir):
    """Bind real handler configuration; each request uses AF_UNIX, not a listener."""
    config = ConsoleConfig(state_dir=run_dir, manifest=run_dir / "run-manifest.json")
    artifacts = (run_dir / "sdk-journal.sqlite", run_dir / "run-plan.json")
    before = {path: path.read_bytes() for path in artifacts}
    try:
        yield type("FixtureConsoleHandler", (ConsoleHandler,), {"config": config})
    finally:
        assert {path: path.read_bytes() for path in artifacts} == before


def _request(handler, method, path="/"):
    """Exercise the HTTP parser and response over a bounded, connected socket pair."""
    client_socket, server_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with client_socket, server_socket:
        client_socket.settimeout(5)
        server_socket.settimeout(5)
        errors = []

        def serve():
            try:
                handler(server_socket, ("local-fixture", 0), None)
            except BaseException as exc:
                errors.append(exc)
            finally:
                server_socket.close()

        thread = threading.Thread(target=serve, name="console-http-fixture", daemon=True)
        conn = http.client.HTTPConnection("unused.invalid", timeout=5)
        conn.sock = client_socket
        try:
            thread.start()
            conn.request(method, path)
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()
            # Unblock the handler even if the client fails before sending bytes.
            try:
                server_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # The handler may already have closed its end.
            if thread.ident is not None:
                thread.join(timeout=6)
                assert not thread.is_alive(), "HTTP fixture handler did not terminate"
            if errors:
                raise errors[0]


def test_request_propagates_handler_failure_after_response(server):
    class BrokenHandler(server):
        def do_GET(self):
            super().do_GET()
            raise RuntimeError("fixture handler failed after response")

    with pytest.raises(RuntimeError, match="fixture handler failed after response"):
        _request(BrokenHandler, "GET")


def test_request_cleans_up_when_client_fails(server, monkeypatch):
    sockets = []
    pair = socket.socketpair
    threads_before = set(threading.enumerate())

    def tracked_pair(*args):
        result = pair(*args)
        sockets.extend(result)
        return result

    def fail_request(*args, **kwargs):
        raise OSError("fixture client failed before request")

    monkeypatch.setattr(socket, "socketpair", tracked_pair)
    monkeypatch.setattr(http.client.HTTPConnection, "request", fail_request)
    with pytest.raises(OSError, match="fixture client failed before request"):
        _request(server, "GET")
    assert len(sockets) == 2 and all(sock.fileno() == -1 for sock in sockets)
    assert not any(thread.name == "console-http-fixture" and thread not in threads_before
                   for thread in threading.enumerate())


def test_get_serves_the_page(server):
    status, body, headers = _request(server, "GET")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"Where execution stopped" in body


def test_check_state_route_is_a_get_that_serves_the_page(server):
    status, body, _ = _request(server, "GET", "/?checked=1")
    assert status == 200
    assert b"Check state" in body


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_no_method_other_than_a_read_is_served(server, method):
    status, body, headers = _request(server, method)
    assert status == 405
    assert headers["Allow"] == "GET, HEAD"
    assert b"reads only" in body


def test_the_handler_defines_no_write_endpoint():
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert getattr(ConsoleHandler, f"do_{method}") is ConsoleHandler._refuse
    assert not any(
        name.startswith("do_") and name not in {"do_GET", "do_HEAD", "do_POST",
                                                "do_PUT", "do_PATCH", "do_DELETE"}
        for name in vars(ConsoleHandler)
    )


@pytest.mark.parametrize("path, expected", [("/", 200), ("/?checked=1", 200), ("/absent", 404)])
def test_head_uses_the_get_route_status(server, path, expected):
    get_status, _, _ = _request(server, "GET", path)
    head_status, body, headers = _request(server, "HEAD", path)
    assert get_status == head_status == expected
    assert body == b""
    assert int(headers["Content-Length"]) > 0


@pytest.mark.parametrize("path, expected", [("/", 200), ("/absent", 404)])
def test_head_sends_headers_without_body_bytes(server, monkeypatch, path, expected):
    # HTTPResponse.read() suppresses HEAD bodies itself. Capture server writes
    # as well, so accidentally sending body bytes cannot pass this check.
    pair = socket.socketpair
    sent = []

    class RecordingSocket:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.raw.close()

        def sendall(self, data, *args, **kwargs):
            sent.append(bytes(data))
            return self.raw.sendall(data, *args, **kwargs)

    def recording_pair(*args):
        client, peer = pair(*args)
        return client, RecordingSocket(peer)

    monkeypatch.setattr(socket, "socketpair", recording_pair)
    status, _, headers = _request(server, "HEAD", path)
    assert status == expected
    wire_headers, wire_body = b"".join(sent).split(b"\r\n\r\n", 1)
    assert wire_headers.startswith(f"HTTP/1.0 {expected} ".encode())
    assert int(headers["Content-Length"]) > 0
    assert wire_body == b""
