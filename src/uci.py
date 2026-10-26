"""A bounded UCI transport with cancellable search and atomic positions."""

from __future__ import annotations

import sys
import threading
from typing import TextIO

import chess

from .engine import ChessEngine, MAX_DEPTH, MAX_NODES, MAX_TIME_SECONDS
from .evaluation import MATE_SCORE
from .utils import MAX_COMMAND_LENGTH, format_output, log_message, parse_input

MAX_POSITION_MOVES = 1_024
MAX_INTEGER = 2_147_483_647


def _integer(value: str, name: str, *, minimum: int = 0) -> int:
    if not value.isascii() or not value.isdigit() or len(value) > 10:
        raise ValueError(f"{name} must be an unsigned integer of at most 10 digits")
    result = int(value)
    if not minimum <= result <= MAX_INTEGER:
        raise ValueError(f"{name} is outside the supported integer range")
    return result


class UCIProtocol:
    """Read commands on one thread while a single worker searches a snapshot."""

    def __init__(
        self,
        engine: ChessEngine | None = None,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
    ) -> None:
        self.engine = engine if engine is not None else ChessEngine()
        self.input_stream = input_stream if input_stream is not None else sys.stdin
        self.output_stream = output_stream if output_stream is not None else sys.stdout
        self._lock = threading.RLock()
        self._generation = 0
        self._worker: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._closed = False

    def _emit(self, *lines: str) -> None:
        with self._lock:
            for line in lines:
                self.output_stream.write(format_output(line) + "\n")
            self.output_stream.flush()

    def _error(self, error: Exception) -> None:
        # Error text can contain a rejected FEN. Never echo control characters.
        message = "".join(
            character if 32 <= ord(character) < 127 else " "
            for character in str(error)[:240]
        )
        self._emit("info string error: " + message)

    def _stop_search(self, *, suppress: bool) -> None:
        with self._lock:
            if suppress:
                self._generation += 1
            worker, event = self._worker, self._stop_event
        if event is not None:
            event.set()
        if worker is not None:
            worker.join(timeout=2.0)
            if worker.is_alive():
                # Never accumulate workers if a future implementation breaks
                # the search cancellation contract.
                with self._lock:
                    self._closed = True
                    self._generation += 1
                raise RuntimeError("search failed to stop within two seconds")
        self._worker = None
        self._stop_event = None

    def _position(self, arguments: list[str]) -> None:
        if not arguments:
            raise ValueError("position requires startpos or a complete FEN")
        if arguments[0] == "startpos":
            candidate = chess.Board()
            remaining = arguments[1:]
        elif arguments[0] == "fen" and len(arguments) >= 7:
            _integer(arguments[5], "FEN halfmove clock")
            _integer(arguments[6], "FEN fullmove number", minimum=1)
            candidate = chess.Board(" ".join(arguments[1:7]))
            if not candidate.is_valid():
                raise ValueError("FEN is not a valid standard chess position")
            remaining = arguments[7:]
        else:
            raise ValueError("position requires startpos or six FEN fields")
        if remaining:
            if remaining[0] != "moves":
                raise ValueError("unexpected text after the position")
            moves = remaining[1:]
            if len(moves) > MAX_POSITION_MOVES:
                raise ValueError("position has too many moves")
            for text in moves:
                if len(text) not in (4, 5):
                    raise ValueError("position contains an invalid UCI move")
                move = candidate.parse_uci(text)
                if not move:
                    raise ValueError("null moves are not legal game moves")
                candidate.push(move)
                if (
                    candidate.halfmove_clock > MAX_INTEGER
                    or candidate.fullmove_number > MAX_INTEGER
                ):
                    raise ValueError("position moves exceed the supported FEN counter range")
        # Only a completely validated command can replace the live game.
        self._stop_search(suppress=True)
        self.engine.board = candidate

    def _go_limits(self, arguments: list[str]) -> tuple[int, int, float, bool]:
        values: dict[str, int] = {}
        infinite = False
        names = {"depth", "nodes", "movetime", "wtime", "btime", "winc", "binc", "movestogo"}
        index = 0
        while index < len(arguments):
            name = arguments[index]
            if name == "infinite":
                if infinite:
                    raise ValueError("duplicate infinite flag")
                infinite = True
                index += 1
                continue
            if name not in names:
                raise ValueError(f"unsupported go parameter: {name[:24]}")
            if name in values or index + 1 >= len(arguments):
                raise ValueError(f"duplicate or missing value for {name}")
            minimum = 1 if name in {"depth", "nodes", "movetime", "movestogo"} else 0
            values[name] = _integer(arguments[index + 1], name, minimum=minimum)
            index += 2

        timed = any(name in values for name in ("movetime", "wtime", "btime"))
        depth = min(values.get("depth", 4 if timed or infinite or "nodes" in values else 2), MAX_DEPTH)
        nodes = min(values.get("nodes", MAX_NODES if infinite else 50_000), MAX_NODES)
        seconds = MAX_TIME_SECONDS if arguments else 1.0
        if "movetime" in values:
            seconds = values["movetime"] / 1_000
        else:
            color = "w" if self.engine.board.turn == chess.WHITE else "b"
            if color + "time" in values:
                remaining = values[color + "time"] / 1_000
                increment = values.get(color + "inc", 0) / 1_000
                allocation = remaining / values.get("movestogo", 30) + increment * 0.8
                seconds = min(allocation, remaining * 0.8)
        return depth, nodes, max(0.001, min(seconds, MAX_TIME_SECONDS)), infinite

    def _go(self, arguments: list[str]) -> None:
        depth, nodes, seconds, infinite = self._go_limits(arguments)
        self._stop_search(suppress=True)
        snapshot = ChessEngine(self.engine.get_board_state())
        snapshot.board = self.engine.board.copy(stack=True)
        event = threading.Event()
        with self._lock:
            generation = self._generation
            self._stop_event = event

        def calculate() -> None:
            try:
                result = snapshot.search(
                    depth, max_nodes=nodes, time_limit=seconds, stop_event=event
                )
                # The core may recognize an automatic draw with legal moves
                # remaining. UCI uses 0000 only when no legal move is available.
                move = result.move or next(iter(snapshot.get_legal_moves()), "0000")
                score = result.score if snapshot.board.turn == chess.WHITE else -result.score
                if abs(score) >= MATE_SCORE - MAX_DEPTH:
                    mate_moves = (MATE_SCORE - abs(score) + 1) // 2
                    score_text = f"mate {mate_moves if score > 0 else -mate_moves}"
                else:
                    score_text = f"cp {score}"
                lines = [
                    f"info depth {result.depth} nodes {result.nodes} score {score_text}",
                    f"bestmove {move}",
                ]
            except Exception as error:
                log_message(f"search failed: {type(error).__name__}")
                fallback = next(iter(sorted(snapshot.get_legal_moves())), "0000")
                lines = ["info string search failed; using a legal fallback", f"bestmove {fallback}"]
            if infinite:
                # Respect UCI infinite without unbounded computation or polling.
                event.wait()
            try:
                with self._lock:
                    if generation == self._generation and not self._closed:
                        self._emit(*lines)
            except (BrokenPipeError, OSError):
                event.set()

        self._worker = threading.Thread(target=calculate, name="qchess-search", daemon=True)
        self._worker.start()

    def handle_command(self, line: str) -> bool:
        """Write responses and return False when the input loop should exit."""
        if self._closed:
            return False
        try:
            command = parse_input(line)
            if not command:
                return True
            name, *arguments = command.split()
            if name == "uci":
                self._emit("id name QChess Engine", "id author Q29F", "uciok")
            elif name == "isready":
                self._emit("readyok")
            elif name == "ucinewgame":
                if arguments:
                    raise ValueError("ucinewgame takes no arguments")
                self._stop_search(suppress=True)
                self.engine.set_board_state(chess.STARTING_FEN)
            elif name == "position":
                self._position(arguments)
            elif name == "go":
                self._go(arguments)
            elif name == "stop":
                self._stop_search(suppress=False)
            elif name == "quit":
                self.close()
                return False
            # Unknown UCI extensions, debug, and unadvertised setoption requests
            # are ignored as required by the protocol's forward compatibility.
        except (ValueError, TypeError) as error:
            self._error(error)
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._stop_search(suppress=True)

    def run(self) -> int:
        try:
            while not self._closed:
                # Bound allocation before splitting/validating input. A too-long
                # physical line terminates the stream without buffering its tail.
                line = self.input_stream.readline(MAX_COMMAND_LENGTH + 2)
                if not line:
                    break
                if len(line.removesuffix("\n").removesuffix("\r")) > MAX_COMMAND_LENGTH:
                    self._error(ValueError("command is too long; closing input"))
                    return 2
                if not self.handle_command(line):
                    break
            return 0
        except (BrokenPipeError, OSError):
            return 1
        except RuntimeError as error:
            log_message(error)
            return 1
        finally:
            self.close()


def run_uci(
    engine: ChessEngine | None = None,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    return UCIProtocol(engine, input_stream=input_stream, output_stream=output_stream).run()
