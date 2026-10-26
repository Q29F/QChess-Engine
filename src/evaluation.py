"""Material and terminal-position evaluation from White's perspective."""

from __future__ import annotations

import chess


MATE_SCORE = 100_000
PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    # Kings cannot be captured legally. Their decisive value is represented by
    # the terminal mate score instead of a material bonus on both sides.
    chess.KING: 0,
}


def _coerce_board(position: str | chess.Board) -> chess.Board:
    """Return a validated independent board, preserving an input board's stack."""
    if isinstance(position, chess.Board):
        board = position.copy(stack=True)
    elif isinstance(position, str):
        if len(position.split()) != 6:
            raise ValueError("FEN must contain all six fields")
        board = chess.Board(position)
    else:
        raise TypeError("position must be a FEN string or chess.Board")
    if not board.is_valid():
        raise ValueError("position fails the chess board validity checks")
    return board


def _terminal_score(board: chess.Board, ply: int = 0) -> int | None:
    """Score automatic game endings; optional draw claims remain with the GUI."""
    outcome = board.outcome(claim_draw=False)
    if outcome is None:
        return None
    if outcome.winner is None:
        return 0
    value = MATE_SCORE - ply
    return value if outcome.winner == chess.WHITE else -value


def _material_score(board: chess.Board) -> int:
    return sum(
        value * (
            len(board.pieces(piece_type, chess.WHITE))
            - len(board.pieces(piece_type, chess.BLACK))
        )
        for piece_type, value in PIECE_VALUES.items()
    )


def evaluate_position(position: str | chess.Board) -> int:
    """Evaluate a valid FEN or board without modifying the supplied position.

    Positive centipawns favor White; negative centipawns favor Black. Checkmate
    is +/-MATE_SCORE, and automatic draws are zero. Nonterminal positions use
    material only. Threefold and fifty-move claims are not automatic game ends.
    """
    board = _coerce_board(position)
    terminal = _terminal_score(board)
    return _material_score(board) if terminal is None else terminal


def best_move(position: str | chess.Board, depth: int = 2) -> str | None:
    """Return a legal UCI move from bounded search, or None for an ended game."""
    from .engine import ChessEngine

    board = _coerce_board(position)
    engine = ChessEngine(board.fen())
    # Retain history when the caller supplied a board, including repetition
    # information that cannot be represented by a FEN string alone.
    engine.board = board
    return engine.choose_move(depth)
