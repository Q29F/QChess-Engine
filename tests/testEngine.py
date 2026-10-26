"""Legal-position and bounded-search regression tests."""
import threading
import time
import unittest
import chess
from src.api import ChessEngine

CHECK_FEN = "4k3/8/8/8/8/8/4r3/4K3 w - - 0 1"
MATE_FEN = "7k/6Q1/5K2/8/8/8/8/8 b - - 0 1"
STALEMATE_FEN = "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1"
MATE_IN_ONE_FEN = "7k/8/5KQ1/8/8/8/8/8 w - - 0 1"
CASTLING_FEN = "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"
PROMOTION_FEN = "7k/P7/8/8/8/8/8/4K3 w - - 0 1"


class BoardTests(unittest.TestCase):
    def test_fixtures_are_valid_positions(self):
        for fen in (CHECK_FEN, MATE_FEN, STALEMATE_FEN, MATE_IN_ONE_FEN, CASTLING_FEN, PROMOTION_FEN):
            with self.subTest(fen=fen):
                self.assertTrue(chess.Board(fen).is_valid())

    def test_initial_position_has_twenty_legal_moves(self):
        engine = ChessEngine()
        self.assertEqual(engine.get_board_state(), chess.STARTING_FEN)
        self.assertEqual(engine.current_turn, "white")
        self.assertEqual(engine.move_history, [])
        expected = {move.uci() for move in chess.Board().legal_moves}
        self.assertEqual(len(engine.get_legal_moves()), 20)
        self.assertEqual(set(engine.get_legal_moves()), expected)
        self.assertEqual(set(engine.generate_moves()), expected)

    def test_e2e4_updates_pieces_and_changes_turn(self):
        engine = ChessEngine()
        engine.make_move("e2e4")
        self.assertIsNone(engine.board.piece_at(chess.E2))
        self.assertEqual(engine.board.piece_at(chess.E4), chess.Piece(chess.PAWN, chess.WHITE))
        self.assertEqual(engine.current_turn, "black")
        self.assertEqual(engine.move_history, ["e2e4"])
        self.assertIn("e7e5", engine.get_legal_moves())
        self.assertNotIn("e4e5", engine.get_legal_moves())

    def test_illegal_and_malformed_moves_leave_position_unchanged(self):
        engine = ChessEngine()
        for move in ("e2e5", "e7e5", "a1a3", "e1g1", "0000", "", "e2e9", "e4", None, True):
            with self.subTest(move=move):
                before = engine.get_board_state(), engine.move_history
                with self.assertRaises((TypeError, ValueError)):
                    engine.make_move(move)
                self.assertEqual((engine.get_board_state(), engine.move_history), before)

    def test_undo_restores_full_position_and_history(self):
        engine = ChessEngine()
        engine.make_move("e2e4")
        before_reply = engine.get_board_state()
        engine.make_move("c7c5")
        self.assertEqual(engine.undo_move(), "c7c5")
        self.assertEqual(engine.get_board_state(), before_reply)
        self.assertEqual(engine.move_history, ["e2e4"])
        self.assertEqual(engine.undo_move(), "e2e4")
        self.assertEqual(engine.get_board_state(), chess.STARTING_FEN)
        with self.assertRaises(ValueError):
            engine.undo_move()

    def test_start_position_has_four_hundred_depth_two_leaves(self):
        engine = ChessEngine()
        leaves = 0
        for move in engine.get_legal_moves():
            engine.make_move(move)
            leaves += len(engine.get_legal_moves())
            engine.undo_move()
        self.assertEqual(leaves, 400)
        self.assertEqual(engine.get_board_state(), chess.STARTING_FEN)
        self.assertEqual(engine.move_history, [])

    def test_castling_moves_both_king_and_rook_and_can_be_undone(self):
        engine = ChessEngine(CASTLING_FEN)
        self.assertIn("e1g1", engine.get_legal_moves())
        self.assertIn("e1c1", engine.get_legal_moves())
        engine.make_move("e1g1")
        self.assertEqual(engine.board.piece_at(chess.G1), chess.Piece(chess.KING, chess.WHITE))
        self.assertEqual(engine.board.piece_at(chess.F1), chess.Piece(chess.ROOK, chess.WHITE))
        self.assertIsNone(engine.board.piece_at(chess.H1))
        self.assertFalse(engine.board.has_kingside_castling_rights(chess.WHITE))
        engine.undo_move()
        self.assertEqual(engine.get_board_state(), CASTLING_FEN)

    def test_en_passant_removes_captured_pawn_and_undo_restores_it(self):
        engine = ChessEngine()
        for move in ("e2e4", "a7a6", "e4e5", "d7d5"):
            engine.make_move(move)
        before = engine.get_board_state()
        self.assertIn("e5d6", engine.get_legal_moves())
        engine.make_move("e5d6")
        self.assertIsNone(engine.board.piece_at(chess.D5))
        self.assertIsNone(engine.board.piece_at(chess.E5))
        self.assertEqual(engine.board.piece_at(chess.D6), chess.Piece(chess.PAWN, chess.WHITE))
        engine.undo_move()
        self.assertEqual(engine.get_board_state(), before)

    def test_all_four_promotion_choices_work(self):
        for suffix, piece_type in (("q", chess.QUEEN), ("r", chess.ROOK), ("b", chess.BISHOP), ("n", chess.KNIGHT)):
            with self.subTest(piece=suffix):
                engine = ChessEngine(PROMOTION_FEN)
                engine.make_move("a7a8" + suffix)
                self.assertEqual(engine.board.piece_at(chess.A8), chess.Piece(piece_type, chess.WHITE))
                engine.undo_move()
                self.assertEqual(engine.get_board_state(), PROMOTION_FEN)
        with self.assertRaises(ValueError):
            ChessEngine(PROMOTION_FEN).make_move("a7a8")

    def test_check_checkmate_and_stalemate_are_distinct(self):
        check = ChessEngine(CHECK_FEN)
        self.assertTrue(check.is_in_check())
        self.assertTrue(check.is_in_check("white"))
        self.assertFalse(check.is_in_check("black"))
        self.assertFalse(check.is_checkmate())
        self.assertFalse(check.is_stalemate())
        mate = ChessEngine(MATE_FEN)
        self.assertTrue(mate.is_in_check("black"))
        self.assertTrue(mate.is_checkmate())
        self.assertFalse(mate.is_stalemate())
        stalemate = ChessEngine(STALEMATE_FEN)
        self.assertFalse(stalemate.is_in_check())
        self.assertFalse(stalemate.is_checkmate())
        self.assertTrue(stalemate.is_stalemate())

    def test_invalid_fen_is_rejected_atomically(self):
        engine = ChessEngine()
        engine.make_move("e2e4")
        before = engine.get_board_state(), engine.move_history
        invalid = (
            "not a FEN",
            "8/8/8/8/8/8/8/7K w - - 0 1",  # Missing black king.
            "8/8/8/8/8/8/7K/7k w - - 0 1",  # Adjacent kings.
            "8/8/8/8/8/8/8/8 w - - 0 1",
        )
        for fen in invalid:
            with self.subTest(fen=fen):
                with self.assertRaises(ValueError):
                    engine.set_board_state(fen)
                self.assertEqual((engine.get_board_state(), engine.move_history), before)
                with self.assertRaises(ValueError):
                    ChessEngine(fen)

    def test_setting_valid_position_resets_move_history(self):
        engine = ChessEngine()
        engine.make_move("e2e4")
        engine.set_board_state(CASTLING_FEN)
        self.assertEqual(engine.get_board_state(), CASTLING_FEN)
        self.assertEqual(engine.move_history, [])


class SearchTests(unittest.TestCase):
    def test_search_returns_legal_move_without_changing_board_or_stack(self):
        engine = ChessEngine()
        for move in ("e2e4", "e7e5"):
            engine.make_move(move)
        before = engine.get_board_state(), engine.move_history
        result = engine.search(depth=2, max_nodes=2_000, time_limit=1.0)
        self.assertIn(result.move, engine.get_legal_moves())
        self.assertEqual((engine.get_board_state(), engine.move_history), before)

    def test_search_finds_mate_in_one_for_either_color(self):
        white_position = chess.Board(MATE_IN_ONE_FEN)
        for board in (white_position, white_position.mirror()):
            with self.subTest(turn=board.turn):
                engine = ChessEngine(board.fen())
                before = engine.get_board_state()
                result = engine.search(depth=2, max_nodes=5_000, time_limit=1.0)
                self.assertIsNotNone(result.move)
                board = board.copy()
                board.push_uci(result.move)
                self.assertTrue(board.is_checkmate())
                if board.turn == chess.BLACK:
                    self.assertGreater(result.score, 99_000)
                else:
                    self.assertLess(result.score, -99_000)
                self.assertEqual(engine.get_board_state(), before)

    def test_terminal_positions_have_no_best_move(self):
        for fen in (MATE_FEN, STALEMATE_FEN):
            with self.subTest(fen=fen):
                engine = ChessEngine(fen)
                result = engine.search()
                self.assertIsNone(result.move)
                self.assertEqual(engine.get_board_state(), fen)

    def test_node_budget_is_respected_and_fallback_is_legal(self):
        engine = ChessEngine()
        for budget in (1, 25):
            with self.subTest(budget=budget):
                result = engine.search(depth=4, max_nodes=budget, time_limit=1.0)
                self.assertLessEqual(result.nodes, budget)
                self.assertIn(result.move, engine.get_legal_moves())
                self.assertEqual(engine.get_board_state(), chess.STARTING_FEN)

    def test_precancelled_search_returns_prompt_legal_fallback(self):
        engine = ChessEngine()
        stop = threading.Event()
        stop.set()
        started = time.monotonic()
        result = engine.search(depth=4, stop_event=stop)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIn(result.move, engine.get_legal_moves())
        self.assertEqual(result.depth, 0)
        self.assertEqual(engine.get_board_state(), chess.STARTING_FEN)

    def test_short_time_budget_returns_within_a_bounded_interval(self):
        engine = ChessEngine()
        started = time.monotonic()
        result = engine.search(depth=4, max_nodes=100_000, time_limit=0.005)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIn(result.move, engine.get_legal_moves())
        self.assertEqual(engine.get_board_state(), chess.STARTING_FEN)

    def test_invalid_search_limits_are_rejected(self):
        engine = ChessEngine()
        invalid_limits = (
            {"depth": 0}, {"depth": 5}, {"depth": True}, {"depth": 1.5},
            {"max_nodes": 0}, {"max_nodes": 100_001}, {"max_nodes": True},
            {"time_limit": 0}, {"time_limit": float("nan")},
            {"time_limit": float("inf")}, {"time_limit": 11}, {"time_limit": True},
        )
        for limits in invalid_limits:
            with self.subTest(limits=limits):
                with self.assertRaises((TypeError, ValueError)):
                    engine.search(**limits)
                self.assertEqual(engine.get_board_state(), chess.STARTING_FEN)


if __name__ == "__main__":
    unittest.main()
