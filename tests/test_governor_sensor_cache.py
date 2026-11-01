"""Keep the 3.2.2 polling bound without caching away ownership changes or writes."""
import unittest
from unittest.mock import patch

from sandbox.run import VClock, VPower
from gfg_plugin.governor import PowerGovernor


class GovernorSensorCacheTest(unittest.TestCase):
    def setUp(self):
        self.clock = VClock()
        self.power = VPower(user_tdp=15.0)
        self.gov = PowerGovernor(self.power, None, clock=self.clock)

    def test_repeated_status_reads_share_snapshot_until_half_second(self):
        with patch.object(self.power, "status", wraps=self.power.status) as read:
            for _ in range(8):
                self.gov.status()
            self.assertEqual(read.call_count, 1)
            self.clock.t += 0.49
            self.gov.status()
            self.assertEqual(read.call_count, 1)
            self.clock.t += 0.01
            self.gov.status()
            self.assertEqual(read.call_count, 2)

    def test_clock_rewind_invalidates_snapshot(self):
        with patch.object(self.power, "status", wraps=self.power.status) as read:
            self.gov.status()
            self.clock.t -= 1
            self.gov.status()
            self.assertEqual(read.call_count, 2)

    def test_write_and_restore_invalidate_previous_observation(self):
        self.gov._start("test", "balanced")
        self.gov.status()
        self.assertTrue(self.gov._write(12.0, "test"))
        self.assertEqual(self.gov.status()["observed_tdp_w"], 12.0)
        self.gov.stop()
        self.assertEqual(self.gov.status()["observed_tdp_w"], 15.0)

    def test_ownership_check_refreshes_cached_sensor_snapshot(self):
        self.gov._start("test", "balanced")
        self.gov.status()
        self.power.tdp = 11.0  # an external tool changes the cap within the cache interval
        self.gov._verified()
        self.assertEqual(self.gov.status()["observed_tdp_w"], 11.0)
        with patch.object(self.power, "status", wraps=self.power.status) as read:
            for _ in range(8):
                self.gov.status()
            self.assertEqual(read.call_count, 0, "reuse the ownership check's fresh snapshot")


if __name__ == "__main__":
    unittest.main()
