import unittest
from src.evaluation import evaluate_position

class TestEvaluation(unittest.TestCase):

    def test_evaluate_simple_position(self):
        position = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"  # Starting position
        self.assertEqual(evaluate_position(position), 0)  # Expecting a neutral evaluation

    def test_evaluate_material_advantage(self):
        position = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"  # Starting position
        # Simulate a position where white has a material advantage
        # This is a placeholder; the actual evaluation logic will depend on your implementation
        self.assertGreater(evaluate_position(position), 0)  # Expecting a positive evaluation

    def test_evaluate_checkmate(self):
        position = "8/8/8/8/8/8/8/7K w - - 0 1"  # Position where black is in checkmate
        self.assertEqual(evaluate_position(position), float('inf'))  # Expecting checkmate evaluation

    def test_evaluate_stalemate(self):
        position = "8/8/8/8/8/8/7K/7k w - - 0 1"  # Stalemate position
        self.assertEqual(evaluate_position(position), 0)  # Expecting a neutral evaluation

if __name__ == '__main__':
    unittest.main()