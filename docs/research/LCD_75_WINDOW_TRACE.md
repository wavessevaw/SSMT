# LCD 75 Hz: original workload remains BLOCKED

The trace runner `tools/experiments/lcd_point_trace.py` observes completed pilot
windows without replacing their verdicts. It records full timelines, controller
events, Python version, Git revision, dirty status and hashes of all Python product
and sandbox sources. Run it in a fresh process for each checkout. Output uses
exclusive creation to preserve previous observations.

Both runs use the original 40 FPS at 15 W game, Balanced, 75 Hz, 900 simulated
seconds, and metrics after second 120. The model source hash is identical:
`1d13d398a6ab86a73f32ffb72a326b879aff955ef5131ce2f858d82e57b121d4`.

| Observation | Historical 92b557f | Integration 207bf3a |
|---|---:|---:|
| Final source cap | 37 | 35 |
| Model smooth percentage | 99.7 | 100.0 |
| Model energy, Wh | 3.103 | 2.949 |
| TDP writes | 8 | 9 |
| Promotions / demotions | 2 / 1 | 1 / 1 |

Lower energy and higher smooth percentage do not establish an improvement because
the candidate renders fewer original frames. No acceptance threshold was changed.

Both versions first promote to 37 at virtual clock 1184. In the candidate the
1194 window has pace 0.989333, below the unchanged 0.99 requirement, while its
entire window is at the power ceiling. It demotes. In the baseline the 1194
window includes a power probe, so the pilot correctly leaves power recovery to
the Governor; it demotes at 1199 instead. The next trial starts five seconds
later in the baseline and passes, whereas the candidate's subsequent two trials
fail pacing. This is a timing-sensitive interaction, not evidence that the pacing
criterion should be loosened. The source of the persistent difference still
requires isolation.

An in-memory diagnostic ablation setting CALM_S and CALM_MAX_S to zero also ends
at 35 (three promotions, three demotions, smooth 99.4%, ten TDP writes). Removing
cooldown alone is therefore not a solution; no such change was made to product
code. Next investigation must separate telemetry calculation, probe timing and
Power Split recovery with the same model and original workload, retaining all
failed cases. A final-cap assertion alone cannot establish a robust fix.

Raw local evidence under `baseline-evidence`:

* `lcd75-baseline-trace.json`: SHA256 `953d452bf35b5b45a87514fc189b2f984a211c239da0f535865576f0575638b8`.
* `lcd75-integration-trace.json`: SHA256 `cd24b2bc607e9a3080a7b8766b72fa7f58ff56ce4eb3e5c2c863f55866387be1`.

Historical checkout modifications are documentation only. The candidate had the
untracked trace runner; actual product/model hashes are recorded in the JSON.
All observations are class B simulation, not Steam Deck performance measurements.
