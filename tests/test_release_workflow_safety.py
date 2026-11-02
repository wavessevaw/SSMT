"""Tripwires for the candidate-only workflow, not GitHub branch protection."""
from pathlib import Path
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"


class ReleaseWorkflowSafetyTest(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_candidate_requires_manual_dispatch_and_has_no_tag_trigger(self):
        self.assertRegex(self.workflow, r"(?m)^  workflow_dispatch:\s*$")
        self.assertNotRegex(self.workflow, r"(?m)^  push:\s*$")

    def test_workflow_does_not_request_release_write_or_call_gh_release(self):
        self.assertNotRegex(self.workflow, r"(?m)^\s+contents:\s*write\s*$")
        self.assertNotIn("gh release", self.workflow)
        self.assertNotIn("--clobber", self.workflow)

    def test_candidate_depends_on_test_and_engine_builds(self):
        self.assertRegex(self.workflow, r"(?m)^  candidate:\s*\n    needs: \[test, gamescope, bench\]")
        self.assertIn("out/BUILD_MANIFEST.json", self.workflow)
        self.assertIn("out/SHA256SUMS.txt", self.workflow)

    def test_engine_builds_wait_for_software_admission(self):
        for job in ("bench", "gamescope"):
            self.assertRegex(self.workflow, rf"(?m)^  {job}:\s*\n    needs: test\s*$")

    def test_manual_build_executes_protected_historical_scenarios(self):
        self.assertIn('python3 -B tools/run_historical_sandbox.py --output "$RUNNER_TEMP/historical-sandbox.json"',
                      self.workflow)
        self.assertIn('${{ runner.temp }}/historical-sandbox.json', self.workflow)


if __name__ == "__main__":
    unittest.main()
