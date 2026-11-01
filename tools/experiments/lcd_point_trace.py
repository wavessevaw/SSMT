"""Record the original LCD-75 workload without changing policy or test criteria.

Run in a fresh process for each source checkout. All results are simulation data.
The wrapper observes completed pilot windows and returns their original verdicts.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    sys.path.insert(0, str(repo))
    from sandbox.run import VGame, make_rig, metrics, play

    # Trust only this explicitly selected checkout; do not alter global Git config.
    git = ["git", "-c", f"safe.directory={repo.as_posix()}"]
    source = subprocess.check_output(git + ["rev-parse", "HEAD"], cwd=repo, text=True).strip()
    tracked = subprocess.check_output(git + ["status", "--porcelain"], cwd=repo, text=True)
    hashes = {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
              for folder in ("py_modules", "sandbox") for p in sorted((repo / folder).rglob("*.py"))}
    rig = make_rig(VGame("lcd", 40.0), objective="balanced", refresh=75)
    windows = []
    original = rig.service.pilot._window

    def observe(ctx, cap, length):
        phase = rig.service.pilot.phase
        result = original(ctx, cap, length)
        if result is not None:
            windows.append({"t": rig.clock(), "phase": phase, "cap": cap,
                            "window_seconds": length, "verdict": dict(result)})
        return result

    rig.service.pilot._window = observe
    try:
        play(rig, 900)
        result = {"evidence_class": "B: simulation, not hardware", "source_sha": source,
                  "working_tree_status": tracked, "python": sys.version, "source_sha256": hashes,
                  "parameters": {"game_fps_at_15w": 40.0, "refresh": 75, "objective": "balanced",
                                 "duration_seconds": 900, "metrics_skip_seconds": 120},
                  "metrics": metrics(rig, skip=120), "pilot_windows": windows,
                  "events": rig.log, "timeline": rig.timeline}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
        print(json.dumps({"source_sha": source, "metrics": result["metrics"]}, indent=2))
    finally:
        rig.engine.cpu.tmp.cleanup()


if __name__ == "__main__":
    main()
