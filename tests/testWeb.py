"""Application integration through the actual fixed loopback routes."""

import http.client
import io
import json
import threading
import unittest

import chess
import chess.pgn

from web import create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        self.server = create_app(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def request(self, path, payload=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            if payload is None:
                connection.request("GET", path)
            else:
                connection.request("POST", path, json.dumps(payload).encode("utf-8"), {
                    "Content-Type": "application/json",
                    "Origin": self.server.origin,
                    "X-CSRF-Token": self.server.session_token,
                })
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_public_assets_and_health_are_served_without_caching(self):
        for path, mime in (("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css"), ("/icon.svg", "image/svg+xml")):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(mime))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertGreater(len(body), 0)
        status, _, body = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok", "application": "qchess-workbench"})

    def test_state_round_trips_without_persisting_another_requests_game(self):
        board = chess.Board()
        board.push_uci("e2e4")
        status, _, body = self.request("/api/state", {"initialFen": chess.STARTING_FEN, "moves": ["e2e4"]})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["fen"], board.fen())
        self.assertEqual(result["san"], ["e4"])
        self.assertEqual(result["turn"], "black")
        status, _, body = self.request("/api/state", {"initialFen": chess.STARTING_FEN, "moves": []})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["fen"], chess.STARTING_FEN)

    def test_search_returns_a_legal_move_and_the_actual_following_position(self):
        status, _, body = self.request("/api/search", {"initialFen": chess.STARTING_FEN, "moves": [], "depth": 1})
        self.assertEqual(status, 200)
        result = json.loads(body)
        board = chess.Board()
        move = chess.Move.from_uci(result["search"]["move"])
        self.assertIn(move, board.legal_moves)
        board.push(move)
        self.assertEqual(result["fen"], chess.STARTING_FEN)
        self.assertEqual(result["nextState"]["fen"], board.fen())
        self.assertEqual(result["nextState"]["moves"], [move.uci()])

    def test_announced_draw_claim_round_trips_without_playing_the_move(self):
        fen = "4k3/8/8/8/8/8/1R6/4K3 w - - 99 80"
        status, _, body = self.request("/api/claim", {
            "initialFen": fen, "moves": [], "reason": "fiftyMoves", "intendedMove": "b2b3",
        })
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["fen"], fen)
        self.assertEqual(result["moves"], [])
        self.assertEqual(result["outcome"]["result"], "1/2-1/2")
        self.assertEqual(result["claim"]["san"], "Rb3")
        game = chess.pgn.read_game(io.StringIO(result["pgn"]))
        self.assertEqual(game.errors, [])
        self.assertEqual(list(game.mainline_moves()), [])
        self.assertEqual(game.headers["Result"], "1/2-1/2")

    def test_invalid_semantic_payload_returns_a_bounded_client_error(self):
        for path, payload in (
            ("/api/state", {"initialFen": chess.STARTING_FEN, "moves": ["e2e5"]}),
            ("/api/search", {"initialFen": chess.STARTING_FEN, "moves": [], "depth": True}),
            ("/api/claim", {"initialFen": chess.STARTING_FEN, "moves": [], "reason": "threefold", "intendedMove": None}),
        ):
            with self.subTest(path=path):
                status, headers, body = self.request(path, payload)
                self.assertEqual(status, 400)
                self.assertEqual(headers["Cache-Control"], "no-store")
                error = json.loads(body)
                self.assertEqual(set(error), {"error"})
                self.assertLessEqual(len(error["error"]), 160)
                self.assertNotIn("Traceback", error["error"])

    def test_source_and_configuration_files_are_not_public_routes(self):
        for path in ("/workbench.py", "/web.py", "/src/engine.py", "/requirements.txt", "/package.json", "/.git/config"):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(body), {"error": "Not found."})


if __name__ == "__main__":
    unittest.main()
