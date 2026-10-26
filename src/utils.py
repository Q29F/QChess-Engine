"""Small, bounded helpers for the line-oriented UCI interface."""

from __future__ import annotations

import sys

import chess

MAX_COMMAND_LENGTH = 16_384
MAX_COMMAND_TOKENS = 1_033  # position + fen + six fields + moves + 1,024 moves


def parse_input(input_string: str) -> str:
    """Accept one bounded ASCII command, optionally ending in CRLF."""
    if not isinstance(input_string, str):
        raise TypeError("command must be text")
    command = input_string.removesuffix("\n").removesuffix("\r")
    if len(command) > MAX_COMMAND_LENGTH:
        raise ValueError("command is too long")
    if not command.isascii() or any(
        ord(character) < 32 and character != "\t" or ord(character) == 127
        for character in command
    ):
        raise ValueError("command must contain printable ASCII or tabs")
    if len(command.split()) > MAX_COMMAND_TOKENS:
        raise ValueError("command has too many tokens")
    return command.strip()


def format_output(output_string: str) -> str:
    """Validate one protocol line; the caller adds the final newline."""
    return parse_input(output_string)


def is_valid_move(move: str, board: chess.Board | None = None) -> bool:
    """Check UCI syntax and, when a board is supplied, position legality."""
    if not isinstance(move, str) or len(move) not in (4, 5):
        return False
    try:
        parsed = chess.Move.from_uci(move)
    except ValueError:
        return False
    return bool(parsed) and (board is None or parsed in board.legal_moves)


def log_message(message: object) -> None:
    """Keep diagnostics off UCI's stdout transport."""
    print(f"[QChess] {message}", file=sys.stderr, flush=True)
