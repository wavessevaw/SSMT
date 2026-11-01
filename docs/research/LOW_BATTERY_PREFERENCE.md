# Low battery: restore Auto and retain explicit Smoothness

The 3.3.0 pilot unconditionally stopped selecting the low battery point in
Autopilot. That made the protected 3.2.2 battery-drain scenario fail both with and
without permission for multipliers above x3. The existing preference selector
already exposes Auto, Battery and Smoothness, but the pilot did not receive it.

Auto again follows the historical low-battery policy: below the existing 20%
threshold, select x3, or x3.75 only when permitted. Existing battery hysteresis
(recovery above 25%) is retained. Explicit Smoothness keeps the later behavior of
following the game rather than forcing a lower source cap. Fixed multipliers
still bypass Auto. This changes the current Auto default back to the 3.2.2
contract; it is not a claim of improved FPS or energy efficiency.

Service forwards the existing preference to the pilot, and the displayed reason
distinguishes the two behaviors. No new controller or settings field is introduced.
The historical battery_drains_autopilot source is restored verbatim. The later
game-following assertions remain in battery_drains_smoothness with explicit
Smoothness selection. Both pass locally. A new unit reproducer failed before the
fix (45 instead of 24); all 38 core tests pass after it. Full Linux regression and
the remaining LCD failure are still required before candidate admission.
