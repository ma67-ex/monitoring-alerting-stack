import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import service


class T(unittest.TestCase):
    def setUp(self):
        service.chaos.update(error=0.0, slow=0.0)

    def test_chaos_valid(self):
        self.assertIsNone(service.set_chaos({"error": ["0.5"], "slow": ["2"]}))
        self.assertEqual(service.chaos, {"error": 0.5, "slow": 2.0})

    def test_chaos_reset_with_no_params(self):
        service.set_chaos({"error": ["0.5"]})
        self.assertIsNone(service.set_chaos({}))
        self.assertEqual(service.chaos, {"error": 0.0, "slow": 0.0})

    def test_chaos_rejects_bad_input_and_keeps_state(self):
        service.set_chaos({"error": ["0.2"]})
        for q in [{"slow": ["-1"]}, {"slow": ["abc"]}, {"error": ["2"]}, {"error": ["nan"]}, {"slow": ["inf"]}, {"slow": ["99"]}]:
            self.assertIsNotNone(service.set_chaos(q), q)
            self.assertEqual(service.chaos, {"error": 0.2, "slow": 0.0})


if __name__ == "__main__":
    unittest.main()
