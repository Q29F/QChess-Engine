"""Small standard-library transport for a single-user desktop browser interface.

create_server(port, assets, endpoints) returns a server bound to 127.0.0.1 only.
Port zero asks the operating system for an available port; server.origin gives
the resulting browser origin. Assets map exact routes to (content_type, bytes).
Endpoints map exact /api/... routes to synchronous dict-to-dict callbacks.

An HTML asset may contain {{csrf-token}} inside a quoted meta content attribute;
the server replaces it with a random per-process token. GET /api/session also
returns {"token": ...}. API POSTs require the exact Origin, X-CSRF-Token, a single
Content-Length, and application/json (optionally charset=utf-8). No CORS is enabled.

Callbacks own semantic validation and must finish within their own short,
deterministic computation budget. Only one callback runs at once. The transport
can close a connection but cannot forcibly interrupt arbitrary Python code.
Raise RequestError with a static public message for a specific client error;
ordinary ValueError/TypeError messages and unexpected exceptions are not exposed.

Use serve_forever() and always call server_close() in finally or use a context
manager. shutdown() may be called by a different thread. This is a local desktop
transport, not a multiuser authentication boundary or a public production server.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import re
import secrets
import socket
import threading
import time
from types import MappingProxyType
from typing import Any, BinaryIO


_MAX_BODY = 16 * 1024
_MAX_HEADERS = 16 * 1024
_MAX_HEADER_LINE = 8 * 1024
_MAX_HEADER_COUNT = 32
_MAX_REQUEST_LINE = 1024
_MAX_RESPONSE = 1024 * 1024
_MAX_ASSETS = 32
_MAX_ASSET_BYTES = 8 * 1024 * 1024
_MAX_CONNECTIONS = 8
_IDLE_SECONDS = 2.0
_CONNECTION_SECONDS = 15.0
_JOIN_SECONDS = 3.0
_SESSION_PATH = "/api/session"
_TOKEN_PLACEHOLDER = b"{{csrf-token}}"
_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_TOKEN = re.compile(r"[0-9a-f]{64}")
_CONTENT_TYPE = re.compile(r"application/json(?:\s*;\s*charset=utf-8)?", re.I)
_CSP = "; ".join((
    "default-src 'none'", "script-src 'self'", "style-src 'self'",
    "connect-src 'self'", "img-src 'self'", "font-src 'self'",
    "object-src 'none'", "base-uri 'none'", "form-action 'none'",
    "frame-ancestors 'none'",
))
_ERRORS = {
    400: "Invalid request.",
    403: "Request not allowed.",
    404: "Not found.",
    405: "Method not allowed.",
    408: "Request timed out.",
    411: "Content-Length is required.",
    413: "Request body is too large.",
    414: "Request target is too long.",
    415: "Use uncompressed application/json encoded as UTF-8.",
    417: "Expect headers are not supported.",
    429: "A calculation is already running. Try again shortly.",
    431: "Request headers are too large or invalid.",
    500: "Unable to complete the request.",
    503: "The local service is stopping.",
    505: "HTTP version is not supported.",
}


class RequestError(Exception):
    """A client status and static, public, single-line message (never raw input)."""

    def __init__(self, status: int = 400, message: str = "Invalid request.") -> None:
        if type(status) is not int or not 400 <= status <= 499:
            raise ValueError("RequestError status must be a client error")
        if (
            not isinstance(message, str) or not 1 <= len(message) <= 160
            or any(not 32 <= ord(character) <= 126 for character in message)
        ):
            raise ValueError("RequestError requires a short printable ASCII message")
        super().__init__(message)
        self.status = status
        self.public_message = message


def _valid_route(route: str) -> bool:
    return (
        isinstance(route, str) and len(route) <= 256
        and re.fullmatch(r"/[A-Za-z0-9./-]*", route) is not None
        and (route == "/" or all(part not in ("", ".", "..") for part in route[1:].split("/")))
    )


def _close_socket(connection: socket.socket) -> None:
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        connection.close()
    except OSError:
        pass


class _HeaderReader:
    """Bound bytes during header reads, before the standard parser allocates them."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.remaining = _MAX_HEADERS
        self.lines = 0

    def readline(self, size: int = -1) -> bytes:
        limit = min(_MAX_HEADER_LINE, self.remaining)
        if size >= 0:
            limit = min(limit, size)
        line = self.stream.readline(limit + 1)
        if len(line) > limit or not line.endswith(b"\r\n"):
            raise http.client.HTTPException("invalid bounded header")
        self.remaining -= len(line)
        if line == b"\r\n":
            return line
        self.lines += 1
        name, separator, value = line[:-2].partition(b":")
        if (
            self.lines > _MAX_HEADER_COUNT or not separator
            or _HEADER_NAME.fullmatch(name) is None
            or any(byte < 32 and byte != 9 or byte == 127 for byte in value)
        ):
            raise http.client.HTTPException("invalid bounded header")
        return line


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in key):
            raise ValueError("unpaired Unicode surrogate")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("non-finite JSON constant")


def _decode_object(body: bytes) -> dict[str, Any]:
    result = json.loads(
        body.decode("utf-8", errors="strict"),
        object_pairs_hook=_unique_object,
        parse_float=_finite_float,
        parse_constant=_reject_constant,
    )
    if type(result) is not dict:
        raise ValueError("JSON object required")
    pending: list[tuple[Any, int]] = [(result, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 8:
            raise ValueError("JSON nesting limit exceeded")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif isinstance(value, str) and any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("unpaired Unicode surrogate")
    return result


def _encode_object(value: dict[str, Any]) -> bytes:
    if type(value) is not dict:
        raise TypeError("endpoint must return a JSON object")
    result = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    if len(result) > _MAX_RESPONSE:
        raise ValueError("response limit exceeded")
    return result


class _LocalServer(HTTPServer):
    allow_reuse_address = False
    request_queue_size = _MAX_CONNECTIONS

    def __init__(self, port: int, assets: dict, endpoints: dict) -> None:
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)
        self.compute_slot = threading.BoundedSemaphore(1)
        self._closing = False
        self._serving_thread: threading.Thread | None = None
        self._workers: set[threading.Thread] = set()
        self._connections: set[socket.socket] = set()
        self._timers: set[threading.Timer] = set()
        self.session_token = secrets.token_hex(32)
        prepared = {
            path: (mime, body.replace(_TOKEN_PLACEHOLDER, self.session_token.encode("ascii"))
                   if mime.split(";", 1)[0].strip().lower() == "text/html" else body)
            for path, (mime, body) in assets.items()
        }
        self.assets = MappingProxyType(prepared)
        self.endpoints = MappingProxyType(endpoints)
        super().__init__(("127.0.0.1", port), _Handler)
        self.authority = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.authority}"

    def verify_request(self, request: socket.socket, client_address: tuple) -> bool:
        return client_address[0] == "127.0.0.1"

    def process_request(self, request: socket.socket, client_address: tuple) -> None:
        with self._lock:
            if self._closing or not self._slots.acquire(blocking=False):
                _close_socket(request)
                return
            request.settimeout(_IDLE_SECONDS)
            timer = threading.Timer(_CONNECTION_SECONDS, _close_socket, (request,))
            timer.daemon = True
            worker = threading.Thread(
                target=self._handle_connection, args=(request, client_address, timer),
                name="local-http", daemon=True,
            )
            self._connections.add(request)
            self._timers.add(timer)
            self._workers.add(worker)
            try:
                timer.start()
                worker.start()
            except Exception:
                timer.cancel()
                if timer.ident is not None:
                    timer.join(timeout=0.1)
                self._connections.discard(request)
                self._timers.discard(timer)
                self._workers.discard(worker)
                self._slots.release()
                _close_socket(request)

    def _handle_connection(self, request: socket.socket, address: tuple, timer: threading.Timer) -> None:
        try:
            self.finish_request(request, address)
        except Exception:
            # Handlers send bounded errors where possible. Never log input,
            # headers, tokens, callback exception messages, or tracebacks.
            pass
        finally:
            timer.cancel()
            _close_socket(request)
            timer.join(timeout=0.1)
            with self._lock:
                self._connections.discard(request)
                self._timers.discard(timer)
                self._workers.discard(threading.current_thread())
            self._slots.release()

    def serve_forever(self, poll_interval: float = 0.1) -> None:
        with self._lock:
            if self._closing:
                return
            self._serving_thread = threading.current_thread()
        try:
            super().serve_forever(poll_interval=min(0.5, max(0.01, poll_interval)))
        finally:
            with self._lock:
                self._serving_thread = None

    def _begin_close(self) -> None:
        with self._lock:
            self._closing = True
            connections, timers = tuple(self._connections), tuple(self._timers)
        for timer in timers:
            timer.cancel()
        for connection in connections:
            _close_socket(connection)

    def shutdown(self) -> None:
        self._begin_close()
        with self._lock:
            serving = self._serving_thread
        if serving is not None:
            if serving is threading.current_thread():
                raise RuntimeError("shutdown must be called from another thread")
            super().shutdown()

    def server_close(self) -> None:
        self.shutdown()
        super().server_close()
        deadline = time.monotonic() + _JOIN_SECONDS
        with self._lock:
            workers = tuple(self._workers)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=max(0.0, deadline - time.monotonic()))


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server: _LocalServer

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._error(code)

    def _respond(self, status: int, body: bytes, mime: str, *, allow: str | None = None) -> None:
        self.close_connection = True
        self.send_response_only(status)
        self.send_header("Date", self.date_time_string())
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        if allow is not None:
            self.send_header("Allow", allow)
        if status == 429:
            self.send_header("Retry-After", "1")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str | None = None, *, allow: str | None = None) -> None:
        body = _encode_object({"error": message or _ERRORS.get(status, "Invalid request.")})
        self._respond(status, body, "application/json; charset=utf-8", allow=allow)

    def _one(self, name: str) -> str | None:
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise RequestError()
        return values[0].strip(" \t") if values else None

    def _check_headers(self) -> int:
        authority = self._one("Host")
        if authority is None:
            raise RequestError()
        if authority != self.server.authority:
            raise RequestError(403, _ERRORS[403])
        origin = self._one("Origin")
        if origin is not None and origin != self.server.origin:
            raise RequestError(403, _ERRORS[403])
        if self.headers.get_all("Transfer-Encoding"):
            raise RequestError()
        if self.headers.get_all("Expect"):
            raise RequestError(417, _ERRORS[417])
        if self.headers.get_all("Content-Encoding"):
            raise RequestError(415, _ERRORS[415])
        content_length = self._one("Content-Length")
        length = 0
        if content_length is not None:
            if re.fullmatch(r"[0-9]{1,5}", content_length) is None:
                raise RequestError()
            length = int(content_length)
            if length > _MAX_BODY:
                raise RequestError(413, _ERRORS[413])
        content_type = self._one("Content-Type")
        token = self._one("X-CSRF-Token")
        if self.command == "POST":
            if origin != self.server.origin or token is None or _TOKEN.fullmatch(token) is None:
                raise RequestError(403, _ERRORS[403])
            if not secrets.compare_digest(token, self.server.session_token):
                raise RequestError(403, _ERRORS[403])
            if content_length is None:
                raise RequestError(411, _ERRORS[411])
            if content_type is None or _CONTENT_TYPE.fullmatch(content_type) is None:
                raise RequestError(415, _ERRORS[415])
        elif length:
            raise RequestError()
        return length

    def handle_one_request(self) -> None:
        self.close_connection = True
        self.request_version = "HTTP/1.0"
        self.requestline = ""
        self.command = ""
        try:
            self.raw_requestline = self.rfile.readline(_MAX_REQUEST_LINE + 1)
            if not self.raw_requestline:
                return
            if len(self.raw_requestline) > _MAX_REQUEST_LINE:
                self._error(414)
                return
            if not self.raw_requestline.endswith(b"\r\n"):
                raise RequestError()
            parts = self.raw_requestline[:-2].split(b" ")
            if len(parts) != 3 or _HEADER_NAME.fullmatch(parts[0]) is None:
                raise RequestError()
            if parts[2] not in (b"HTTP/1.0", b"HTTP/1.1"):
                self._error(505)
                return
            target = parts[1].decode("ascii", errors="strict")
            if not _valid_route(target):
                raise RequestError()
            body_stream = self.rfile
            try:
                self.rfile = _HeaderReader(body_stream)
                if not super().parse_request():
                    return
            finally:
                self.rfile = body_stream
            self.close_connection = True
            length = self._check_headers()
            self._dispatch(length)
            self.wfile.flush()
        except RequestError as error:
            self._error(error.status, error.public_message)
        except (UnicodeError, ValueError, TypeError, RecursionError):
            self._error(400)
        except TimeoutError:
            self._error(408)
        except OSError:
            pass

    def _dispatch(self, length: int) -> None:
        if self.path in self.server.assets:
            if self.command not in ("GET", "HEAD"):
                self._error(405, allow="GET, HEAD")
                return
            mime, body = self.server.assets[self.path]
            self._respond(200, body, mime)
            return
        if self.path == _SESSION_PATH:
            if self.command not in ("GET", "HEAD"):
                self._error(405, allow="GET, HEAD")
                return
            self._respond(200, _encode_object({"token": self.server.session_token}), "application/json; charset=utf-8")
            return
        callback = self.server.endpoints.get(self.path)
        if callback is None:
            self._error(404)
            return
        if self.command != "POST":
            self._error(405, allow="POST")
            return
        body = self.rfile.read(length)
        if len(body) != length:
            raise RequestError()
        payload = _decode_object(body)
        if not self.server.compute_slot.acquire(blocking=False):
            self._error(429)
            return
        try:
            with self.server._lock:
                closing = self.server._closing
            if closing:
                self._error(503)
                return
            try:
                result = callback(payload)
            except RequestError:
                raise
            except (ValueError, TypeError):
                raise RequestError() from None
            except Exception:
                self._error(500)
                return
            try:
                encoded = _encode_object(result)
            except (ValueError, TypeError, RecursionError, OverflowError):
                self._error(500)
                return
            self._respond(200, encoded, "application/json; charset=utf-8")
        finally:
            self.server.compute_slot.release()


def create_server(
    port: int,
    assets: Mapping[str, tuple[str, bytes]],
    endpoints: Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]],
) -> _LocalServer:
    """Validate fixed configuration and create an unstarted loopback-only server."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be an integer between 0 and 65535")
    if not isinstance(assets, Mapping) or not isinstance(endpoints, Mapping):
        raise TypeError("assets and endpoints must be mappings")
    if not 1 <= len(assets) <= _MAX_ASSETS or len(endpoints) > 16:
        raise ValueError("too many assets or endpoints, or no root asset")
    copied_assets = dict(assets)
    copied_endpoints = dict(endpoints)
    if "/" not in copied_assets or _SESSION_PATH in copied_assets or _SESSION_PATH in copied_endpoints:
        raise ValueError("a root asset is required and /api/session is reserved")
    total = 0
    for path, asset in copied_assets.items():
        if not _valid_route(path) or path in copied_endpoints or not isinstance(asset, tuple) or len(asset) != 2:
            raise ValueError("invalid or overlapping asset route")
        mime, body = asset
        if not isinstance(mime, str) or not 1 <= len(mime) <= 128 or any(not 32 <= ord(character) <= 126 for character in mime):
            raise ValueError("invalid asset content type")
        if type(body) is not bytes:
            raise TypeError("asset content must be preloaded bytes")
        total += len(body)
    if total > _MAX_ASSET_BYTES:
        raise ValueError("assets exceed the preload limit")
    for path, callback in copied_endpoints.items():
        if not _valid_route(path) or not path.startswith("/api/") or not callable(callback):
            raise ValueError("invalid API route or callback")
    return _LocalServer(port, copied_assets, copied_endpoints)
