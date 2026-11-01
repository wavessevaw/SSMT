"""Execute all byte-protected 3.2.2 scenarios against the current product/model.

This is simulation coverage, not hardware validation. No filters or scenario
overrides are accepted. The AST and assertion comparison gates remain separate.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import traceback
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "py_modules")]
from tools.guard_sandbox_source import BASELINE_SHA256, contracts


def execute_scenarios(scenarios):
    results = {}
    for name, fn in scenarios.items():
        try:
            result = fn()
            checks = result.get("checks", [])
            result["pass"] = bool(checks) and all(c.get("ok") is True for c in checks)
        except Exception:
            result = {"pass": False, "error": traceback.format_exc(), "checks": []}
        results[name] = result
        print(f"{'PASS' if result['pass'] else 'FAIL'} {name}", flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; preserve previous observations")
    snapshot = ROOT / "docs/baseline-3.2.2/sandbox-run.py.txt"
    source = snapshot.read_bytes()
    if hashlib.sha256(source).hexdigest() != BASELINE_SHA256:
        parser.error("historical source SHA256 mismatch")
    names, _ = contracts(source)
    if len(names) != 39:
        parser.error("historical scenario identity set is invalid")
    sys.pycache_prefix = str(args.output.resolve().with_suffix(".unused-cache"))
    if Path(sys.pycache_prefix).exists():
        parser.error("bytecode isolation path already exists")
    sys.dont_write_bytecode = True
    git = ["git", "-c", f"safe.directory={ROOT.as_posix()}"]
    candidate_sha = subprocess.check_output(git + ["rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    tracked = subprocess.check_output(git + ["status", "--porcelain"], cwd=ROOT, text=True)
    def source_hashes():
        return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for folder in ("py_modules", "sandbox") for p in sorted((ROOT / folder).rglob("*.py"))}
    hashes = source_hashes()
    module = types.ModuleType("sandbox.historical_contracts")
    module.__package__ = "sandbox"
    # Keep the historical script's repository-relative ROOT semantics. Exception
    # tracebacks still name the immutable snapshot via compile's filename.
    module.__file__ = str(ROOT / "sandbox/run.py")
    sys.modules[module.__name__] = module
    exec(compile(source, str(snapshot), "exec"), module.__dict__)
    if set(module.SCENARIOS) != set(names):
        parser.error("registered scenarios differ from protected source")
    results = execute_scenarios(module.SCENARIOS)
    report = {"evidence_class": "B: simulation, not hardware", "historical_source_sha256": BASELINE_SHA256,
              "candidate_sha": candidate_sha, "working_tree_status": tracked, "source_sha256": hashes,
              "python": sys.version, "results": results,
              "status": "PASS" if all(r["pass"] for r in results.values()) else "BLOCKED"}
    if source_hashes() != hashes:
        report["status"] = "UNVERIFIED"
        report["error"] = "product/model sources changed during execution"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
