import http.client
import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_dashboard_account import (
    _account_server,
    _owner_headers,
    _request,
    _request_raw,
)

import dashboard_server


def _account(service, email, *, role="admin"):
    user = service.create_user(
        email,
        "correct horse battery staple",
        role=role,
        access_state="approved",
    )
    session = service.create_session(user["id"])
    return user, {
        "Cookie": (
            f"{dashboard_server.ACCOUNT_SESSION_COOKIE}={session['session_token']}"
        ),
        "X-Real-IP": "203.0.113.40",
    }


def _users(port):
    status, payload, _ = _request(
        port, "GET", "/api/admin/users", headers=_owner_headers(port)
    )
    assert status == 200, payload
    return payload


def _downloads(payload, user_id):
    user = next(item for item in payload["users"] if item["id"] == user_id)
    return user["database_downloads"]


def _wait_download(port, user_id, status, *, completed_count=None):
    deadline = time.monotonic() + 3
    while True:
        payload = _users(port)
        downloads = (
            payload["unattributed_downloads"]
            if user_id is None
            else _downloads(payload, user_id)
        )
        if (
            downloads["recent"]
            and downloads["recent"][0]["status"] == status
            and (
                completed_count is None
                or downloads["completed_count"] == completed_count
            )
        ):
            return payload, downloads
        assert time.monotonic() < deadline, downloads
        time.sleep(0.01)


def _snapshot_factory(monkeypatch, tmp_path, content):
    snapshots = []

    def create_snapshot():
        path = tmp_path / f"snapshot-{len(snapshots)}.db"
        path.write_bytes(content)
        snapshots.append(path)
        return str(path)

    monkeypatch.setattr(dashboard_server, "create_db_snapshot", create_snapshot)
    return snapshots


def test_download_http_counts_completed_bytes_for_authenticated_account_only(
    monkeypatch, tmp_path
):
    content = b"SQLite format 3\x00" + b"download payload" * 113
    snapshots = _snapshot_factory(monkeypatch, tmp_path, content)
    with _account_server(monkeypatch, tmp_path) as (port, service, _):
        owner, owner_cookie = _account(service, "downloader@example.com")
        member, member_cookie = _account(service, "member@example.com", role="member")
        assert _request(port, "GET", "/download/db")[0] == 401
        assert _request(port, "GET", "/download/db", headers=member_cookie)[0] == 403

        for completed_count in (1, 2):
            status, body, headers = _request_raw(
                port, "GET", "/download/db", headers=owner_cookie
            )
            assert status == 200
            assert body == content
            assert dict(headers)["Content-Length"] == str(len(content))
            payload, downloads = _wait_download(
                port, owner["id"], "completed", completed_count=completed_count
            )
            # Completion is persisted before the handler removes its temporary
            # snapshot and releases the lock; wait for that cleanup too.
            assert dashboard_server.DB_SNAPSHOT_LOCK.acquire(timeout=3)
            dashboard_server.DB_SNAPSHOT_LOCK.release()

        assert downloads["active_count"] == 0
        assert downloads["completed_count"] == 2
        assert downloads["total_bytes"] == 2 * len(content)
        assert len(downloads["recent"]) == 2
        assert len({row["id"] for row in downloads["recent"]}) == 2
        for row in downloads["recent"]:
            assert row["bytes_sent"] == row["total_bytes"] == len(content)
            assert row["finished_at"]
            assert row["filename"].endswith(".db")
            assert row["ip_address"] == "203.0.113.40"
        assert _downloads(payload, member["id"])["recent"] == []
        assert payload["unattributed_downloads"]["recent"] == []
        assert payload["summary"]["downloading"] == 0
    assert len(snapshots) == 2
    assert all(not path.exists() for path in snapshots)


def test_download_preparation_is_visible_and_busy_request_is_not_counted(
    monkeypatch, tmp_path
):
    preparing = threading.Event()
    continue_snapshot = threading.Event()
    snapshot = tmp_path / "preparing.db"
    snapshot.write_bytes(b"database snapshot")

    def create_snapshot():
        preparing.set()
        assert continue_snapshot.wait(5)
        return str(snapshot)

    monkeypatch.setattr(dashboard_server, "create_db_snapshot", create_snapshot)
    with _account_server(monkeypatch, tmp_path) as (port, service, _):
        user, cookie = _account(service, "preparing@example.com")
        with ThreadPoolExecutor(max_workers=1) as executor:
            request = executor.submit(
                _request_raw, port, "GET", "/download/db", headers=cookie
            )
            try:
                assert preparing.wait(3)
                payload = _users(port)
                downloads = _downloads(payload, user["id"])
                assert downloads["active_count"] == 1
                assert downloads["recent"][0]["status"] == "preparing"
                assert downloads["recent"][0]["bytes_sent"] == 0
                assert payload["summary"]["downloading"] == 1
                assert _request(port, "GET", "/download/db", headers=cookie)[0] == 429
                assert len(_downloads(_users(port), user["id"])["recent"]) == 1
            finally:
                continue_snapshot.set()
            assert request.result(timeout=5)[0] == 200
        _, downloads = _wait_download(port, user["id"], "completed")
        assert downloads["active_count"] == 0
    assert not snapshot.exists()


def test_legacy_token_download_is_reported_without_guessing_account(
    monkeypatch, tmp_path
):
    content = b"legacy database download"
    _snapshot_factory(monkeypatch, tmp_path, content)
    with _account_server(monkeypatch, tmp_path) as (port, service, _):
        user, _ = _account(service, "same-ip@example.com")
        status, body, _ = _request_raw(
            port,
            "GET",
            "/download/db",
            headers={**_owner_headers(port), "X-Real-IP": "203.0.113.40"},
        )
        assert status == 200
        assert body == content
        payload, downloads = _wait_download(port, None, "completed")
        assert downloads["completed_count"] == 1
        assert downloads["total_bytes"] == len(content)
        assert downloads["recent"][0]["ip_address"] == "203.0.113.40"
        assert _downloads(payload, user["id"])["recent"] == []


def test_snapshot_failure_finishes_record_and_releases_download_lock(
    monkeypatch, tmp_path
):
    def failed_snapshot():
        raise OSError("snapshot disk is full")

    monkeypatch.setattr(dashboard_server, "create_db_snapshot", failed_snapshot)
    with _account_server(monkeypatch, tmp_path) as (port, service, _):
        user, cookie = _account(service, "failed@example.com")
        assert _request(port, "GET", "/download/db", headers=cookie)[0] == 500
        payload, downloads = _wait_download(port, user["id"], "failed")
        assert downloads["active_count"] == 0
        assert downloads["total_bytes"] == 0
        assert downloads["completed_count"] == 0
        assert downloads["recent"][0]["finished_at"]
        assert payload["summary"]["downloading"] == 0
        _snapshot_factory(monkeypatch, tmp_path, b"retry database")
        assert _request_raw(port, "GET", "/download/db", headers=cookie)[0] == 200


class _FileHandler:
    def __init__(self, writer):
        self.wfile = writer
        self.responses = []
        self.headers = {}

    def send_response(self, status):
        self.responses.append(status)

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


@pytest.mark.parametrize("content", [b"", bytes(range(251)) * 3], ids=["empty", "short-writes"])
def test_file_response_completes_empty_files_and_retries_short_positive_writes(
    monkeypatch, tmp_path, content
):
    snapshot = tmp_path / "short-writes.db"
    snapshot.write_bytes(content)
    monkeypatch.setattr(dashboard_server, "DATABASE_DOWNLOAD_CHUNK_BYTES", 31)
    monkeypatch.setattr(dashboard_server.time, "monotonic", lambda: 1.0)
    events = []

    class ShortWriter:
        def __init__(self):
            self.output = bytearray()
            self.calls = 0
            self.flushed = False

        def write(self, data):
            self.calls += 1
            written = min(7, len(data))
            self.output.extend(data[:written])
            return written

        def flush(self):
            self.flushed = True

    writer = ShortWriter()
    handler = _FileHandler(writer)
    dashboard_server.file_response(
        handler,
        snapshot,
        snapshot.name,
        "application/vnd.sqlite3",
        progress_callback=lambda *event: events.append(event),
    )
    assert bytes(writer.output) == content
    assert writer.flushed
    assert writer.calls == 0 if not content else writer.calls > len(content) // 31
    assert events == [
        ("downloading", 0, len(content)),
        ("completed", len(content), len(content)),
    ]
    assert handler.responses == [200]
    assert handler.headers["Content-Length"] == str(len(content))


def test_file_response_reports_live_progress_and_preserves_bytes_on_disconnect(
    monkeypatch, tmp_path
):
    chunk_size = dashboard_server.DATABASE_DOWNLOAD_CHUNK_BYTES
    snapshot = tmp_path / "partial.db"
    snapshot.write_bytes(b"x" * (chunk_size + 13))
    events = []
    clock = itertools.count(0, 2)
    monkeypatch.setattr(dashboard_server.time, "monotonic", lambda: next(clock))

    class DisconnectingWriter:
        written = 0

        def write(self, chunk):
            if self.written:
                assert ("downloading", chunk_size, chunk_size + 13) in events
                raise BrokenPipeError("client closed download")
            self.written += len(chunk)
            return len(chunk)

        def flush(self):
            pass

    handler = _FileHandler(DisconnectingWriter())
    dashboard_server.file_response(
        handler,
        snapshot,
        "partial.db",
        "application/vnd.sqlite3",
        progress_callback=lambda *event: events.append(event),
    )
    assert events[0] == ("downloading", 0, chunk_size + 13)
    assert events[-1] == ("interrupted", chunk_size, chunk_size + 13)
    assert not any(event[0] == "completed" for event in events)
    assert handler.responses == [200]


def test_stream_read_failure_does_not_append_second_http_response(
    monkeypatch, tmp_path
):
    chunk_size = dashboard_server.DATABASE_DOWNLOAD_CHUNK_BYTES
    snapshot = tmp_path / "read-failure.db"
    snapshot.write_bytes(b"x" * (chunk_size + 20))
    monkeypatch.setattr(dashboard_server, "create_db_snapshot", lambda: str(snapshot))
    original_open = Path.open

    class FailingReader:
        def __init__(self, stream):
            self.stream = stream
            self.reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            self.reads += 1
            if self.reads > 1:
                raise OSError("snapshot read failed")
            return self.stream.read(size)

    def open_snapshot(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return FailingReader(stream) if path == snapshot else stream

    monkeypatch.setattr(Path, "open", open_snapshot)
    with _account_server(monkeypatch, tmp_path) as (port, service, _):
        user, cookie = _account(service, "read-failed@example.com")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", "/download/db", headers=cookie)
            response = connection.getresponse()
            assert response.status == 200
            with pytest.raises(http.client.IncompleteRead) as exc:
                response.read()
            assert exc.value.partial == b"x" * chunk_size
        finally:
            connection.close()
        _, downloads = _wait_download(port, user["id"], "failed")
        assert downloads["active_count"] == 0
        assert downloads["total_bytes"] == chunk_size
        assert downloads["recent"][0]["bytes_sent"] == chunk_size
    assert not snapshot.exists()
