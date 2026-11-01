# Restore the historical Governor sensor cache

`0427f2e` removed the 3.2.2 actuator cache while adding Governor cooldown. The
actuator's `status()` still reads fast/slow power limits and power draw from sysfs;
it does not cache these reads. Calls from Governor status and limits therefore
repeat sensor work within the same tick. The new regression test reproduced eight
actuator status reads for eight status requests at one timestamp, rather than one.
Existing scenario tests covered control outcomes, not the number of sensor reads.

Restore the historical 0.5-second cache and reuse the fresh ownership verification
snapshot. Invalidate it after claim, TDP write and restoration; a backwards clock
also forces a refresh. Ownership verification itself remains uncached. The four
new tests cover reuse/expiry, clock rewind, write/restore visibility and an external
change refreshed by ownership verification. The cooldown, probe-size protection
and nearest-watt fix remain present.

The four targeted tests pass locally; the reuse test failed before the patch
(8 reads versus 1). This measures Python call count, not physical Steam Deck
read frequency or energy. The release claim of six sensor reads per second still
needs a separate complete-service measurement. No hardware improvement is claimed.

The original LCD-75 trace remains at 35 FPS after this cache repair, with unchanged
reported metrics. A separate in-memory ablation substituting the entire historical
Governor reproduces the baseline's 37 FPS and metrics. This narrows the LCD problem
to Governor behavior/interactions; restoring caching alone does not fix it. Keep
the historical scenario and acceptance criteria unchanged.

Full regression on the final commit is required before accepting this change.
Rollback: revert the cache repair; no settings schema or engine pin changed.
Overall candidate remains BLOCKED by historical checks and other documented gaps.
