"""White-relative material and terminal-position evaluation tests."""
import unittest
import chess
from src.api import best_move, evaluate_position


class EvaluationTests(unittest.TestCase):
    def test_starting_position_is_materially_equal(self):
        self.assertEqual(evaluate_position(chess.STARTING_FEN), 0)
        self.assertEqual(evaluate_position(chess.Board()), 0)

    def test_every_material_value_and_color_symmetry(self):
        # Balanced pawns avoid insufficient-material draws in minor-piece fixtures.
        for piece_type, expected in (
            (chess.PAWN, 100), (chess.KNIGHT, 320), (chess.BISHOP, 330),
            (chess.ROOK, 500), (chess.QUEEN, 900),
        ):
            with self.subTest(piece=piece_type):
                board = chess.Board("4k3/7p/8/8/8/8/7P/4K3 w - - 0 1")
                board.set_piece_at(chess.B2, chess.Piece(piece_type, chess.WHITE))
                self.assertTrue(board.is_valid())
                self.assertEqual(evaluate_position(board), expected)
                self.assertEqual(evaluate_position(board.fen()), expected)
                mirrored = board.mirror()
                self.assertTrue(mirrored.is_valid())
                self.assertEqual(evaluate_position(mirrored), -expected)

    def test_evaluation_is_white_relative_regardless_of_side_to_move(self):
        board = chess.Board("4k3/7p/8/8/8/8/1R5P/4K3 w - - 0 1")
        self.assertEqual(evaluate_position(board), 500)
        board.turn = chess.BLACK
        self.assertTrue(board.is_valid())
        self.assertEqual(evaluate_position(board), 500)

    def test_checkmate_scores_have_the_winning_colors_sign(self):
        black_mated = chess.Board("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1")
        self.assertTrue(black_mated.is_valid())
        self.assertTrue(black_mated.is_checkmate())
        self.assertEqual(evaluate_position(black_mated), 100_000)
        white_mated = black_mated.mirror()
        self.assertTrue(white_mated.is_valid())
        self.assertTrue(white_mated.is_checkmate())
        self.assertEqual(evaluate_position(white_mated), -100_000)

    def test_stalemate_and_insufficient_material_score_zero(self):
        for fen in (
            "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1",
            "4k3/8/8/8/8/8/1B6/4K3 w - - 0 1",
        ):
            with self.subTest(fen=fen):
                board = chess.Board(fen)
                self.assertTrue(board.is_valid())
                self.assertTrue(board.is_stalemate() or board.is_insufficient_material())
                self.assertEqual(evaluate_position(board), 0)

    def test_claimable_draw_is_distinct_from_automatic_seventy_five_move_draw(self):
        claimable = chess.Board("4k3/8/8/8/8/8/1R6/4K3 w - - 100 80")
        automatic = chess.Board("4k3/8/8/8/8/8/1R6/4K3 w - - 150 80")
        self.assertTrue(claimable.can_claim_fifty_moves())
        self.assertEqual(evaluate_position(claimable), 500)
        self.assertTrue(automatic.is_seventyfive_moves())
        self.assertEqual(evaluate_position(automatic), 0)

    def test_automatic_fivefold_draw_uses_the_supplied_boards_history(self):
        board = chess.Board("4k1n1/p7/8/8/8/8/PP6/4K1N1 w - - 0 1")
        self.assertTrue(board.is_valid())
        self.assertEqual(evaluate_position(board), 100)
        for _ in range(4):
            for move in ("g1f3", "g8f6", "f3g1", "f6g8"):
                board.push_uci(move)
        self.assertTrue(board.is_fivefold_repetition())
        before = board.fen(), list(board.move_stack)
        self.assertEqual(evaluate_position(board), 0)
        self.assertEqual((board.fen(), list(board.move_stack)), before)

    def test_invalid_positions_are_rejected(self):
        for position in (
            "not a FEN", "8/8/8/8/8/8/8/7K w - - 0 1",
            chess.Board("8/8/8/8/8/8/7K/7k w - - 0 1"),
        ):
            with self.subTest(position=str(position)):
                with self.assertRaises(ValueError):
                    evaluate_position(position)

    def test_best_move_wrapper_returns_a_legal_move_without_mutating_input(self):
        board = chess.Board()
        board.push_uci("e2e4")
        before = board.fen(), list(board.move_stack)
        move = best_move(board, depth=1)
        self.assertIn(chess.Move.from_uci(move), board.legal_moves)
        self.assertEqual((board.fen(), list(board.move_stack)), before)

    def test_best_move_wrapper_returns_none_for_stalemate(self):
        self.assertIsNone(best_move("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1"))


if __name__ == "__main__":
    unittest.main()
