import copy
import json
from pathlib import Path
import unittest
from tools.candidate_scope_gate import evaluate, EXCEPTION
from tools.guard_sandbox_source import BASELINE_SHA256

ROOT = Path(__file__).resolve().parents[1]


class CandidateScopeGateTest(unittest.TestCase):
    def setUp(self):
        self.baseline = json.loads((ROOT / "docs/baseline-3.2.2/sandbox.json").read_text())
        self.historical = {"results": copy.deepcopy(self.baseline), "status": "BLOCKED",
                           "candidate_sha": "abc", "working_tree_status": "",
                           "historical_source_sha256": BASELINE_SHA256}
        result = self.historical["results"][EXCEPTION[0]]
        for item in result["checks"]:
            if item["name"] == EXCEPTION[1]:
                item["ok"] = False
        result["pass"] = False
        self.current = copy.deepcopy(self.historical["results"])
        self.current["lcd_native_60hz_output"] = {"pass": True, "checks": [{"name": "output", "ok": True}]}
        self.names = set(self.current)

    def result(self):
        return evaluate(self.current, self.historical, self.baseline, self.names, "abc")

    def test_only_exact_approved_failure_is_scoped_and_remains_fail(self):
        report = self.result()
        self.assertEqual(report["scope_status"], "PASS")
        self.assertEqual(report["full_historical_status"], "BLOCKED")
        self.assertEqual(len(report["excluded_failures"]), 2)
        self.assertTrue(all(r["status"] == "FAIL" for r in report["excluded_failures"]))
        self.assertFalse(report["stable_admission"])

    def test_different_failure_in_excluded_scenario_still_blocks(self):
        result = self.current[EXCEPTION[0]]
        for item in result["checks"]:
            if item["name"] != EXCEPTION[1]:
                item["ok"] = False
                break
        self.assertEqual(self.result()["scope_status"], "BLOCKED")

    def test_native60_failure_blocks(self):
        self.current["lcd_native_60hz_output"]["checks"][0]["ok"] = False
        self.current["lcd_native_60hz_output"]["pass"] = False
        self.assertEqual(self.result()["scope_status"], "BLOCKED")

    def test_missing_and_renamed_checks_block(self):
        self.current[EXCEPTION[0]]["checks"].pop()
        self.assertEqual(self.result()["scope_status"], "BLOCKED")

    def test_missing_historical_scenario_blocks(self):
        del self.historical["results"][EXCEPTION[0]]
        self.assertEqual(self.result()["scope_status"], "BLOCKED")

    def test_wrong_revision_dirty_source_or_unverified_execution_block(self):
        for key, value in (("candidate_sha", "wrong"), ("working_tree_status", " M service.py"),
                           ("status", "UNVERIFIED"), ("historical_source_sha256", "wrong")):
            with self.subTest(key=key):
                before = self.historical[key]
                self.historical[key] = value
                self.assertEqual(self.result()["scope_status"], "BLOCKED")
                self.historical[key] = before

    def test_exception_does_not_hide_crash(self):
        self.historical["results"][EXCEPTION[0]]["error"] = "crashed"
        self.assertEqual(self.result()["scope_status"], "BLOCKED")

    def test_scope_gate_is_mandatory_in_both_workflows(self):
        for name in ("ci.yml", "release.yml", "installer-staging.yml"):
            text = (ROOT / ".github/workflows" / name).read_text()
            block = text.split("      - name: Enforce owner-approved LCD60 candidate scope\n", 1)[1].split("      - name:", 1)[0]
            self.assertNotIn("continue-on-error", block)
            self.assertIn("tools/candidate_scope_gate.py", block)
            self.assertIn("--revision", block)


if __name__ == "__main__":
    unittest.main()
