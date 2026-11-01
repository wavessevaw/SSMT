"""Retain the 3.2.2 virtual Deck LCD scenario at its original game load."""
import unittest

from sandbox.run import VGame, ladder, make_rig, metrics, play


class Lcd75HzBaselineTest(unittest.TestCase):
    def test_original_and_extra_margin_game_loads(self):
        for fps in (40.0, 41.0):
            with self.subTest(game_fps_at_15w=fps):
                rig = make_rig(VGame("lcd", fps), objective="balanced", refresh=75)
                self.addCleanup(rig.engine.cpu.tmp.cleanup)
                play(rig, 900)
                result = metrics(rig, skip=120)
                self.assertTrue(set(result["points"]) <= set(ladder(75)), result)
                self.assertEqual(result["point_final"], 37, result)
                self.assertGreaterEqual(result["smooth_pct"], 97.0, result)


if __name__ == "__main__":
    unittest.main()
