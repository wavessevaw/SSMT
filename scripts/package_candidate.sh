#!/usr/bin/env bash
# Package a candidate from checked source and prebuilt inputs; never publish it.
set -euo pipefail
engine="${1:?engine source required}"
runtime="${2:?runtime required}"
bench="${3:?bench required}"
out="${4:?output directory required}"
: "${TAG:?candidate tag required}" "${GITHUB_SHA:?GFG SHA required}" "${KEDAVRA_SHA:?GSF SHA required}" "${KEDAVRA_REPO:?GSF repository required}"
export GFG_SHA="$(git rev-parse HEAD)"
test "$GFG_SHA" = "$GITHUB_SHA" || { echo 'source commit mismatch' >&2; exit 1; }
test "$(git -C "$engine" rev-parse HEAD)" = "$KEDAVRA_SHA" || { echo 'engine commit mismatch' >&2; exit 1; }
export GAMESCOPE_SHA="$(sed -n 's/^GAMESCOPE_COMMIT=//p' "$engine/kedavra/gamescope/apply.sh")"
[[ "$GAMESCOPE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo 'invalid Gamescope revision' >&2; exit 1; }
version="$(python3 -c 'import json; print(json.load(open("package.json"))["version"])')"
test "v$version" = "$TAG" || { echo 'candidate version mismatch' >&2; exit 1; }
scripts/build_release.sh "$version" "$runtime" "$out" "$bench"
python3 -c 'import json, os; print(json.dumps({"gfg_sha": os.environ["GFG_SHA"], "gsf_repository": os.environ["KEDAVRA_REPO"], "gsf_sha": os.environ["KEDAVRA_SHA"], "gamescope_sha": os.environ["GAMESCOPE_SHA"], "candidate_version": os.environ["TAG"], "qualification_scope": "lcd60-output60-2026-11-02", "lcd_display_hz": 60, "lcd_target_output_fps": 60, "lcd_75hz_status": "OUTSIDE_CANDIDATE_SCOPE_KNOWN_FAIL", "hardware_status": "UNVERIFIED", "stable_admission": False}, indent=2))' > "$out/BUILD_MANIFEST.json"
(cd "$out" && sha256sum BUILD_MANIFEST.json >> SHA256SUMS.txt && sha256sum -c SHA256SUMS.txt)
unzip -Z1 "$out"/*.zip | grep -Fx GFG-Extreme/runtime/bin/gamescope
unzip -Z1 "$out"/*.zip | grep -Fx GFG-Extreme/bin/kfg_pacing.py
unzip -Z1 "$out"/*.zip | grep -Fx GFG-Extreme/bench/kfg_vktest
unzip -Z1 "$out"/*.zip | grep -Fx GFG-Extreme/dist/index.js
if unzip -Z1 "$out"/*.zip | grep -iE 'mako|lsfg|lossless' > /dev/null; then echo 'unexpected MAKO/LSFG files in zip' >&2; exit 1; fi
