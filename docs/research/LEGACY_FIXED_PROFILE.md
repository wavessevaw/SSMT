# Legacy fixed-mode migration

The protected 3.2.2 scenario checks migration of a legacy profile, not the runtime
behavior of an explicitly selected fixed multiplier. Current normalization had
lost that distinction when fixed controls were restored in schema 4: an old x3
profile could unexpectedly become fixed again after upgrading.

Profile normalization now receives the enclosing settings schema. Legacy input
retains the 3.2.2 rule (Off remains Off, other old fixed values become Auto).
Schema 4 input and current user edits/copies preserve all supported fixed choices.
The standalone normalizer retains its historical legacy-input default; production
call sites explicitly supply either the loaded schema or the current schema.

Two new tests failed before the fix. After it, all 11 settings-upgrade tests and
36 core tests pass locally, including a real-file migration, subsequent fixed
selection, per-game copy, save and reload. The historical fixed_x3_too_heavy
scenario has been restored verbatim. The current runtime check is retained as
the additional current_fixed_x3_too_heavy scenario. Both pass; neither assertion
was weakened. The source archive remains unchanged.

These are functional and simulation checks. They do not establish device results
or resolve the independent low-battery and LCD historical failures. No diagnostic
candidate with known failures is authorized by the owner.
