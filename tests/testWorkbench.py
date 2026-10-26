"""Independent chess-rule and resource-bound checks for the web controller."""

import copy
from functools import cache
import io
import random
import time
import unittest

import chess
import chess.pgn

from localServer import RequestError
from workbench import claim, search, state


CASTLING = "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"
PROMOTION = "7k/P7/8/8/8/8/8/4K3 w - - 0 1"
MATE = "7k/6Q1/5K2/8/8/8/8/8 b - - 150 80"
MATE_IN_ONE = "7k/8/5KQ1/8/8/8/8/8 w - - 99 80"
FIFTY = "4k3/8/8/8/8/8/1R6/4K3 w - - 99 80"
CYCLE = ["g1f3", "g8f6", "f3g1", "f6g8"]


def position(moves=(), fen=chess.STARTING_FEN):
    return {"initialFen": fen, "moves": list(moves)}


def restored(payload):
    board = chess.Board(payload["initialFen"])
    for move in payload["moves"]:
        board.push_uci(move)
    return board


@cache
def long_game():
    """Build 512 legal plies while independently excluding automatic endings."""
    fen = "r6k/pppppppp/8/8/8/8/PPPPPPPP/R6K w - - 0 1"
    board = chess.Board(fen)
    randomizer = random.Random(29)
    pawn_moves = dict(zip((100, 200, 300, 400, 500), ("a2a3", "b2b3", "c2c3", "d2d3", "e2e3")))
    played = []
    for ply in range(512):
        if ply in pawn_moves:
            move = chess.Move.from_uci(pawn_moves[ply])
            assert move in board.legal_moves
            board.push(move)
            assert board.outcome(claim_draw=False) is None
        else:
            origin = next(iter(board.pieces(chess.ROOK, board.turn)))
            destinations = list(range(7))
            randomizer.shuffle(destinations)
            for file_index in destinations:
                move = chess.Move(origin, chess.square(file_index, 0 if board.turn else 7))
                if move not in board.legal_moves:
                    continue
                board.push(move)
                if board.outcome(claim_draw=False) is None:
                    break
                board.pop()
            else:
                raise AssertionError("The bounded history fixture must have a nonterminal rook move")
        played.append(move.uci())
    return fen, tuple(played)


class WorkbenchTests(unittest.TestCase):
    def assert_pgn(self, result):
        game = chess.pgn.read_game(io.StringIO(result["pgn"]))
        self.assertIsNotNone(game)
        self.assertEqual(game.errors, [])
        self.assertEqual(game.board().fen(), result["initialFen"])
        self.assertEqual([move.uci() for move in game.mainline_moves()], result["moves"])
        self.assertEqual(game.end().board().fen(), result["fen"])
        expected = result["outcome"]["result"] if result["outcome"] is not None else "*"
        self.assertEqual(game.headers["Result"], expected)
        return game

    def assert_client_error(self, function, payload):
        with self.assertRaises(RequestError) as caught:
            function(payload)
        self.assertEqual(caught.exception.status, 400)
        self.assertLessEqual(len(caught.exception.public_message), 160)

    def test_starting_snapshot_matches_standard_chess(self):
        result = state(position())
        self.assertEqual(result["initialFen"], chess.STARTING_FEN)
        self.assertEqual(result["fen"], chess.STARTING_FEN)
        self.assertEqual(result["moves"], [])
        self.assertEqual(result["san"], [])
        self.assertEqual(result["turn"], "white")
        self.assertEqual(len(result["pieces"]), 32)
        self.assertEqual(set(result["legalMoves"]), {move.uci() for move in chess.Board().legal_moves})
        self.assertEqual(result["evaluation"], {"score": 0, "perspective": "white", "type": "material"})
        self.assertEqual(result["status"], "playing")
        self.assertIsNone(result["outcome"])
        self.assertTrue(result["canPlay"])
        self.assert_pgn(result)

    def test_full_move_history_san_and_pgn_follow_the_supplied_position(self):
        payload = position(["e2e4", "c7c5", "g1f3", "d7d6"])
        board = restored(payload)
        result = state(payload)
        self.assertEqual(result["fen"], board.fen())
        self.assertEqual(result["moves"], payload["moves"])
        self.assertEqual(result["san"], ["e4", "c5", "Nf3", "d6"])
        self.assertEqual(result["turn"], "white")
        self.assert_pgn(result)

    def test_castling_en_passant_and_all_promotion_pieces_survive_replay(self):
        cases = [
            (position(["e1g1"], CASTLING), {"g1": "K", "f1": "R", "h1": None}),
            (position(["e2e4", "a7a6", "e4e5", "d7d5", "e5d6"]), {"d6": "P", "d5": None, "e5": None}),
        ]
        cases.extend((position(["a7a8" + suffix], PROMOTION), {"a8": suffix.upper(), "a7": None}) for suffix in "qrbn")
        for payload, expected in cases:
            with self.subTest(moves=payload["moves"]):
                result = state(payload)
                self.assertEqual(result["fen"], restored(payload).fen())
                pieces = {piece["square"]: piece["symbol"] for piece in result["pieces"]}
                for square, symbol in expected.items():
                    self.assertEqual(pieces.get(square), symbol)
                self.assert_pgn(result)

    def test_check_and_automatic_results_preserve_chess_precedence(self):
        checked = state(position(fen="4k3/8/8/8/8/8/4r3/4K3 w - - 0 1"))
        self.assertTrue(checked["check"])
        self.assertEqual(checked["status"], "check")
        self.assertTrue(checked["canPlay"])
        for fen, termination, winner in (
            (MATE, "checkmate", "white"),
            ("7k/5Q2/6K1/8/8/8/8/8 b - - 150 80", "stalemate", None),
            ("4k3/8/8/8/8/8/8/4K3 w - - 0 1", "insufficient-material", None),
            ("4k3/8/8/8/8/8/1R6/4K3 w - - 150 80", "seventyfive-moves", None),
        ):
            with self.subTest(termination=termination):
                result = state(position(fen=fen))
                self.assertEqual(result["outcome"]["termination"], termination)
                self.assertEqual(result["outcome"]["winner"], winner)
                self.assertEqual(result["evaluation"]["type"], "mate" if winner else "draw")
                self.assertEqual(result["legalMoves"], [])
                self.assertEqual(result["drawClaims"], [])
                self.assertFalse(result["canPlay"])
                self.assert_pgn(result)

    def test_fivefold_uses_actual_history_and_rejects_play_after_the_result(self):
        payload = position(CYCLE * 4)
        result = state(payload)
        self.assertEqual(result["outcome"]["termination"], "fivefold-repetition")
        self.assertEqual(result["outcome"]["result"], "1/2-1/2")
        self.assert_client_error(state, position([*payload["moves"], "g1f3"]))
        for reason in ("threefold", "fiftyMoves"):
            self.assert_client_error(claim, {**payload, "reason": reason, "intendedMove": None})

    def test_immediate_draw_claims_are_explicit_and_leave_history_unchanged(self):
        cases = (
            (position(CYCLE * 2), "threefold", "claimed-threefold-repetition"),
            (position(fen=FIFTY.replace("99 80", "100 80")), "fiftyMoves", "claimed-fifty-moves"),
        )
        for payload, reason, termination in cases:
            with self.subTest(reason=reason):
                before = state(payload)
                self.assertIsNone(before["outcome"])
                self.assertIn({"reason": reason, "intendedMove": None, "san": None}, before["drawClaims"])
                result = claim({**payload, "reason": reason, "intendedMove": None})
                self.assertEqual(result["fen"], before["fen"])
                self.assertEqual(result["moves"], before["moves"])
                self.assertEqual(result["san"], before["san"])
                self.assertEqual(result["outcome"], {"result": "1/2-1/2", "winner": None, "termination": termination})
                self.assertEqual(result["evaluation"], {"score": 0, "perspective": "white", "type": "draw"})
                self.assertEqual(result["legalMoves"], [])
                self.assert_pgn(result)

    def test_announced_threefold_move_is_validated_but_never_played(self):
        payload = position([*(CYCLE * 2)[:-1]])
        board = restored(payload)
        self.assertFalse(board.is_repetition(3))
        self.assertTrue(board.can_claim_threefold_repetition())
        before = state(payload)
        option = {"reason": "threefold", "intendedMove": "f6g8", "san": "Ng8"}
        self.assertIn(option, before["drawClaims"])
        self.assert_client_error(claim, {**payload, "reason": "threefold", "intendedMove": None})
        self.assert_client_error(claim, {**payload, "reason": "threefold", "intendedMove": "f6h5"})
        result = claim({**payload, "reason": "threefold", "intendedMove": "f6g8"})
        self.assertEqual(result["fen"], before["fen"])
        self.assertEqual(result["turn"], "black")
        self.assertEqual(result["moves"], payload["moves"])
        self.assertEqual(result["claim"], option)
        game = self.assert_pgn(result)
        self.assertIn("Ng8", game.end().comment)
        self.assertEqual(len(list(game.mainline_moves())), 7)

    def test_announced_fifty_move_claim_does_not_advance_the_game(self):
        payload = position(fen=FIFTY)
        before = state(payload)
        self.assertFalse(restored(payload).is_fifty_moves())
        self.assert_client_error(claim, {**payload, "reason": "fiftyMoves", "intendedMove": None})
        result = claim({**payload, "reason": "fiftyMoves", "intendedMove": "b2b3"})
        self.assertEqual(result["fen"], before["fen"])
        self.assertEqual(result["moves"], [])
        self.assertEqual(result["claim"]["san"], "Rb3")
        self.assertEqual(chess.Board(result["fen"]).halfmove_clock, 99)
        self.assert_pgn(result)
        with_pawn = position(fen="4k3/8/8/8/8/8/1R5P/4K3 w - - 99 80")
        self.assertIn(chess.Move.from_uci("h2h3"), restored(with_pawn).legal_moves)
        self.assert_client_error(claim, {**with_pawn, "reason": "fiftyMoves", "intendedMove": "h2h3"})

    def test_checkmate_on_an_announced_move_cannot_be_claimed_as_fifty_moves(self):
        payload = position(fen=MATE_IN_ONE)
        board = restored(payload)
        board.push_uci("g6g7")
        self.assertTrue(board.is_checkmate())
        self.assert_client_error(claim, {**payload, "reason": "fiftyMoves", "intendedMove": "g6g7"})
        result = state(position(["g6g7"], MATE_IN_ONE))
        self.assertEqual(result["outcome"], {"result": "1-0", "winner": "white", "termination": "checkmate"})
        self.assert_client_error(claim, {**position(fen=MATE), "reason": "fiftyMoves", "intendedMove": None})

    def test_claim_schema_reason_and_concrete_move_are_required(self):
        valid = {**position(fen=FIFTY), "reason": "fiftyMoves", "intendedMove": "b2b3"}
        for payload in (
            None, [], {key: value for key, value in valid.items() if key != "intendedMove"},
            {**valid, "extra": True}, {**valid, "reason": "draw"}, {**valid, "reason": True},
            {**valid, "intendedMove": True}, {**valid, "intendedMove": "b2b9"},
        ):
            with self.subTest(payload=repr(payload)):
                self.assert_client_error(claim, payload)

    def test_position_schema_and_move_primitives_are_strict(self):
        for payload in (None, True, [], {}, {"initialFen": chess.STARTING_FEN}, {**position(), "fen": chess.STARTING_FEN}):
            self.assert_client_error(state, payload)
        for moves in (None, True, "e2e4", ("e2e4",), [None], [True], ["0000"], ["e2e5"], ["e2e4", "e7e9"], ["E2E4"]):
            with self.subTest(moves=moves):
                self.assert_client_error(state, {"initialFen": chess.STARTING_FEN, "moves": moves})

    def test_fen_limits_and_counter_bounds_are_validated(self):
        placement = " ".join(chess.STARTING_FEN.split()[:4])
        for fen in (None, True, "", " " * 161, chess.STARTING_FEN + "\n", "é", "8/8/8/8/8/8/8/7K w - - 0 1"):
            self.assert_client_error(state, position(fen=fen))
        for halfmove, fullmove in (("-1", "1"), ("+1", "1"), ("1.0", "1"), ("0", "0"), ("0", "+1"), ("2147483648", "1"), ("0", "2147483648"), ("00000000000", "1"), ("0", "00000000001")):
            with self.subTest(counters=(halfmove, fullmove)):
                self.assert_client_error(state, position(fen=f"{placement} {halfmove} {fullmove}"))
        padded = state(position(fen=f" {placement} 0000000000 0000000001 "))
        self.assertEqual(padded["initialFen"], chess.STARTING_FEN)
        upper = state(position(fen=f"{placement} 2147483647 2147483647"))
        self.assertEqual(chess.Board(upper["fen"]).fullmove_number, 2147483647)

    def test_512_played_moves_reach_an_application_limit_without_inventing_a_draw(self):
        fen, moves = long_game()
        payload = position(moves, fen)
        self.assertIsNone(restored(payload).outcome(claim_draw=False))
        result = state(payload)
        self.assertEqual(len(result["moves"]), 512)
        self.assertTrue(result["historyLimitReached"])
        self.assertFalse(result["canPlay"])
        self.assertEqual(result["legalMoves"], [])
        self.assertIsNone(result["outcome"])
        self.assert_pgn(result)
        self.assert_client_error(search, {**payload, "depth": 1})
        self.assert_client_error(state, position([*moves, "a1b1"], fen))

    def test_counter_limit_prevents_play_but_allows_a_before_play_draw_claim(self):
        fen = "8/8/8/8/8/6k1/8/R5K1 b - - 99 2147483647"
        payload = position(fen=fen)
        before = state(payload)
        self.assertTrue(before["counterLimitReached"])
        self.assertFalse(before["canPlay"])
        self.assertEqual(before["legalMoves"], [])
        self.assertIsNone(before["outcome"])
        option = next(option for option in before["drawClaims"] if option["reason"] == "fiftyMoves")
        self.assertIsNotNone(option["intendedMove"])
        claimed = claim({**payload, "reason": option["reason"], "intendedMove": option["intendedMove"]})
        self.assertEqual(claimed["fen"], before["fen"])
        self.assertEqual(claimed["outcome"]["result"], "1/2-1/2")
        self.assert_client_error(search, {**payload, "depth": 1})
        self.assert_client_error(state, position([option["intendedMove"]], fen))
        white_fen = chess.STARTING_FEN.rsplit(" ", 1)[0] + " 2147483647"
        white = state(position(fen=white_fen))
        self.assertTrue(white["canPlay"])
        black = state(position(["e2e4"], white_fen))
        self.assertTrue(black["counterLimitReached"])
        self.assertIsNone(black["outcome"])

    def test_calls_are_stateless_and_do_not_mutate_payloads_or_return_shared_data(self):
        payload = position(["e2e4"])
        original = copy.deepcopy(payload)
        first = state(payload)
        state(position(["d2d4"]))
        self.assertEqual(state(payload), first)
        self.assertEqual(payload, original)
        first["moves"].append("a7a5")
        first["pieces"].clear()
        self.assertEqual(payload, original)
        self.assertEqual(state(payload)["moves"], ["e2e4"])
        self.assertEqual(state(position())["fen"], chess.STARTING_FEN)

    def test_search_returns_legal_move_and_separate_next_state_with_full_history(self):
        payload = position(["e2e4", "e7e5"])
        original = copy.deepcopy(payload)
        before = state(payload)
        result = search({**payload, "depth": 1})
        self.assertEqual(result["fen"], before["fen"])
        self.assertEqual(result["moves"], payload["moves"])
        self.assertIn(result["search"]["move"], before["legalMoves"])
        board = restored(payload)
        move = chess.Move.from_uci(result["search"]["move"])
        self.assertEqual(result["search"]["san"], board.san(move))
        board.push(move)
        following = result["nextState"]
        self.assertEqual(following["fen"], board.fen())
        self.assertEqual(following["moves"], [*payload["moves"], move.uci()])
        self.assertEqual(payload, original)
        self.assert_pgn(following)

    def test_search_reports_mate_for_both_colors(self):
        board = chess.Board(MATE_IN_ONE.replace("99 80", "0 1"))
        for initial in (board, board.mirror()):
            with self.subTest(turn=initial.turn):
                result = search({**position(fen=initial.fen()), "depth": 2})
                following = result["nextState"]
                self.assertTrue(chess.Board(following["fen"]).is_checkmate())
                winner = "white" if initial.turn else "black"
                self.assertEqual(following["outcome"]["winner"], winner)
                self.assertGreater(result["search"]["score"] * (1 if initial.turn else -1), 99000)

    def test_search_depth_nodes_and_elapsed_time_remain_bounded(self):
        started = time.monotonic()
        result = search({**position(), "depth": 4})
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertLessEqual(result["search"]["nodes"], 50000)
        self.assertGreaterEqual(result["search"]["depth"], 0)
        self.assertLessEqual(result["search"]["depth"], 4)
        self.assertIn(result["search"]["move"], result["legalMoves"])

    def test_search_schema_and_depth_cannot_expand_the_budget(self):
        for depth in (None, True, False, 0, 5, -1, 1.5, "2", float("nan"), float("inf")):
            self.assert_client_error(search, {**position(), "depth": depth})
        for key, value in (("maxNodes", 1000000), ("timeLimit", 100), ("nodes", 1), ("movetime", 1)):
            self.assert_client_error(search, {**position(), "depth": 1, key: value})

    def test_search_on_automatic_results_has_no_move_or_next_state(self):
        for payload in (position(fen=MATE), position(CYCLE * 4), position(fen="4k3/8/8/8/8/8/8/4K3 w - - 0 1")):
            with self.subTest(fen=payload["initialFen"]):
                result = search({**payload, "depth": 1})
                self.assertIsNone(result["search"]["move"])
                self.assertIsNone(result["nextState"])
                self.assertIsNotNone(result["outcome"])


if __name__ == "__main__":
    unittest.main()
