"""RPC send-failure recovery keeps rejection ordering and never retries."""
from __future__ import annotations

import socket
import struct
import threading
from pathlib import Path

import pytest

from daikibo.common import Fault, canonical
from daikibo.rpc import Client, Server
import daikibo.rpc as rpc


class _PreconnectedClientSocket:
    """Expose a connected socket while making the client write fail once."""

    def __init__(self, raw, error=...):
        self.raw = raw
        self.error = error
        self.connect_calls = 0
        self.send_calls = 0
        self.actual_send_error = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.raw.close()

    def settimeout(self, value):
        return self.raw.settimeout(value)

    def connect(self, path):
        # The raw endpoint is connected before the factory is installed.  A
        # second connect would be a reconnect attempt and is deliberately not
        # performed by Client.call.
        self.connect_calls += 1

    def sendall(self, data):
        self.send_calls += 1
        if self.error is not ...:
            self.actual_send_error = self.error
            raise self.error
        try:
            return self.raw.sendall(data)
        except ConnectionError as exc:
            self.actual_send_error = exc
            raise

    def recv(self, size):
        return self.raw.recv(size)


def _frame(body):
    data = body if isinstance(body, bytes) else canonical(body)
    return struct.pack("!I", len(data)) + data


def _buffered_endpoint(tmp_path, response):
    """Return a preconnected client socket after the server has closed it."""
    path = Path(tmp_path) / "rpc.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    ready = threading.Event()
    closed = threading.Event()

    def serve_once():
        ready.set()
        connection, _ = listener.accept()
        try:
            if response is not None:
                connection.sendall(_frame(response))
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            connection.close()
            listener.close()
            closed.set()

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    assert ready.wait(2)
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.connect(str(path))
    assert closed.wait(2)
    thread.join(timeout=2)
    return path, raw


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"ok": False, "error": {"code": "capacity"}},
        {"ok": True, "result": {"accepted": True}},
    ],
    ids=["no-response", "malformed-rejection", "success-envelope"],
)
def test_send_failure_re_raises_original_error_without_retry(tmp_path, response):
    path, raw = _buffered_endpoint(tmp_path, response)
    send_error = BrokenPipeError("client write failed")
    wrapped = _PreconnectedClientSocket(raw, send_error)
    original_socket = rpc.socket.socket
    rpc.socket.socket = lambda *args, **kwargs: wrapped
    try:
        with pytest.raises(BrokenPipeError, match="client write failed") as exc:
            Client(path, timeout=2).call("api.describe")
    finally:
        rpc.socket.socket = original_socket
    assert exc.value is send_error
    assert wrapped.connect_calls == 1
    assert wrapped.send_calls == 1


def test_send_failure_decodes_only_a_typed_rejection_envelope(tmp_path):
    response = {
        "ok": False,
        "error": {
            "code": "write_conflict",
            "message": "Request was superseded.",
            "details": {"request": "REQ-test"},
        },
    }
    path, raw = _buffered_endpoint(tmp_path, response)
    wrapped = _PreconnectedClientSocket(raw, BrokenPipeError("client write failed"))
    original_socket = rpc.socket.socket
    rpc.socket.socket = lambda *args, **kwargs: wrapped
    try:
        with pytest.raises(Fault) as exc:
            Client(path, timeout=2).call("api.describe")
    finally:
        rpc.socket.socket = original_socket
    assert exc.value.code == "write_conflict"
    assert exc.value.message == "Request was superseded."
    assert exc.value.details == {"request": "REQ-test"}
    assert wrapped.connect_calls == 1
    assert wrapped.send_calls == 1


def test_send_failure_decodes_buffered_typed_capacity_from_real_server(full, tmp_path):
    path = Path(tmp_path) / "rpc" / "sock"
    server = Server(full, path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for _ in range(64):
        assert server.slots.acquire(blocking=False)

    closed = threading.Event()
    original_shutdown_request = server.shutdown_request

    def close_and_signal(request):
        try:
            return original_shutdown_request(request)
        finally:
            closed.set()

    server.shutdown_request = close_and_signal
    original_control_request = full.request
    control_requests = []

    def record_control_request(*args, **kwargs):
        control_requests.append((args, kwargs))
        return original_control_request(*args, **kwargs)

    full.request = record_control_request
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    original_socket = rpc.socket.socket
    try:
        raw.connect(str(path))
        assert closed.wait(5)
        wrapped = _PreconnectedClientSocket(raw, error=...)
        rpc.socket.socket = lambda *args, **kwargs: wrapped
        try:
            with pytest.raises(Fault) as exc:
                Client(path, Path(full.sec.bootstrap()).read_text(), timeout=2).call("api.describe")
        finally:
            rpc.socket.socket = original_socket
        assert exc.value.code == "capacity"
        assert exc.value.message == "Control request capacity exceeded; retry with bounded backoff."
        assert exc.value.details is None
        assert isinstance(wrapped.actual_send_error, ConnectionError)
        assert wrapped.connect_calls == 1
        assert wrapped.send_calls == 1
        assert control_requests == []
    finally:
        full.request = original_control_request
        rpc.socket.socket = original_socket
        raw.close()
        for _ in range(64):
            server.slots.release()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
