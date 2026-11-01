# Acknowledgement belongs to a specific policy

Service computed `confirmed` before asking the pilot for its next point. That
value described the previous policy. After selecting and submitting a new policy,
it reused that boolean for `locked`, so CPU/GPU control could see a steady point
before the engine acknowledged it. Governor could also receive the new cap with
observations from the previous one.

The reproducer starts at confirmed fixed x2, changes to x3, and inspects the
controller inputs during the transition. Before the fix Power Split receives
`steady=True`. After it both controllers wait and Governor cap is zero until
the new policy is confirmed; afterwards Governor receives cap 30. The normal
coordinator owns acknowledgement; no parallel controller or retry is introduced.

Service now checks `engine_runs(policy, st)` for the newly submitted policy when
determining `locked`. The earlier confirmation remains valid for the pilot's
evaluation of preceding observations. Local confirmation and settings-upgrade
tests pass. Full Linux integration remains required.

The original LCD 75 Hz workload still ends at 35 rather than 37 in a targeted
simulation after this change. This is a separate confirmed control defect, not
an asserted fix for LCD. Its criteria and protected source remain unchanged.
