"""Owner-approved LCD60 admission; keep the full historical verdict unchanged."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.compare_sandbox import checks
from tools.guard_sandbox_source import BASELINE_SHA256, contracts

SCOPE = "lcd60-output60-2026-11-02"
EXCEPTION = ("lcd_unlocked_75hz", "x2 (37) when the game holds it")


def evaluate(current, historical, baseline, expected_current, revision):
    errors = []
    excluded = []
    if historical.get("candidate_sha") != revision or historical.get("working_tree_status"):
        errors.append("historical source revision is mismatched or dirty")
    if historical.get("historical_source_sha256") != BASELINE_SHA256:
        errors.append("historical source digest mismatch")
    if historical.get("status") not in ("PASS", "BLOCKED") or historical.get("error"):
        errors.append("historical execution is unverified")
    try:
        reference = checks(baseline)
        now = checks(current)
        old = checks(historical["results"])
        if any(result.get("error") or result.get("skipped")
               for results in (current, historical["results"]) for result in results.values()):
            errors.append("scenario error or skip is not covered by the scope exception")
        if set(now) != set(expected_current) or "lcd_native_60hz_output" not in now:
            errors.append("current scenario identity set mismatch")
        if set(old) != set(reference) or len(old) != 39:
            errors.append("historical scenario identity set mismatch")
        if not all(all(c.values()) for c in reference.values()):
            errors.append("baseline contains failures")
        for label, data in (("current", now), ("historical", old)):
            for name, required in reference.items():
                if name not in data or set(data[name]) != set(required):
                    errors.append(f"{label}: historical check identities changed: {name}")
            for name, items in data.items():
                for check, ok in items.items():
                    if not ok:
                        if (name, check) == EXCEPTION:
                            excluded.append({"report": label, "scenario": name, "check": check, "status": "FAIL"})
                        else:
                            errors.append(f"{label}: failed check: {name}: {check}")
        expected_status = "PASS" if all(all(c.values()) for c in old.values()) else "BLOCKED"
        if historical.get("status") != expected_status:
            errors.append("historical summary contradicts check outcomes")
    except (ValueError, KeyError, TypeError) as exc:
        errors.append(f"invalid or incomplete evidence: {exc}")
    return {"scope": SCOPE, "scope_status": "BLOCKED" if errors else "PASS",
            "full_historical_status": historical.get("status", "UNVERIFIED"),
            "excluded_failures": excluded, "errors": errors,
            "hardware_status": "UNVERIFIED", "stable_admission": False,
            "limitations": "LCD native 60 Hz / 60 output FPS only. 75 Hz is outside candidate admission, not fixed."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--historical", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inputs = {"current": args.current, "historical": args.historical,
              "baseline": ROOT / "docs/baseline-3.2.2/sandbox.json"}
    try:
        raw = {name: path.read_bytes() for name, path in inputs.items()}
        data = {name: json.loads(value.decode("utf-8-sig")) for name, value in raw.items()}
        names, _ = contracts((ROOT / "sandbox/run.py").read_bytes())
        report = evaluate(data["current"], data["historical"], data["baseline"], names, args.revision)
        actual = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for folder in ("py_modules", "sandbox") for p in sorted((ROOT / folder).rglob("*.py"))}
        if data["historical"].get("source_sha256") != actual:
            report["scope_status"] = "BLOCKED"
            report["errors"].append("product/model source hashes differ from the executed historical run")
        report["input_sha256"] = {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report = {"scope": SCOPE, "scope_status": "BLOCKED", "errors": [str(exc)]}
    report["candidate_sha"] = args.revision
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["scope_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
