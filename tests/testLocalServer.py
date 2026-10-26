"""Loopback transport checks using real sockets and bounded test callbacks."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
import io
import json
import secrets
import socket
import threading
import time
import unittest
from unittest.mock import patch

import localServer


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []

        def echo(payload):
            self.calls.append(payload)
            return {"received": payload}

        def blocked(payload):
            self.entered.set()
            self.release.wait(4)
            return {"done": True}

        def public_error(payload):
            raise localServer.RequestError(422, "Unsupported calculation.")

        def invalid(payload):
            raise ValueError("private-operation-detail")

        def broken(payload):
            raise RuntimeError("private-operation-detail")

        self.server = localServer.create_server(0, {
            "/": ("text/html; charset=utf-8", b'<!doctype html><meta name="csrf-token" content="{{csrf-token}}"><h1>Local test</h1>'),
            "/style.css": ("text/css; charset=utf-8", b"body { color: black; }"),
        }, {
            "/api/echo": echo,
            "/api/blocked": blocked,
            "/api/public-error": public_error,
            "/api/invalid": invalid,
            "/api/broken": broken,
            "/api/nonfinite": lambda payload: {"value": float("inf")},
        })
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.release.set()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive(), "The server thread must stop during cleanup")

    def wire(self, method="GET", path="/", headers=None, body=b""):
        if headers is None:
            headers = [("Host", self.server.authority)]
        lines = [f"{method} {path} HTTP/1.1", *(f"{key}: {value}" for key, value in headers), "", ""]
        return "\r\n".join(lines).encode("ascii") + body

    def post_headers(self, body):
        return [
            ("Host", self.server.authority),
            ("Origin", self.server.origin),
            ("X-CSRF-Token", self.server.session_token),
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ]

    def exchange(self, request):
        with socket.create_connection(self.server.server_address, timeout=3) as connection:
            connection.sendall(request)
            connection.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                try:
                    chunk = connection.recv(65536)
                except ConnectionResetError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        response = b"".join(chunks)
        self.assertTrue(response.startswith(b"HTTP/"), "A complete request must receive a status response")
        header, body = response.split(b"\r\n\r\n", 1)
        lines = header.decode("ascii").split("\r\n")
        status = int(lines[0].split()[1])
        headers = dict(line.lower().split(": ", 1) for line in lines[1:])
        return status, headers, body

    def post(self, body=b"{}", path="/api/echo", headers=None):
        return self.exchange(self.wire("POST", path, self.post_headers(body) if headers is None else headers, body))

    def test_assets_session_and_successful_post_have_security_headers(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, headers, body = self.exchange(self.wire())
        self.assertEqual(status, 200)
        self.assertNotIn(b"{{csrf-token}}", body)
        self.assertTrue(self.server.session_token.encode("ascii") in body)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(headers["cross-origin-resource-policy"], "same-origin")
        self.assertEqual(headers["connection"], "close")
        self.assertNotIn("server", headers)
        self.assertNotIn("access-control-allow-origin", headers)
        csp = headers["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        status, _, body = self.exchange(self.wire(path="/api/session"))
        self.assertEqual(status, 200)
        session = json.loads(body)
        self.assertEqual(set(session), {"token"})
        self.assertTrue(secrets.compare_digest(session["token"], self.server.session_token))
        status, _, body = self.post(b'{"value":42}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"received": {"value": 42}})

    def test_tokens_are_distinct_for_each_server_instance(self):
        with localServer.create_server(0, {"/": ("text/html", b"ready")}, {}) as another:
            self.assertFalse(secrets.compare_digest(self.server.session_token, another.session_token))

    def test_head_has_no_body_and_routes_are_fixed(self):
        status, headers, body = self.exchange(self.wire("HEAD", "/style.css"))
        self.assertEqual(status, 200)
        self.assertGreater(int(headers["content-length"]), 0)
        self.assertEqual(body, b"")
        for path, expected in (("/missing", 404), ("/../entropy.py", 400), ("/%2e%2e/entropy.py", 400), ("/style.css?path=entropy.py", 400)):
            with self.subTest(path=path):
                self.assertEqual(self.exchange(self.wire(path=path))[0], expected)
        self.assertEqual(self.exchange(self.wire("GET", "/api/echo"))[0], 405)
        self.assertEqual(self.exchange(self.wire("OPTIONS", "/api/echo"))[0], 405)
        self.assertEqual(self.post(path="/style.css")[0], 405)
        self.assertEqual(self.post(path="/missing")[0], 404)

    def test_exact_host_origin_and_session_token_are_required(self):
        normal = self.post_headers(b"{}")
        cases = (
            ("Host", None, 400), ("Host", "localhost:80", 403),
            ("Host", self.server.authority + ".example", 403),
            ("Origin", None, 403), ("Origin", "null", 403),
            ("Origin", self.server.origin + ".example", 403),
            ("Origin", self.server.origin + "/", 403),
            ("X-CSRF-Token", None, 403), ("X-CSRF-Token", "0" * 64, 403),
        )
        for name, replacement, expected in cases:
            headers = [(key, value) for key, value in normal if key != name]
            if replacement is not None:
                headers.append((name, replacement))
            with self.subTest(header=name, missing=replacement is None):
                self.assertEqual(self.post(headers=headers)[0], expected)
        self.assertEqual(self.calls, [])

    def test_duplicate_security_and_framing_headers_are_rejected(self):
        normal = self.post_headers(b"{}")
        for name in ("Host", "Origin", "X-CSRF-Token", "Content-Length", "Content-Type"):
            duplicate = next(pair for pair in normal if pair[0] == name)
            with self.subTest(header=name):
                self.assertEqual(self.post(headers=[*normal, duplicate])[0], 400)
        self.assertEqual(self.calls, [])

    def test_content_length_and_body_limit_are_enforced(self):
        normal = self.post_headers(b"{}")
        for value, expected in ((None, 411), ("-1", 400), ("+2", 400), ("2,2", 400), ("000002", 400), ("16385", 413)):
            headers = [(name, item) for name, item in normal if name != "Content-Length"]
            if value is not None:
                headers.append(("Content-Length", value))
            with self.subTest(length=value):
                self.assertEqual(self.post(headers=headers)[0], expected)
        too_short = [(name, "10" if name == "Content-Length" else value) for name, value in normal]
        self.assertEqual(self.post(headers=too_short)[0], 400)
        boundary = b'{"value":"' + b"x" * (16384 - len(b'{"value":""}')) + b'"}'
        self.assertEqual(len(boundary), 16384)
        self.assertEqual(self.post(boundary)[0], 200)

    def test_ambiguous_encoding_and_unsupported_expectation_are_rejected(self):
        normal = self.post_headers(b"{}")
        for name, value, expected in (
            ("Transfer-Encoding", "chunked", 400),
            ("Expect", "100-continue", 417),
            ("Content-Encoding", "gzip", 415),
            ("Content-Type", "text/plain", 415),
            ("Content-Type", "application/json; charset=latin1", 415),
        ):
            headers = [(key, item) for key, item in normal if key != name] + [(name, value)]
            with self.subTest(header=name, value=value):
                self.assertEqual(self.post(headers=headers)[0], expected)

    def test_malformed_nonfinite_and_excessively_nested_json_is_rejected(self):
        for body in (
            b"", b"[]", b"true", b"null", b'{"a":1,"a":2}',
            b'{"value":NaN}', b'{"value":Infinity}', b'{"value":1e999}',
            b'{"value":"\\ud800"}', b'{"\\udfff":1}', b'{"value":"\xff"}',
            b'{"value":' + b"[" * 9 + b"0" + b"]" * 9 + b"}",
        ):
            with self.subTest(body=repr(body)):
                self.assertEqual(self.post(body)[0], 400)
        self.assertEqual(self.calls, [])

    def test_header_request_line_and_header_count_limits(self):
        ordinary = [("Host", self.server.authority)]
        requests = (
            (self.wire(path="/" + "a" * 1100), 414),
            (self.wire(headers=[*ordinary, ("X-Large", "a" * 8200)]), 431),
            (self.wire(headers=[*ordinary, *((f"X-{index}", "a") for index in range(32))]), 431),
            (self.wire(headers=[*ordinary, *((f"X-{index}", "a" * 4100) for index in range(4))]), 431),
            (f"GET / HTTP/1.1\r\nHost: {self.server.authority}\r\n Folded: header\r\n\r\n".encode("ascii"), 431),
        )
        for index, (request, expected) in enumerate(requests):
            with self.subTest(case=index):
                self.assertEqual(self.exchange(request)[0], expected)

    def test_only_one_request_is_served_per_connection(self):
        request = self.wire() + self.wire(path="/style.css")
        status, headers, body = self.exchange(request)
        self.assertEqual(status, 200)
        self.assertEqual(len(body), int(headers["content-length"]))
        self.assertNotIn(b"HTTP/1.0", body)

    def test_errors_are_bounded_json_without_internal_details_or_stderr(self):
        output = io.StringIO()
        with redirect_stderr(output):
            for path, expected in (("/api/public-error", 422), ("/api/invalid", 400), ("/api/broken", 500), ("/api/nonfinite", 500)):
                with self.subTest(path=path):
                    status, headers, body = self.post(path=path)
                    self.assertEqual(status, expected)
                    self.assertEqual(headers["cache-control"], "no-store")
                    error = json.loads(body)
                    self.assertEqual(set(error), {"error"})
                    self.assertNotIn("private-operation-detail", error["error"])
                    self.assertLessEqual(len(error["error"]), 160)
        self.assertEqual(output.getvalue(), "")

    def test_one_computation_runs_while_other_requests_remain_bounded(self):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.post, path="/api/blocked")
            try:
                self.assertTrue(self.entered.wait(2), "The first calculation must start")
                status, headers, _ = self.post()
                self.assertEqual(status, 429)
                self.assertEqual(headers["retry-after"], "1")
                self.assertEqual(self.exchange(self.wire())[0], 200)
                self.assertEqual(self.calls, [])
            finally:
                self.release.set()
            self.assertEqual(future.result(timeout=2)[0], 200)
        self.assertEqual(self.post()[0], 200)

    def test_idle_request_reads_time_out(self):
        with patch.object(localServer, "_IDLE_SECONDS", 0.2):
            with socket.create_connection(self.server.server_address, timeout=2) as connection:
                connection.sendall(b"GET / HTTP/1.1\r\n")
                started = time.monotonic()
                response = connection.recv(4096)
                self.assertIn(b" 408 ", response.split(b"\r\n", 1)[0])
                self.assertLess(time.monotonic() - started, 1.5)

    def test_absolute_deadline_stops_a_client_that_keeps_sending(self):
        with patch.object(localServer, "_IDLE_SECONDS", 0.5), patch.object(localServer, "_CONNECTION_SECONDS", 0.6):
            with socket.create_connection(self.server.server_address, timeout=2) as connection:
                connection.sendall(b"GET / HTTP/1.1\r\nX-Slow: ")
                started = time.monotonic()
                closed = threading.Event()

                def receive():
                    try:
                        while connection.recv(1024):
                            pass
                    except (ConnectionResetError, OSError):
                        pass
                    finally:
                        closed.set()

                reader = threading.Thread(target=receive)
                reader.start()
                try:
                    while not closed.wait(0.06) and time.monotonic() - started < 1.5:
                        try:
                            connection.sendall(b"x")
                        except OSError:
                            break
                    self.assertTrue(closed.wait(0.5), "The absolute deadline must stop an active slow client")
                    self.assertLess(time.monotonic() - started, 1.5)
                finally:
                    connection.close()
                    reader.join(timeout=2)
                self.assertFalse(reader.is_alive())

    def test_eight_connection_limit_rejects_excess_admission(self):
        connections = []
        try:
            for _ in range(8):
                connection = socket.create_connection(self.server.server_address, timeout=2)
                connection.sendall(b"GET / HTTP/1.1\r\n")
                connections.append(connection)
            with socket.create_connection(self.server.server_address, timeout=1) as excess:
                try:
                    excess.sendall(self.wire())
                    self.assertEqual(excess.recv(1024), b"")
                except ConnectionResetError:
                    pass
        finally:
            for connection in connections:
                connection.close()

    def test_shutdown_closes_partial_requests_and_listener(self):
        connection = socket.create_connection(self.server.server_address, timeout=2)
        self.addCleanup(connection.close)
        connection.sendall(b"GET / HTTP/1.1\r\nHost: ")
        self.assertEqual(self.exchange(self.wire())[0], 200)
        started = time.monotonic()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        self.assertLess(time.monotonic() - started, 2)
        try:
            self.assertEqual(connection.recv(1024), b"")
        except ConnectionResetError:
            pass
        with self.assertRaises(OSError):
            socket.create_connection(self.server.server_address, timeout=0.2)


if __name__ == "__main__":
    unittest.main()
