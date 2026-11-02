"""Frame-generation point pilot: GSF Kedavra Auto and the GFG Extreme Autopilot as one.

A "point" is the game's base frame cap; KEDAVRA generates the rest of the refresh. At 90 Hz
the ladder is 24 (x3.75), 30 (x3), 35 (x2.6), 40 (x2.25), 45 (x2); at 60 Hz 20 (x3), 25, 30.

GFG Extreme's principles, with Kedavra Auto as the measuring instrument:

  MEASURE  the TDP is held at the ceiling and KEDAVRA Auto finds, vblank by vblank, the
           highest cap the game holds (it counts late frames, which a 1 s average cannot).
           The pilot waits until Auto stays on one cap for 12 s (at most 45 s, then the median).
  LOCK     the point is fixed as the engine's base cap: no more Auto trials, an even cadence,
           and the Governor looks for the lowest TDP that still holds it.
  GUARD    every 5 s the pilot checks the game against the point. If it falls short while
           the TDP is at the ceiling (the game itself got heavier), the point steps down at
           once to the highest cap the game's frame rate holds. A shortfall below the ceiling
           is the Governor's: it raises the TDP (to the ceiling at once when severe).
  ADAPT    Quality and Balanced try the next point up after 2 min clean (backing off to 16
           min after failed tries): TDP at the ceiling, 6 s trial, kept only if the game holds.

Objectives: Quality and Balanced climb to the top of the ladder (Balanced then saves watts
with a margin, Quality keeps the player's TDP, at most 15 W). Balanced never goes below x3,
Quality never below x2.5 (user, 3.0.10). Battery locks the lowest point, x3.75 at 90 Hz
(x3 at 60 Hz): the game renders the fewest frames, generation fills the screen. Battery and
Autopilot fall back to x3.75 when a game cannot hold x3 even at full power: frames held 3-4 refreshes
look smoother than x3 with repeated frames.

Fixed choices (x2, x3, x3.75, a custom cap) are never changed by the pilot; it only reports when
the chosen point does not hold.
"""
from __future__ import annotations

import math
import statistics
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

STABLE_S = 12.0
MEASURE_MIN_S = 8.0
MEASURE_MAX_S = 45.0
GUARD_S = 5.0
# A trial of the next point up lasts long enough to see a scene change (3.3.1: 6 s trials held,
# then the point fell within a minute, and every switch is a visible change of cadence).
TRIAL_S = 6.0
TRIAL_EARLY_S = 4.0     # ... but one clearly short (median under 92 %) ends after 4 s
TRIAL_EARLY_RATIO = 0.92
# A point that falls within this long of its promotion did not really hold: it waits like a
# failed trial before the next try.
PROBATION_S = 180.0
# Per point, the wait before climbing into it again after it failed: 1, 2, 4, 8, 16 min.
RETRY_BASE_S = 60.0
RETRY_MAX_S = 960.0
PROMOTE_WAIT_S = 120.0
PROMOTE_WAIT_MAX_S = 960.0
# Headroom: the GPU (or the CPU) clearly not at its limit at the locked point means the game
# could run the next point up. Then the next point is tried after HEADROOM_WAIT_S of clean play,
# whatever the back-off, so a lighter scene gets its real frames back within seconds.
MEASURE_GAP_S = 2.5   # seconds without a confirmed Auto cap that break a measurement
HEADROOM_WAIT_S = 10.0
HEADROOM_GPU_PCT = 92.0      # the GPU at the next point is estimated to stay under this
HEADROOM_CPU_PCT = 85.0
HOLD_RATIO = 0.95       # real fps vs cap over a guard window
AUTOPILOT_HOLD_RATIO = 0.93  # Autopilot: a dip of 5-8 % is still fine (user, 3.1.1; was 12 %)
AUTOPILOT_PACE_MIN = 0.93
TRIAL_RATIO = 0.97
PACE_MIN = 0.99
# Autopilot's power target (user, 3.0.3): 12 W is good, 9-10 W ideal. The Governor may go higher
# to hold a point, but a point that needs more than 12 W for a minute is given up for the next
# lower one, and Autopilot climbs only while the TDP is at the ideal.
POWER_GOOD_W = 12.0
POWER_IDEAL_W = 10.0
# Autopilot (user, 3.3.0): the multiplier follows the game's base FPS, not the other way round.
# Whenever the game holds more than 30, the x2 family (35, 40, 45 at 90 Hz) is the aim; x3 stays
# only where the game really does not get above 30. Every point is climbed into and kept below
# the whole 15 W; x3.75 only when x3 needs it. A low battery keeps following the game, it only
# climbs within the good 12 W.
BAND_MAX_W = 14.5
# The highest multiplier per objective (user, 3.0.10): Balanced works down to x3, Quality to x2.5.
MAX_MULT = {"balanced": 3.0, "quality": 2.5}
DOCK_MAX_MULT = 3.0
LIGHT_MAX_MULT = 2.0
LIGHT_OBJECTIVES = ("balanced", "quality", "autopilot")
POWER_WAIT_S = 60.0
KNOWN_RECHECK_S = 30.0
COSTLY_HOLD_S = 600.0


def div_up(a: int, b: int) -> int:
    return (a + b - 1) // b


def ladder(refresh: int) -> List[int]:
    """Base caps for a refresh rate, ascending (same steps as kfg_auto, plus x3.75)."""
    refresh = max(30, int(refresh))
    lo, hi = div_up(refresh, 3), refresh // 2
    floor = max(20, div_up(refresh * 4, 15))
    out = [floor] if floor < lo else []
    out.append(lo)
    c = (lo // 5 + 1) * 5
    while c < hi:
        out.append(c)
        c += 5
    if hi > lo:
        out.append(hi)
    return out


def lowest_point(refresh: int, objective: str, light: bool = False, dock: bool = False) -> int:
    """The lowest point an objective may use: the first cap whose multiplier is within its limit.
    Dock (user, 3.1.0): a TV with an external gamepad needs low latency, x3 at most in every mode.
    A light game (DuckTales, Terraria) wants no multiplier above x2 in Balanced, Quality, Autopilot."""
    steps = ladder(refresh)
    mult = MAX_MULT.get(objective)
    if dock:
        mult = min(mult or DOCK_MAX_MULT, DOCK_MAX_MULT)
    if light and objective in LIGHT_OBJECTIVES:
        mult = min(mult or LIGHT_MAX_MULT, LIGHT_MAX_MULT)
    if mult is None:
        return steps[0]
    return next((c for c in steps if refresh / c <= mult + 1e-6), steps[-1])


def x375_cap(refresh: int) -> int:
    return ladder(refresh)[0]


def x3_cap(refresh: int) -> int:
    return div_up(max(30, int(refresh)), 3)


def capped(steps: List[int], refresh: int, above_x3: bool) -> List[int]:
    """Above x3 (user, 3.1.1): "above x3 plays very badly", so x3 is the limit in every mode and
    x3.75 is unlocked only by the setting."""
    return steps if above_x3 else [c for c in steps if c >= x3_cap(refresh)]


def mult_label(refresh: int, cap: int) -> str:
    if cap <= 0:
        return "off"
    v = refresh / cap
    if abs(v * 4 - round(v * 4)) < 0.01:
        return "x" + f"{round(v * 4) / 4:.2f}".rstrip("0").rstrip(".")
    return f"x{v:.1f}"


class PointPilot:
    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 journal: Optional[Callable[..., None]] = None) -> None:
        self.clock = clock
        self.journal = journal
        self.reset()

    def reset(self) -> None:
        self.context: Optional[tuple] = None
        self.light_failed = False   # listed as light, but x2 does not hold even at full power
        self.measured = 0           # what the last full-power measurement held (read once by the service)
        self.phase = "idle"
        self.point = 0
        self.reason = ""
        self.since = self.clock()
        self.caps: Deque[tuple] = deque(maxlen=120)  # (t, engine cap) during measure
        self.window: List[Dict[str, Any]] = []
        self.window_done: List[Dict[str, Any]] = []
        self.window_start = self.clock()
        self.clean_since: Optional[float] = None
        self.promote_wait = PROMOTE_WAIT_S
        self.headroom_backoff = 0.0  # after a failed trial the headroom estimate waits this long
        self.costly_since: Optional[float] = None
        self.costly: Dict[int, float] = {}  # point -> until: needed more than POWER_GOOD_W
        self.falls: Dict[int, int] = {}     # point -> times it fell at full power this game
        self.tries: Dict[int, int] = {}     # point -> failed climbs into it this game
        self.retry_at: Dict[int, float] = {}  # point -> earliest next climb into it
        self.promoted_at: Optional[float] = None
        self.trial_cap = 0
        self.trial_from = 0
        self.holds = True
        self.demotions = 0
        self.promotions = 0
        self._measure_sample_id = None
        self._measure_sample_at = None

    def _note(self, kind: str, **fields: Any) -> None:
        if self.journal:
            try:
                self.journal(kind, **fields)
            except Exception:
                pass

    def _go(self, phase: str, reason: str, point: Optional[int] = None) -> None:
        now = self.clock()
        if point is not None:
            self.point = point
        self._note("pilot", phase=phase, was=self.phase, point=self.point, reason=reason)
        self.phase, self.reason, self.since = phase, reason, now
        self.window, self.window_start = [], now
        self.clean_since = None

    # ------------------------------------------------------------------ request
    @staticmethod
    def _request(multiplier: int, base: int, floor: int, hold: bool) -> Dict[str, Any]:
        return {"multiplier": multiplier, "base_fps": base, "auto_floor": floor, "hold_ceiling": hold}

    def tick(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """ctx: fg, base_fps, objective (quality|balanced|battery), refresh, game (bool), app,
        engine (status dict), real (game fps over the last second or None), pace, confirmed
        (the engine runs the policy the pilot asked for), at_ceiling (TDP at the ceiling or not
        managed)."""
        fg = ctx.get("fg", "auto")
        refresh = int(ctx.get("refresh") or 90)
        above = bool(ctx.get("above_x3"))
        steps = capped(ladder(refresh), refresh, above)
        if fg == "x3.75" and not above:
            fg = "x3"
        objective = ctx.get("objective", "balanced")
        context = (ctx.get("app"), refresh, fg, objective, int(ctx.get("base_fps") or 0), bool(ctx.get("dock")), above)
        if context != self.context:
            keep = (self.context is not None and self.context[:3] == context[:3] and self.point
                    and self.context[3] != "battery")
            old_point = self.point
            self.reset()
            self.context = context
            if keep and fg == "auto":
                self.point = old_point  # an objective change keeps the measured point
        if fg == "off":
            self.phase, self.point = "off", 0
            return {**self._request(0, 0, 0, False), **self.snapshot(refresh)}
        if fg in ("x2", "x3"):
            self.phase, self.point = "fixed", div_up(refresh, int(fg[1]))
            req = self._request(int(fg[1]), 0, 0, False)
            self._guard_fixed(ctx)
            return {**req, **self.snapshot(refresh)}
        if fg in ("x3.75", "custom"):
            cap = x375_cap(refresh) if fg == "x3.75" else max(20 if above else x3_cap(refresh), min(refresh, int(ctx.get("base_fps") or 45)))
            self.phase, self.point = "fixed", cap
            self._guard_fixed(ctx)
            return {**self._request(1, cap, 0, False), **self.snapshot(refresh)}
        return {**self._auto(ctx, steps, objective), **self.snapshot(refresh)}

    def _guard_fixed(self, ctx: Dict[str, Any]) -> None:
        verdict = self._window(ctx, self.point, GUARD_S)
        if verdict is not None:
            self.holds = verdict["ok"] or not verdict["at_ceiling"]
            self.reason = "" if self.holds else "the game does not hold this point even at full power"

    def _window(self, ctx: Dict[str, Any], cap: int, length: float) -> Optional[Dict[str, Any]]:
        """Collect samples; a verdict when the window is full. Samples taken while the engine
        does not run this policy yet are not counted."""
        now = self.clock()
        # "settling": Power Split is trying a CPU clock level; its own guard lifts the cap on a
        # dip, so those seconds say nothing about the point.
        if not ctx.get("game") or not ctx.get("confirmed") or ctx.get("real") is None or ctx.get("settling"):
            self.window, self.window_start = [], now
            return None
        self.window.append({"real": float(ctx["real"]), "pace": ctx.get("pace"),
                            "ceiling": bool(ctx.get("at_ceiling", True)),
                            "gpu": ctx.get("gpu_busy"), "cpu": ctx.get("cpu_top")})
        if now - self.window_start < length:
            return None
        self.window_done = self.window
        reals = [s["real"] for s in self.window]
        paces = [s["pace"] for s in self.window if s["pace"] is not None]
        real = statistics.fmean(reals)
        pace = statistics.fmean(paces) if paces else None
        self.window, self.window_start = [], now
        auto = ctx.get("objective") == "autopilot"
        hold = AUTOPILOT_HOLD_RATIO if auto else HOLD_RATIO
        # Autopilot judges the median: a short sharp drop does not move the point.
        ok = ((statistics.median(reals) if auto else real) >= hold * cap
              and (pace is None or pace >= (AUTOPILOT_PACE_MIN if auto else PACE_MIN)))
        gpus = [x["gpu"] for x in self.window_done if isinstance(x.get("gpu"), (int, float))]
        cpus = [x["cpu"] for x in self.window_done if isinstance(x.get("cpu"), (int, float))]
        return {"ok": ok, "real": real, "median": statistics.median(reals), "pace": pace,
                "at_ceiling": all(x["ceiling"] for x in self.window_done),
                "gpu": statistics.median(gpus) if len(gpus) * 2 >= len(self.window_done) else None,
                "cpu": statistics.median(cpus) if len(cpus) * 2 >= len(self.window_done) else None}

    def _auto(self, ctx: Dict[str, Any], steps: List[int], objective: str) -> Dict[str, Any]:
        now = self.clock()
        floor = self._battery_point(ctx, steps)
        # Auto retains the 3.2.2 low-battery point. The existing explicit Smoothness
        # preference keeps following the game instead; fixed choices bypass _auto.
        low = objective == "battery" or bool(ctx.get("autopilot") and ctx.get("low_battery")
                                             and ctx.get("preference", "auto") != "smoothness")
        top = floor if low else steps[-1]
        light = ctx.get("game_class") == "light" and not self.light_failed
        bottom = max(steps[0], lowest_point(int(ctx.get("refresh") or 90), objective, light, bool(ctx.get("dock"))))
        floor_cap = steps[0]
        steps = [c for c in steps if c >= bottom]
        if not ctx.get("game"):
            self.phase = "idle"
            return self._request(1, 0, steps[0], False)
        known = ctx.get("known_point") or (ctx.get("stable_point") if ctx.get("autopilot") else None)
        known = int(known) if isinstance(known, (int, float)) else None
        if self.phase in ("idle",):
            if objective == "battery":
                self._go("locked", "Battery: a low point, generation fills the refresh", floor)
            elif self.point:
                self._go("locked", "kept the measured point", max(min(self.point, top), bottom))
            elif known in steps and known <= top:
                # The point that held last time (per game, user 3.1.0): no full-power measurement
                # (the guard and the headroom climb still correct it if the game changed).
                self._go("locked", "the point from last time", known)
                self.promote_wait = KNOWN_RECHECK_S   # it may have been a heavier scene: try up soon
            elif light:
                self._go("locked", "light game: x2 at once, no full-power measurement", steps[-1])
            else:
                self.caps.clear()
                self._go("measure", "measuring the game at full power")
        if self.phase == "measure":
            return self._measure(ctx, steps, top)
        if self.phase == "trial":
            return self._trial(ctx, steps)
        # locked
        if self.point > top:
            self._go("locked", "objective lowered the point", top)
        elif self.point < bottom:
            self._go("locked", "objective raised the point to its lowest", bottom)
        if self.promoted_at is not None and now - self.promoted_at >= PROBATION_S:
            # The promoted point held its probation: its failed tries are forgotten.
            self.tries.pop(self.point, None)
            self.promoted_at = None
        verdict = self._window(ctx, self.point, GUARD_S)
        if verdict is not None:
            if verdict["ok"]:
                self.holds = True
                if self.clean_since is None:
                    self.clean_since = now
                nxt = [c for c in steps if c > self.point]
                if (nxt and objective != "battery" and self._may_climb(ctx, nxt[0])
                        and self._headroom(verdict, self.point, nxt[0])):
                    self.promote_wait = min(self.promote_wait, max(HEADROOM_WAIT_S, self.headroom_backoff))
            else:
                self.clean_since = None
                if verdict["at_ceiling"]:
                    # Short at full power the whole window: the game got heavier.
                    lower = [c for c in steps if c < self.point]
                    fit = [c for c in lower if TRIAL_RATIO * c <= verdict["median"]]
                    if lower:
                        new = fit[-1] if fit else lower[0]
                        self.demotions += 1
                        # A point that keeps falling at full power is tried again later each time
                        # (Deck report 3.1.2: x2 <-> x2.4 every 5 min in one game): 2, 4, 8, 16 min.
                        # The GPU headroom check still climbs early when the scene is clearly lighter.
                        falls = self.falls[self.point] = self.falls.get(self.point, 0) + 1
                        self.promote_wait = min(PROMOTE_WAIT_MAX_S, PROMOTE_WAIT_S * 2 ** (falls - 1))
                        if self.promoted_at is not None and now - self.promoted_at < PROBATION_S:
                            self._failed_climb(self.point, now)
                        self.promoted_at = None
                        self._go("locked", f"game fell to {verdict['median']:.0f} fps at full power: "
                                 f"point {self.point} -> {new}", new)
                    elif light:
                        self.light_failed = True
                        self.reason = "not a light game: x2 does not hold even at full power"
                    else:
                        self.holds = False
                        self.reason = ("the game does not hold the lowest point even at full power"
                                       if bottom == floor_cap else
                                       f"the game does not hold {bottom} (this mode's lowest point) even at full power")
        compass = ctx.get("bench_point")
        if isinstance(compass, (int, float)) and self.point < compass and compass in steps:
            # Below the benchmark's point (a heavier scene): back up there soon once it is calm,
            # later after each try that did not hold.
            self.promote_wait = min(self.promote_wait, max(KNOWN_RECHECK_S, self.headroom_backoff))
        if self._over_power_target(ctx, steps, now):
            return self._request(1, self.point, 0, False)
        if (objective != "battery" and self.clean_since is not None and self.point < top
                and now - self.clean_since >= self.promote_wait and ctx.get("at_ceiling_possible", True)
                and now >= self.retry_at.get([c for c in steps if c > self.point][0], 0.0)
                and self._may_climb(ctx, [c for c in steps if c > self.point][0])):
            nxt = [c for c in steps if c > self.point][0]
            self.trial_cap, self.trial_from = nxt, self.point
            self._go("trial", f"trying point {nxt}")
            return self._request(1, self.trial_from, 0, True)  # raise the TDP first
        return self._request(1, self.point, 0, False)

    @staticmethod
    def _battery_point(ctx: Dict[str, Any], steps: List[int]) -> int:
        """Battery's point. x3.75 shows the most artefacts: Autopilot keeps x3 (30 at 90 Hz) and
        goes to x3.75 only on a low battery; the Battery mode chosen by hand keeps x3.75."""
        full = ladder(int(ctx.get("refresh") or 90))
        point = full[1] if ctx.get("autopilot") and not ctx.get("low_battery") and len(full) > 1 else full[0]
        return max(point, steps[0])

    def adopt(self, point: int, reason: str) -> None:
        """A point chosen outside the pilot (an accepted benchmark): locked now, guarded as usual."""
        self.caps.clear()
        self.clean_since = None
        self._go("locked", reason, int(point))
        self.promote_wait = KNOWN_RECHECK_S

    def _may_climb(self, ctx: Dict[str, Any], nxt: int) -> bool:
        """Autopilot climbs while the TDP is below the whole 15 W (12 W on a low battery) and never
        into a point that cost too much."""
        if ctx.get("warm"):
            return False   # 80 C and up, every mode (Deck health, user 3.1.0): no higher point
        if ctx.get("dock") or not ctx.get("autopilot"):
            # Dock (user, 3.1.0): a high TDP is fine on the TV; only the temperature holds a climb.
            return True
        if self.costly.get(nxt, 0.0) > self.clock():
            return False
        tdp = ctx.get("tdp_w")
        limit = POWER_GOOD_W if ctx.get("low_battery") else BAND_MAX_W
        return not isinstance(tdp, (int, float)) or tdp <= limit

    def _over_power_target(self, ctx: Dict[str, Any], steps: List[int], now: float) -> bool:
        """Autopilot: x3 that the settled Governor holds only near the whole 15 W for a minute steps
        down to x3.75. Above x3 the multiplier follows the game (3.3.0): a point the game holds
        within the 15 W ceiling is kept, and the guard steps down one it does not hold."""
        tdp = ctx.get("tdp_w")
        lower = [c for c in steps if c < self.point]
        limit = BAND_MAX_W
        if (not ctx.get("autopilot") or ctx.get("dock") or not ctx.get("gov_settled") or not isinstance(tdp, (int, float))
                or tdp <= limit or not lower or self.point > x3_cap(int(ctx.get("refresh") or 90))):
            self.costly_since = None
            return False
        if self.costly_since is None:
            self.costly_since = now
        if now - self.costly_since < POWER_WAIT_S:
            return False
        self.costly[self.point] = now + COSTLY_HOLD_S
        new = lower[-1]
        self.costly_since = None
        self.demotions += 1
        self.clean_since = None
        self._go("locked", f"Autopilot power target: point {self.point} needs {tdp:.0f} W, "
                 f"over {limit:.0f} W -> {new}", new)
        return True

    @staticmethod
    def _headroom(verdict: Dict[str, Any], point: int, nxt: int) -> bool:
        """The game has room for the next point: GPU load scaled to it stays under the limit,
        and the busiest CPU core is not near its own."""
        gpu, cpu = verdict.get("gpu"), verdict.get("cpu")
        if gpu is None or gpu <= 0:
            return False
        if cpu is not None and cpu * nxt / point >= HEADROOM_CPU_PCT:
            return False
        return gpu * nxt / point < HEADROOM_GPU_PCT

    def _measure(self, ctx: Dict[str, Any], steps: List[int], top: int) -> Dict[str, Any]:
        now = self.clock()
        st = ctx.get("engine") or {}
        sample_id = st.get("time_ns", now)
        real = ctx.get("real")
        valid = bool(ctx.get("confirmed") and st.get("auto") and ctx.get("game")
                     and not st.get("steam_ui") and not ctx.get("settling")
                     and isinstance(real, (int, float)) and math.isfinite(real))
        if valid and sample_id != self._measure_sample_id:
            # Only a new engine status counts: a repeated one is not a second reading.
            self._measure_sample_id = sample_id
            cap = int(st.get("cap_fps") or 0)
            if cap:
                self.caps.append((now, cap))
        if self.caps and now - self.caps[-1][0] > MEASURE_GAP_S:
            # No fresh confirmed reading: silence is not stability, the measurement restarts.
            self.caps.clear()
        elapsed = now - self.since
        if self.caps:
            last = self.caps[-1][1]
            stable_from = now
            for t, c in reversed(self.caps):
                if c != last:
                    break
                stable_from = t
            choice = None
            if elapsed >= MEASURE_MIN_S and now - stable_from >= STABLE_S:
                choice = last
            elif elapsed >= MEASURE_MAX_S:
                choice = int(statistics.median(c for _, c in self.caps))
            if choice:
                fit = [c for c in steps if c <= choice] or [steps[0]]
                point = min(fit[-1], top)
                self.measured = choice
                self._go("locked", f"measured: the game holds {choice} fps; locked {point}", point)
                return self._request(1, point, 0, False)
        elif elapsed >= MEASURE_MAX_S:
            x3 = div_up(int(ctx.get("refresh") or 90), 3)
            self._go("locked", "no measurement (no game frames): x3 or this mode's lowest point",
                     next((c for c in steps if c >= x3), steps[-1]))
            return self._request(1, self.point, 0, False)
        return self._request(1, 0, steps[0], True)

    def _trial(self, ctx: Dict[str, Any], steps: List[int]) -> Dict[str, Any]:
        if not ctx.get("at_ceiling", True):
            # Waiting for the Governor to bring the TDP to the ceiling.
            self.window, self.window_start = [], self.clock()
            return self._request(1, self.trial_from, 0, True)
        verdict = self._window(ctx, self.trial_cap, TRIAL_S)
        if verdict is None:
            # Clearly short after a few seconds: no need to show the whole trial.
            reals = [x["real"] for x in self.window]
            if (self.clock() - self.window_start >= TRIAL_EARLY_S and len(reals) >= 3
                    and statistics.median(reals) < TRIAL_EARLY_RATIO * self.trial_cap):
                verdict = {"ok": False, "real": statistics.fmean(reals)}
                self.window, self.window_start = [], self.clock()
            else:
                return self._request(1, self.trial_cap, 0, True)
        if verdict["ok"] and verdict["real"] >= TRIAL_RATIO * self.trial_cap:
            self.promotions += 1
            self.promote_wait = 30.0  # keep climbing soon while it holds
            self.headroom_backoff = 0.0
            self._go("locked", f"point {self.trial_cap} holds: locked", self.trial_cap)
            self.clean_since = self.clock()
            self.promoted_at = self.clock()
        else:
            self.promote_wait = min(PROMOTE_WAIT_MAX_S, self.promote_wait * 2)
            self.headroom_backoff = min(480.0, max(60.0, self.headroom_backoff * 2))
            wait = self._failed_climb(self.trial_cap, self.clock())
            self._go("locked", f"point {self.trial_cap} did not hold; next try in {wait:.0f} s",
                     self.trial_from)
        return self._request(1, self.point, 0, False)

    def _failed_climb(self, point: int, now: float) -> float:
        """A climb into point did not hold: it waits 1, 2, 4, 8, 16 min before the next try,
        whatever path (headroom, benchmark) would climb sooner."""
        n = self.tries[point] = self.tries.get(point, 0) + 1
        wait = min(RETRY_MAX_S, RETRY_BASE_S * 2 ** (n - 1))
        self.retry_at[point] = now + wait
        return wait

    def snapshot(self, refresh: int = 90) -> Dict[str, Any]:
        return {"phase": self.phase, "point": self.point, "label": mult_label(refresh, self.point),
                "reason": self.reason, "holds": self.holds, "demotions": self.demotions,
                "promotions": self.promotions, "trial_cap": self.trial_cap if self.phase == "trial" else 0}
