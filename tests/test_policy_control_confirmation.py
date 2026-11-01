"""Controller ownership must follow the policy actually acknowledged by the engine."""
import unittest
from unittest.mock import patch
from sandbox.run import make_rig, play
from sandbox.vdeck import VGame


class PolicyControlConfirmationTest(unittest.TestCase):
    def test_new_fixed_point_waits_for_ack_before_power_experiments(self):
        rig = make_rig(VGame("steady", 90, noise=0), fg="x2", objective="balanced")
        self.addCleanup(rig.engine.cpu.tmp.cleanup)
        self.addCleanup(rig.service.stop)
        play(rig, 120)
        self.assertEqual(rig.service.snapshot["policy"]["state"], "confirmed")
        rig.settings.data["profiles"]["default"]["fg"] = "x3"
        with patch.object(rig.service.governor, "settled", return_value=True), \
             patch.object(rig.service.split, "tick", return_value={}) as split, \
             patch.object(rig.service.gclock, "tick", return_value={}) as gpu:
            play(rig, 1)
            self.assertFalse(split.call_args.args[0]["steady"])
            self.assertFalse(gpu.call_args.args[0]["steady"])
            self.assertEqual(rig.service.governor.cap, 0, "old samples cannot describe the new cap")
            play(rig, 4)
            self.assertEqual(rig.service.snapshot["policy"]["state"], "confirmed")
            self.assertEqual(rig.service.governor.cap, 30)


if __name__ == "__main__":
    unittest.main()
