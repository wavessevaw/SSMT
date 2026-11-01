import contextlib
import io
import unittest

from tools.run_historical_sandbox import execute_scenarios


class HistoricalSandboxRunnerTest(unittest.TestCase):
    def test_exception_is_failure_and_does_not_hide_later_scenario(self):
        def broken():
            raise RuntimeError("reproduction")
        with contextlib.redirect_stdout(io.StringIO()):
            result = execute_scenarios({"broken": broken, "next": lambda: {"checks": [{"ok": True}]}})
        self.assertFalse(result["broken"]["pass"])
        self.assertIn("reproduction", result["broken"]["error"])
        self.assertTrue(result["next"]["pass"])

    def test_empty_checks_and_truthy_nonboolean_are_not_success(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = execute_scenarios({"empty": lambda: {"checks": []},
                                        "text": lambda: {"checks": [{"ok": "false"}]}})
        self.assertTrue(all(not r["pass"] for r in result.values()))
