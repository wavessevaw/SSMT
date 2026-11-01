# LCD-75 seeded comparison: design before execution

Compare exact historical 3.2.2 against the sensor-cache candidate at fixed seeds
1, 2, 3, 4, 5. Every run uses the original 40 FPS at 15 W load, Balanced, 75 Hz,
900 simulated seconds, and unchanged model/controller parameters. Report the
original post-120-second metrics plus total original frames and presentation
repeat counters over the same tail. Preserve every result and every failure.

Run each checkout/seed in a fresh Python process with existing bytecode excluded.
Reject uncommitted product/model changes and existing output files. Record source
SHA, per-file hashes, environment, exact parameters, full timeline and decisions.

This diagnostic comparison has no retrospective tolerance and does not replace
the historical 39-scenario guard. Seed 1 must still satisfy the original test.
For each other seed show whether original frames, final cap, pacing or energy
worsen individually; an improvement in one does not offset loss in another.
Do not select only favourable seeds or treat model variation as hardware noise.
No product/controller patch is introduced by this experiment.

Stop any proposed fix that requires removing cooldown, reducing the two-window
guard, crossing controller experiment boundaries, modifying the workload/model
or relaxing historical assertions merely to obtain a passing final cap.

## Result: no candidate acceptance

Historical product: `92b557f678d118ae97948673c26e2498b7a28467`.
Candidate product: `45774efb410758762003958ccbcdd44bde13174f` locally,
tree-equivalent to GitHub `d4481fdf76a63cd899c6b005723edc463d69c862`.
Product/model files were clean in both checkouts; documentation/tool changes are
reported separately in the saved working-tree status. Model hashes match.

Values below are modeled counters over seconds 120–899; fractional counts arise
from the virtual engine. They are not physical frame captures.

| Seed | Original frames baseline → candidate | Repeats baseline → candidate | Energy Wh baseline → candidate |
|---|---:|---:|---:|
| 1 | 28444.21 → 27333.68 | 93.47 → 59.34 | 3.103 → 2.949 |
| 2 | 27307.77 → 27306.37 | 42.65 → 45.63 | 2.920 → 2.921 |
| 3 | 27350.83 → 27341.51 | 106.92 → 106.40 | 2.955 → 3.013 |
| 4 | 27366.71 → 27369.11 | 123.12 → 131.63 | 2.987 → 3.025 |
| 5 | 27345.60 → 27439.11 | 76.65 → 131.16 | 2.952 → 3.038 |

Only baseline seed 1 ends at 37; all other runs end at 35. The original seed-1
requirement remains binding. No aggregate average can erase that failure or the
other per-seed losses. Repeating all ten runs with raw counter observation added
produced exactly identical existing metric dictionaries to the first ten runs.
No affected case was removed from the evidence.

`lcd-seeded-evidence.zip` contains all ten full traces, runner source, comparison
and per-file SHA256 manifest. Archive SHA256:
`e1fb0703934fab92967d95795d7306057cf68a6051fa8b7ade85423659d8502b`.
The archive was reopened and every manifest entry rehashed successfully.

## Direct historical execution gate

`python3 -B tools/run_historical_sandbox.py --output historical-sandbox.json`
loads the immutable historical scenario source only after checking its SHA256,
then executes every one of its 39 registered scenarios against the current
product and current virtual Deck. It does not substitute old controller modules.
The module's repository-relative path semantics are retained; tracebacks name
the archived source. Exceptions are failures and do not hide later scenarios.
Empty checks cannot become a vacuous pass. Output files cannot be overwritten.

CI now retains this report alongside the existing current-scenario, stress,
assertion-comparison and AST-comparison reports. Neither previous guard is
removed or weakened. A successful historical execution covers only these model
contracts; it does not prove hardware performance, image quality, installation
or acceptable behavior for subsequently approved additions.
