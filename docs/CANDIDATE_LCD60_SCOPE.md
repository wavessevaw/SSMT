# Owner-approved candidate scope: LCD 60 Hz / 60 output FPS

On 2026-11-02 (Asia/Vladivostok), the owner explicitly selected:
**“60 Output FPS, LCD 60 Гц”**, after being told that LCD 75 Hz would remain
outside candidate admission with its known historical failure. This is a scope
decision, not a fix for the 75 Hz regression and not approval to publish Stable.

For this candidate, configure the Steam Deck LCD display to native **60 Hz in
SteamOS**. The target is 60 presented/generated output frames per second; fixed
x2 targets 30 original game frames per second. Auto may use other multipliers
(for example Battery: 20 original frames at x3). A game that cannot supply the
required source frames cannot be promised 60 output FPS. OLED and external-display
coverage remains subject to its existing checks; this decision does not limit them
to 60 Hz. The plugin does not forcibly change SteamOS display settings.

## Retained evidence and narrow exception

All 39 historical scenarios execute unchanged, including `lcd_unlocked_75hz`
with the original 40 FPS workload. Its raw result remains FAIL when it ends at
35 instead of 37. No original tag, source snapshot or assertion is changed.
The later 41 FPS workload is retained separately as a diagnostic, not a substitute.

`candidate_scope_gate.py` admits only the exact known failed assertion
`lcd_unlocked_75hz / x2 (37) when the game holds it`. Other failures, even other
assertions in that scenario, missing checks/scenarios, crashes, skips, dirty or
mismatched source, and missing evidence block admission. Raw current/historical
steps may report failure; their artifacts and full historical BLOCKED status are
retained. A mandatory separate scoped gate cannot turn an unrelated failure into
PASS. The AST source guard, backend, stress, frontend and build checks remain
mandatory. The exception is specific to this candidate scope, not a general
permission to weaken future tests.

The new `lcd_native_60hz_output` simulation exercises Auto and fixed x2 under all
four objectives, on a fixed 40 FPS modeled game at 60 Hz: 120 seconds warmup and
480 seconds observation. Before running it, acceptance was set to average
delivered output 59.4–60 FPS (1% model tolerance), existing >=97% smoothness,
expected source point and confirmed engine policy. This tolerance is not a
hardware measurement error allowance. All eight combinations pass locally;
real-device qualification remains UNVERIFIED.

## Device test and remaining limits

Use `DECK_TEST_PROTOCOL_3_2_2.md`, with LCD native 60 Hz as the supported LCD
matrix for this candidate. Record the actual display rate and source/output FPS.
Mark LCD nonstandard rates, including 75 Hz, OUTSIDE CANDIDATE SCOPE, never PASS.
Keep SDR/HDR, battery/charging, suspend/resume, installation/rollback, controls,
HUD and filters in the applicable matrix. Do not interpret scoped simulation PASS
as device safety, graphics-quality evidence, or Stable approval. Engine comparison
and final package verification remain separate requirements.
