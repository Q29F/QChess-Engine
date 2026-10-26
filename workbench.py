"""Stateless, bounded browser controller for the existing classical engine.

state accepts exactly initialFen and moves. search also requires depth and
returns the supplied position plus search metadata and a separate nextState.
claim additionally requires reason and intendedMove; an announced move is
validated but is not appended to the played history.

Every call reconstructs an independent full-history board. Search scores use
the engine's White perspective: positive favors White; mate scores use the
engine's MATE_SCORE sentinel with its distance adjustment. A position's
evaluation.type distinguishes material, mate and draw for presentation.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

import chess
import chess.pgn

from localServer import RequestError
from src.engine import ChessEngine
from src.evaluation import evaluate_position


MAX_HISTORY = 512
MAX_FEN_LENGTH = 160
MAX_COUNTER = 2_147_483_647
SEARCH_NODES = 50_000
SEARCH_SECONDS = 2.0
MAX_SEARCH_DEPTH = 4
_UCI_MOVE = re.compile(r"[a-h][1-8][a-h][1-8][qrbn]?")
_COUNTER = re.compile(r"[0-9]{1,10}")
_REASONS = ("threefold", "fiftyMoves")
_TERMINATIONS = {
    chess.Termination.CHECKMATE: "checkmate",
    chess.Termination.STALEMATE: "stalemate",
    chess.Termination.INSUFFICIENT_MATERIAL: "insufficient-material",
    chess.Termination.SEVENTYFIVE_MOVES: "seventyfive-moves",
    chess.Termination.FIVEFOLD_REPETITION: "fivefold-repetition",
}


@dataclass(slots=True)
class _Position:
    engine: ChessEngine
    initial_fen: str
    moves: list[str]
    san: list[str]


def _schema(payload: object, fields: set[str], message: str) -> dict:
    if type(payload) is not dict or set(payload) != fields:
        raise RequestError(400, message)
    return payload


def _fen(value: object) -> str:
    if (
        type(value) is not str or not 1 <= len(value) <= MAX_FEN_LENGTH
        or any(not 32 <= ord(character) <= 126 for character in value)
    ):
        raise RequestError(400, "initialFen must be printable ASCII with at most 160 characters.")
    fields = value.split()
    if len(fields) != 6:
        raise RequestError(400, "initialFen must contain all six FEN fields.")
    for text, minimum in ((fields[4], 0), (fields[5], 1)):
        if _COUNTER.fullmatch(text) is None or not minimum <= int(text) <= MAX_COUNTER:
            raise RequestError(400, "FEN counters must be at most 2147483647; the fullmove number starts at 1.")
    return " ".join(fields)


def _check_counters(board: chess.Board) -> None:
    if not 0 <= board.halfmove_clock <= MAX_COUNTER or not 1 <= board.fullmove_number <= MAX_COUNTER:
        raise RequestError(400, "The move would exceed the supported FEN counter range.")


def _move(board: chess.Board, value: object) -> chess.Move:
    if type(value) is not str or _UCI_MOVE.fullmatch(value) is None:
        raise RequestError(400, "Moves must use standard UCI notation, including the promotion piece when needed.")
    try:
        move = board.parse_uci(value)
    except ValueError:
        raise RequestError(400, "A move is not legal in the supplied position history.") from None
    if not move:
        raise RequestError(400, "Null moves are not legal game moves.")
    return move


def _restore(payload: dict) -> _Position:
    initial_fen = _fen(payload["initialFen"])
    moves = payload["moves"]
    if type(moves) is not list or len(moves) > MAX_HISTORY:
        raise RequestError(400, "moves must be an array containing at most 512 played moves.")
    try:
        engine = ChessEngine(initial_fen)
    except (ValueError, TypeError, OverflowError):
        raise RequestError(400, "initialFen must describe a valid standard chess position.") from None
    canonical_initial = engine.get_board_state()
    played, notation = [], []
    for text in moves:
        if engine.board.outcome(claim_draw=False) is not None:
            raise RequestError(400, "The history contains a move after an automatic game ending.")
        move = _move(engine.board, text)
        notation.append(engine.board.san(move))
        engine.board.push(move)
        _check_counters(engine.board)
        played.append(move.uci())
    return _Position(engine, canonical_initial, played, notation)


def _eligible(board: chess.Board, reason: str) -> bool:
    # is_fifty_moves preserves checkmate/stalemate precedence. These predicates
    # test the actual board, unlike can_claim_* which also explores next moves.
    return board.is_repetition(3) if reason == "threefold" else board.is_fifty_moves()


def _draw_claims(board: chess.Board, legal_moves: list[chess.Move]) -> list[dict]:
    options = []
    prospective = []
    for reason in _REASONS:
        if _eligible(board, reason):
            options.append({"reason": reason, "intendedMove": None, "san": None})
        else:
            prospective.append(reason)
    if not prospective:
        return options
    candidate = board.copy(stack=True)
    for move in legal_moves:
        candidate.push(move)
        try:
            eligible = [reason for reason in prospective if _eligible(candidate, reason)]
        finally:
            candidate.pop()
        if eligible:
            notation = board.san(move)
            options.extend(
                {"reason": reason, "intendedMove": move.uci(), "san": notation}
                for reason in eligible
            )
    return options


def _outcome(board: chess.Board, claimed: dict | None) -> dict | None:
    if claimed is not None:
        termination = "claimed-threefold-repetition" if claimed["reason"] == "threefold" else "claimed-fifty-moves"
        return {"result": "1/2-1/2", "winner": None, "termination": termination}
    outcome = board.outcome(claim_draw=False)
    if outcome is None:
        return None
    winner = None if outcome.winner is None else "white" if outcome.winner == chess.WHITE else "black"
    return {
        "result": outcome.result(),
        "winner": winner,
        "termination": _TERMINATIONS[outcome.termination],
    }


def _pgn(board: chess.Board, claimed: dict | None) -> str:
    game = chess.pgn.Game.from_board(board)
    game.headers["Event"] = "QChess local game"
    game.headers["Site"] = "Local"
    game.headers["White"] = "White"
    game.headers["Black"] = "Black"
    if claimed is not None:
        game.headers["Result"] = "1/2-1/2"
        reason = "threefold repetition" if claimed["reason"] == "threefold" else "the fifty-move rule"
        comment = f"Draw claimed by {reason}"
        if claimed["san"] is not None:
            comment += f" with the announced move {claimed['san']}, which was not played"
        game.end().comment = comment + "."
    exporter = chess.pgn.StringExporter(headers=True, comments=claimed is not None, variations=False)
    return game.accept(exporter)


def _render(position: _Position, *, claimed: dict | None = None) -> dict[str, Any]:
    board = position.engine.board
    outcome = _outcome(board, claimed)
    legal_moves = sorted(board.legal_moves, key=lambda move: move.uci()) if outcome is None else []
    draw_claims = _draw_claims(board, legal_moves) if outcome is None else []
    history_limit = len(position.moves) >= MAX_HISTORY
    counter_limit = board.fullmove_number >= MAX_COUNTER and board.turn == chess.BLACK
    can_play = outcome is None and not history_limit and not counter_limit
    if outcome is not None:
        evaluation_type = "draw" if outcome["winner"] is None else "mate"
    else:
        evaluation_type = "material"
    score = 0 if claimed is not None else evaluate_position(board)
    return {
        "initialFen": position.initial_fen,
        "fen": board.fen(),
        "moves": list(position.moves),
        "san": list(position.san),
        "turn": "white" if board.turn == chess.WHITE else "black",
        "pieces": [
            {"square": chess.square_name(square), "symbol": piece.symbol(),
             "color": "white" if piece.color == chess.WHITE else "black"}
            for square, piece in sorted(board.piece_map().items())
        ],
        "legalMoves": [move.uci() for move in legal_moves] if can_play else [],
        "check": board.is_check(),
        "status": outcome["termination"] if outcome is not None else "check" if board.is_check() else "playing",
        "outcome": outcome,
        "drawClaims": draw_claims,
        "claimableDraw": {
            reason: any(option["reason"] == reason for option in draw_claims)
            for reason in _REASONS
        },
        "claim": dict(claimed) if claimed is not None else None,
        "evaluation": {"score": score, "perspective": "white", "type": evaluation_type},
        "pgn": _pgn(board, claimed),
        "historyLimitReached": history_limit,
        "counterLimitReached": counter_limit,
        "canPlay": can_play,
    }


def state(payload: dict) -> dict[str, Any]:
    """Validate and describe the entire supplied history without changing it."""
    _schema(payload, {"initialFen", "moves"}, "Provide exactly initialFen and moves for a position.")
    return _render(_restore(payload))


def search(payload: dict) -> dict[str, Any]:
    """Search the supplied position; keep it separate from its suggested next state."""
    _schema(payload, {"initialFen", "moves", "depth"}, "Provide exactly initialFen, moves and depth for a search.")
    depth = payload["depth"]
    if type(depth) is not int or not 1 <= depth <= MAX_SEARCH_DEPTH:
        raise RequestError(400, "depth must be an integer from 1 to 4.")
    position = _restore(payload)
    before = _render(position)
    if before["outcome"] is None and not before["canPlay"]:
        raise RequestError(400, "The game reached an application limit. Undo a move or start a new game.")
    result = position.engine.search(depth, max_nodes=SEARCH_NODES, time_limit=SEARCH_SECONDS)
    next_state = None
    notation = None
    if result.move is not None:
        try:
            move = _move(position.engine.board, result.move)
        except RequestError:
            raise RuntimeError("engine returned an invalid move") from None
        notation = position.engine.board.san(move)
        next_engine = ChessEngine(position.initial_fen)
        next_engine.board = position.engine.board.copy(stack=True)
        next_engine.board.push(move)
        _check_counters(next_engine.board)
        next_position = _Position(
            next_engine, position.initial_fen,
            position.moves + [move.uci()], position.san + [notation],
        )
        next_state = _render(next_position)
    before["search"] = {
        "move": result.move, "san": notation, "score": result.score,
        "depth": result.depth, "nodes": result.nodes,
    }
    before["nextState"] = next_state
    return before


def claim(payload: dict) -> dict[str, Any]:
    """Validate an immediate or explicitly announced draw claim without playing it."""
    _schema(
        payload, {"initialFen", "moves", "reason", "intendedMove"},
        "Provide exactly initialFen, moves, reason and intendedMove for a draw claim.",
    )
    reason = payload["reason"]
    if type(reason) is not str or reason not in _REASONS:
        raise RequestError(400, "Draw reason must be threefold or fiftyMoves.")
    position = _restore(payload)
    board = position.engine.board
    if board.outcome(claim_draw=False) is not None:
        raise RequestError(400, "The game already has an automatic result.")
    intended = payload["intendedMove"]
    notation = None
    candidate = board
    if intended is not None:
        move = _move(board, intended)
        notation = board.san(move)
        candidate = board.copy(stack=True)
        candidate.push(move)
        # This board is hypothetical: the announced move does not advance the
        # returned history or its bounded counters.
        intended = move.uci()
    if not _eligible(candidate, reason):
        raise RequestError(400, "The supplied position or announced move does not permit this draw claim.")
    return _render(position, claimed={"reason": reason, "intendedMove": intended, "san": notation})
