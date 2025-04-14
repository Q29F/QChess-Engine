import unittest
from src.engine import ChessEngine

class TestChessEngine(unittest.TestCase):

    def setUp(self):
        self.engine = ChessEngine()

    def test_initial_position(self):
        self.assertEqual(self.engine.get_board_state(), self.engine.initial_board_state)

    def test_move_generation(self):
        self.engine.make_move("e2e4")
        self.assertIn("e4", self.engine.get_legal_moves())

    def test_invalid_move(self):
        with self.assertRaises(ValueError):
            self.engine.make_move("e2e5")  # Invalid move for the initial position

    def test_check_detection(self):
        self.engine.set_board_state("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")  # Initial position
        self.engine.make_move("e2e4")
        self.engine.make_move("e7e5")
        self.engine.make_move("g1f3")
        self.engine.make_move("b8c6")
        self.assertTrue(self.engine.is_in_check("white"))

if __name__ == '__main__':
    unittest.main()