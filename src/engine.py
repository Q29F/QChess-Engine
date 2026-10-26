"""Legal chess state and deterministic, bounded alpha-beta search."""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Integral, Real
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING, TextIO

import chess

from .evaluation import (
    MATE_SCORE,
    PIECE_VALUES,
    _coerce_board,
    _material_score,
    _terminal_score,
    evaluate_position,
)

if TYPE_CHECKING:
    from .uci import UCIProtocol


MAX_DEPTH = 4
MAX_NODES = 100_000
MAX_TIME_SECONDS = 10.0
_INFINITY = 1_000_000


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Last completed iteration; score is White's perspective in centipawns.

    A depth of zero identifies an unsearched legal fallback whose score is the
    current position's evaluation. Nodes includes work in an interrupted final
    iteration. Terminal positions have no move and zero searched nodes.
    """

    move: str | None
    score: int
    depth: int
    nodes: int


class _SearchStopped(Exception):
    """Unwind a stopped search through the push/pop cleanup blocks."""


@dataclass(slots=True)
class _SearchBudget:
    max_nodes: int
    deadline: float
    stop_event: Event | None
    nodes: int = 0

    def check(self) -> None:
        if (
            (self.stop_event is not None and self.stop_event.is_set())
            or monotonic() >= self.deadline
        ):
            raise _SearchStopped

    def visit(self) -> None:
        self.check()
        if self.nodes >= self.max_nodes:
            raise _SearchStopped
        self.nodes += 1


def _integer_limit(value: int, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise TypeError(f"{name} must be an integer")
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return int(value)


def _ordered_moves(board: chess.Board) -> list[chess.Move]:
    """Search captures and promotions first, using UCI order for stable ties."""
    def ordering(move: chess.Move) -> tuple[int, str]:
        victim = board.piece_type_at(move.to_square)
        victim_value = PIECE_VALUES.get(victim, 0)
        if board.is_en_passant(move):
            victim_value = PIECE_VALUES[chess.PAWN]
        promotion_value = PIECE_VALUES.get(move.promotion, 0)
        priority = 0
        if victim_value or promotion_value:
            attacker = board.piece_type_at(move.from_square)
            priority = 10 * (victim_value + promotion_value) - PIECE_VALUES.get(attacker, 0)
        return -priority, move.uci()

    return sorted(board.legal_moves, key=ordering)


def _alpha_beta(
    board: chess.Board,
    depth: int,
    alpha: int,
    beta: int,
    ply: int,
    budget: _SearchBudget,
) -> int:
    budget.visit()
    terminal = _terminal_score(board, ply)
    if terminal is not None:
        return terminal
    if depth == 0:
        return _material_score(board)

    maximizing = board.turn == chess.WHITE
    value = -_INFINITY if maximizing else _INFINITY
    for move in _ordered_moves(board):
        budget.check()
        board.push(move)
        try:
            score = _alpha_beta(board, depth - 1, alpha, beta, ply + 1, budget)
        finally:
            board.pop()
        if maximizing:
            value = max(value, score)
            alpha = max(alpha, value)
        else:
            value = min(value, score)
            beta = min(beta, value)
        if alpha >= beta:
            break
    return value


class ChessEngine:
    """Standard chess state with validated FEN and UCI move operations."""

    def __init__(self, fen: str = chess.STARTING_FEN) -> None:
        if not isinstance(fen, str):
            raise TypeError("fen must be a string")
        self.board = _coerce_board(fen)
        self.initial_board_state = self.board.fen()
        self._uci_protocol: UCIProtocol | None = None

    @staticmethod
    def initialize_board() -> chess.Board:
        """Return a fresh starting board without changing an existing game."""
        return chess.Board()

    @property
    def current_turn(self) -> str:
        return "white" if self.board.turn == chess.WHITE else "black"

    @property
    def move_history(self) -> list[str]:
        """Return a copy of the UCI history, keeping the board authoritative."""
        return [move.uci() for move in self.board.move_stack]

    def get_board_state(self) -> str:
        return self.board.fen()

    def set_board_state(self, fen: str) -> None:
        """Replace the position and clear history only after validation succeeds."""
        if not isinstance(fen, str):
            raise TypeError("fen must be a string")
        candidate = _coerce_board(fen)
        self.board = candidate

    def get_legal_moves(self) -> list[str]:
        return sorted(move.uci() for move in self.board.legal_moves)

    def generate_moves(self) -> list[str]:
        return self.get_legal_moves()

    def make_move(self, move: str) -> None:
        """Apply a legal UCI move; invalid syntax, nulls and illegal moves fail."""
        if not isinstance(move, str):
            raise TypeError("move must be a UCI string")
        parsed = chess.Move.from_uci(move)
        if not parsed or parsed not in self.board.legal_moves:
            raise ValueError("move is not legal in the current position")
        self.board.push(parsed)

    def undo_move(self) -> str:
        if not self.board.move_stack:
            raise ValueError("there is no move to undo")
        return self.board.pop().uci()

    def is_in_check(self, color: str | None = None) -> bool:
        if color is None:
            return self.board.is_check()
        if color not in ("white", "black"):
            raise ValueError("color must be 'white' or 'black'")
        side = chess.WHITE if color == "white" else chess.BLACK
        king = self.board.king(side)
        if king is None:
            raise ValueError("the requested color has no king")
        return self.board.is_attacked_by(not side, king)

    def is_checkmate(self) -> bool:
        return self.board.is_checkmate()

    def is_stalemate(self) -> bool:
        return self.board.is_stalemate()

    def evaluate_position(self) -> int:
        return evaluate_position(self.board)

    def search(
        self,
        depth: int = 2,
        *,
        max_nodes: int = 50_000,
        time_limit: float = 1.0,
        stop_event: Event | None = None,
    ) -> SearchResult:
        """Search a full-history copy with iterative deepening and alpha-beta.

        The limits are depth 1..4, nodes 1..100000, and a positive finite time
        allowance up to ten seconds. Cancellation and time are checked at each
        visited node and before exploring moves. Only completed iterations are
        published; interruption before one completes returns a legal fallback.
        Faster mates and slower forced losses receive preferable scores.
        """
        target_depth = _integer_limit(depth, "depth", MAX_DEPTH)
        node_limit = _integer_limit(max_nodes, "max_nodes", MAX_NODES)
        if isinstance(time_limit, bool) or not isinstance(time_limit, Real):
            raise TypeError("time_limit must be a real number")
        if not 0 < time_limit <= MAX_TIME_SECONDS:
            raise ValueError("time_limit must be positive and at most 10 seconds")
        seconds = float(time_limit)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("time_limit must be finite and positive")
        if stop_event is not None and not isinstance(stop_event, Event):
            raise TypeError("stop_event must be a threading.Event or None")

        budget = _SearchBudget(node_limit, monotonic() + seconds, stop_event)
        board = _coerce_board(self.board)
        terminal = _terminal_score(board)
        if terminal is not None:
            return SearchResult(None, terminal, 0, 0)
        moves = _ordered_moves(board)
        # A valid nonterminal position always has a legal move.
        result = SearchResult(moves[0].uci(), _material_score(board), 0, 0)
        maximizing = board.turn == chess.WHITE

        for current_depth in range(1, target_depth + 1):
            try:
                budget.visit()
                alpha, beta = -_INFINITY, _INFINITY
                best_score = -_INFINITY if maximizing else _INFINITY
                best_move = moves[0]
                for move in moves:
                    budget.check()
                    board.push(move)
                    try:
                        score = _alpha_beta(
                            board, current_depth - 1, alpha, beta, 1, budget
                        )
                    finally:
                        board.pop()
                    if (maximizing and score > best_score) or (
                        not maximizing and score < best_score
                    ):
                        best_score, best_move = score, move
                    if maximizing:
                        alpha = max(alpha, best_score)
                    else:
                        beta = min(beta, best_score)
                budget.check()
            except _SearchStopped:
                break
            result = SearchResult(best_move.uci(), best_score, current_depth, budget.nodes)
            if abs(best_score) >= MATE_SCORE - current_depth:
                # This completed iteration proves a mate within its horizon.
                break
        return SearchResult(result.move, result.score, result.depth, budget.nodes)

    def choose_move(self, depth: int = 2) -> str | None:
        return self.search(depth).move

    def handle_uci_command(self, command: str) -> bool:
        """Delegate a command to a persistent protocol session and its output."""
        if self._uci_protocol is None:
            from .uci import UCIProtocol

            self._uci_protocol = UCIProtocol(self)
        return self._uci_protocol.handle_command(command)

    def start(
        self, *, input_stream: TextIO | None = None, output_stream: TextIO | None = None
    ) -> int:
        """Start the UCI transport without importing it during core imports."""
        from .uci import run_uci

        return run_uci(engine=self, input_stream=input_stream, output_stream=output_stream)


QChessEngine = ChessEngine
