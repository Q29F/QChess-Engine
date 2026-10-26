"""Real-process UCI protocol tests with bounded reads and process cleanup."""
import io
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import unittest

import chess

from src.uci import UCIProtocol


ROOT = Path(__file__).resolve().parents[1]


class UCISession:
    def __init__(self, *, direct_script=False):
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        arguments = [str(ROOT / "src" / "qchess.py")] if direct_script else ["-m", "src.qchess"]
        self.process = subprocess.Popen(
            [sys.executable, *arguments], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=environment,
        )
        self.lines = queue.Queue()
        self.transcript = []
        self.errors = []

        def read_stdout():
            for line in self.process.stdout:
                line = line.rstrip("\r\n")
                self.transcript.append(line)
                self.lines.put(line)
            self.lines.put(None)

        def read_stderr():
            for line in self.process.stderr:
                self.errors.append(line)

        self.readers = [
            threading.Thread(target=read_stdout, daemon=True),
            threading.Thread(target=read_stderr, daemon=True),
        ]
        for reader in self.readers:
            reader.start()

    def send(self, command):
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def until(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        received = []
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise queue.Empty
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                raise AssertionError(
                    f"Timed out waiting for UCI response; received={received!r}; stderr={''.join(self.errors)!r}"
                ) from None
            if line is None:
                raise AssertionError(f"Unexpected UCI EOF; stderr={''.join(self.errors)!r}")
            received.append(line)
            if predicate(line):
                return received

    def ready(self):
        self.send("isready")
        return self.until(lambda line: line == "readyok")

    def bestmove(self):
        return self.until(lambda line: line.startswith("bestmove "))[-1].split()[1]

    def close(self):
        try:
            if self.process.poll() is None:
                try:
                    if not self.process.stdin.closed:
                        self.send("quit")
                except (BrokenPipeError, OSError):
                    pass
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
        finally:
            for reader in self.readers:
                reader.join(timeout=1)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()


class UCIProcessTests(unittest.TestCase):
    def session(self, *, direct_script=False):
        session = UCISession(direct_script=direct_script)
        self.addCleanup(session.close)
        session.send("uci")
        session.until(lambda line: line == "uciok")
        return session

    def assert_legal_bestmove(self, session, board):
        move = chess.Move.from_uci(session.bestmove())
        self.assertIn(move, board.legal_moves)
        return move

    def test_direct_script_launch_supports_uci(self):
        session = self.session(direct_script=True)
        session.ready()

    def test_handshake_and_readiness_use_clean_protocol_stdout(self):
        session = self.session()
        session.ready()
        self.assertTrue(any(line.startswith("id name ") for line in session.transcript))
        self.assertTrue(any(line.startswith("id author ") for line in session.transcript))
        session.send("position startpos")
        session.send("go depth 1")
        self.assert_legal_bestmove(session, chess.Board())
        session.ready()
        for line in session.transcript:
            self.assertTrue(
                line in ("uciok", "readyok") or line.startswith(("id ", "option ", "info ", "bestmove ")),
                f"Non-protocol output on stdout: {line!r}",
            )

    def test_startpos_move_sequence_produces_a_legal_reply(self):
        session = self.session()
        session.send("ucinewgame")
        session.send("position startpos moves e2e4 c7c5 g1f3")
        session.send("go nodes 32")
        board = chess.Board()
        for move in ("e2e4", "c7c5", "g1f3"):
            board.push_uci(move)
        self.assert_legal_bestmove(session, board)

    def test_fen_with_moves_is_supported(self):
        session = self.session()
        fen = "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"
        session.send(f"position fen {fen} moves e1g1")
        session.send("go movetime 20")
        board = chess.Board(fen)
        board.push_uci("e1g1")
        self.assert_legal_bestmove(session, board)

    def test_malformed_position_does_not_partially_replace_current_position(self):
        session = self.session()
        session.send("position startpos moves e2e4 e7e5")
        # Applying only the first move would leave Black to move, unlike the retained position.
        session.send("position startpos moves d2d4 e7e9")
        session.send("go depth 1")
        board = chess.Board()
        board.push_uci("e2e4")
        board.push_uci("e7e5")
        self.assert_legal_bestmove(session, board)

    def test_checkmate_returns_the_uci_no_move_token(self):
        session = self.session()
        session.send("position fen 7k/6Q1/5K2/8/8/8/8/8 b - - 0 1")
        session.send("go depth 1")
        self.assertEqual(session.bestmove(), "0000")

    def test_mate_in_one_is_reported_as_uci_mate_distance(self):
        session = self.session()
        board = chess.Board("7k/8/5KQ1/8/8/8/8/8 w - - 0 1")
        session.send(f"position fen {board.fen()}")
        session.send("go depth 2")
        board.push(self.assert_legal_bestmove(session, board))
        self.assertTrue(board.is_checkmate())
        self.assertTrue(any("score mate 1" in line for line in session.transcript))

    def test_isready_and_stop_remain_responsive_during_search(self):
        session = self.session()
        session.send("position startpos")
        session.send("go infinite")
        before_stop = session.ready()
        self.assertFalse(any(line.startswith("bestmove ") for line in before_stop))
        session.send("stop")
        self.assert_legal_bestmove(session, chess.Board())
        session.ready()
        self.assertEqual(sum(line.startswith("bestmove ") for line in session.transcript), 1)

    def test_malformed_search_limits_do_not_crash_the_protocol(self):
        session = self.session()
        invalid_commands = (
            "go depth nan", "go nodes invalid", "go movetime nan",
            "go depth -1", "go nodes -1", "go movetime -1",
            "go depth 2147483648", "go nodes 2147483648", "go movetime 2147483648",
            "go depth 10000000000", "go nodes 10000000000", "go movetime 00000000001",
        )
        for command in invalid_commands:
            session.send(command)
        responses = session.ready()
        self.assertEqual(sum(line.startswith("info string error:") for line in responses), len(invalid_commands))
        self.assertFalse(any(line.startswith("bestmove ") for line in session.transcript))
        # Maximum permitted integers are accepted and clamped to the engine's
        # resource limits. A tiny time/node budget keeps these checks cheap.
        for command in (
            "go depth 2147483647 nodes 2147483647 movetime 1",
            "go depth 2147483647 nodes 1 movetime 2147483647",
        ):
            session.send(command)
            self.assert_legal_bestmove(session, chess.Board())
        self.assertFalse(any("search failed" in line for line in session.transcript))

    def test_oversized_physical_line_exits_two_and_stops_search(self):
        session = self.session()
        session.send("go infinite")
        session.ready()
        session.send("x" * 16_385)
        session.until(lambda line: line.startswith("info string error:") and "too long" in line)
        self.assertEqual(session.process.wait(timeout=5), 2)

    def test_quit_stops_an_active_search_and_exits(self):
        session = self.session()
        session.send("go infinite")
        session.ready()
        session.send("quit")
        self.assertEqual(session.process.wait(timeout=5), 0)

    def test_stdin_eof_stops_an_active_search_and_exits(self):
        session = self.session()
        session.send("go infinite")
        session.ready()
        session.process.stdin.close()
        self.assertEqual(session.process.wait(timeout=5), 0)


class UCIInputBoundaryTests(unittest.TestCase):
    def protocol(self):
        output = io.StringIO()
        protocol = UCIProtocol(input_stream=io.StringIO(), output_stream=output)
        self.addCleanup(protocol.close)
        return protocol, output

    def test_fen_accepts_1024_history_moves_and_rejects_1025_atomically(self):
        protocol, output = self.protocol()
        protocol.engine.make_move("e2e4")
        moves = ["g1f3", "g8f6", "f3g1", "f6g8"] * 256
        command = f"position fen {chess.STARTING_FEN} moves " + " ".join(moves)
        self.assertTrue(protocol.handle_command(command))
        self.assertEqual(output.getvalue(), "")
        expected = chess.Board()
        for move in moves:
            expected.push_uci(move)
        self.assertEqual(protocol.engine.move_history, moves)
        self.assertEqual(protocol.engine.get_board_state(), expected.fen())
        before = protocol.engine.get_board_state(), protocol.engine.move_history
        self.assertTrue(protocol.handle_command(command + " g1f3"))
        self.assertIn("info string error:", output.getvalue())
        self.assertEqual((protocol.engine.get_board_state(), protocol.engine.move_history), before)

    def test_fen_counters_enforce_length_range_and_fullmove_minimum(self):
        protocol, output = self.protocol()
        placement = " ".join(chess.STARTING_FEN.split()[:4])
        for halfmove, fullmove in (
            ("0", "1"), ("0000000000", "0000000001"),
            ("2147483647", "2147483647"),
        ):
            with self.subTest(accepted=(halfmove, fullmove)):
                protocol.handle_command(f"position fen {placement} {halfmove} {fullmove}")
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(protocol.engine.board.halfmove_clock, int(halfmove))
                self.assertEqual(protocol.engine.board.fullmove_number, int(fullmove))
        before = protocol.engine.get_board_state(), protocol.engine.move_history
        invalid_counters = (
            ("-1", "1"), ("+1", "1"), ("1.0", "1"), ("1e1", "1"),
            ("0", "0"), ("0", "-1"), ("0", "+1"), ("0", "1.0"),
            ("2147483648", "1"), ("0", "2147483648"),
            ("10000000000", "1"), ("0", "10000000000"),
            ("00000000000", "1"), ("0", "00000000001"),
        )
        for halfmove, fullmove in invalid_counters:
            with self.subTest(rejected=(halfmove, fullmove)):
                output.seek(0)
                output.truncate()
                protocol.handle_command(f"position fen {placement} {halfmove} {fullmove}")
                self.assertIn("info string error:", output.getvalue())
                self.assertEqual((protocol.engine.get_board_state(), protocol.engine.move_history), before)

    def test_replayed_moves_cannot_overflow_fen_counters(self):
        protocol, output = self.protocol()
        placement = " ".join(chess.STARTING_FEN.split()[:4])
        for counters, moves, expected in (
            ("0 2147483647", "e2e4", (0, 2147483647)),
            ("2147483647 1", "e2e4", (0, 1)),
        ):
            with self.subTest(accepted=(counters, moves)):
                protocol.handle_command(f"position fen {placement} {counters} moves {moves}")
                self.assertEqual(output.getvalue(), "")
                self.assertEqual((protocol.engine.board.halfmove_clock, protocol.engine.board.fullmove_number), expected)

        protocol.engine.set_board_state(chess.STARTING_FEN)
        protocol.engine.make_move("e2e4")
        before = protocol.engine.get_board_state(), protocol.engine.move_history
        for counters, moves in (
            ("0 2147483647", "e2e4 e7e5"),
            ("2147483647 1", "g1f3"),
            ("2147483646 1", "g1f3 g8f6 e2e4"),
        ):
            with self.subTest(rejected=(counters, moves)):
                output.seek(0)
                output.truncate()
                protocol.handle_command(f"position fen {placement} {counters} moves {moves}")
                self.assertIn("info string error:", output.getvalue())
                self.assertEqual((protocol.engine.get_board_state(), protocol.engine.move_history), before)


if __name__ == "__main__":
    unittest.main()
