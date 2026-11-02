"""GFG Extreme 3.0 service: one tick a second that ties everything together.

  engine status -> rates -> profile -> objective (Autopilot) -> point (pilot) -> policy
  (Coordinator, confirmed by the engine) -> TDP (Governor) -> HUD -> telemetry

Everything that touches the Deck is injected (engine control, TDP actuator, sensors,
battery), so the sandbox runs this same code against a virtual Deck.
"""
from __future__ import annotations

import time
import threading
from typing import Any, Callable, Dict, Optional

from . import filters
from .autopilot import Autopilot
from .governor import PowerGovernor
from .filter_budget import FilterBudget, shed
from .game_book import GameBook, START_W as CLASS_START_W
from .governor_device import external_display_connected
from .lease import ExperimentLease
from .kedavra import hud as hudmod
from .benchmark import Benchmark
from .pilot import PointPilot, ladder
from .policy import Policy, PolicyCoordinator
from .session_stats import SessionStats
from .split_control import SplitControl
from .boost import Boost
from .gpu_clock_control import GpuClockControl

GAME_GRACE_S = 10.0   # the QAM or a loading screen does not end a game session
STEAM_APP = 769
HDR_RESEND_S = 10.0
DOCK_CHECK_S = 5.0    # an external display (the dock) is looked up this often
# No game (user, 3.1.1): after a game GFG resets everything and computes nothing. An idle tick
# only reads the engine status (to see the next game) and keeps the idle policy confirmed; the
# sensors shown in the panel are read this often, the full pipeline runs again only for a
# settings change.
IDLE_SENSORS_S = 5.0


class Service:
    def __init__(self, *, settings: Any, ctl: Any, actuator: Any, model: Any, sensors: Any,
                 battery: Callable[[], Dict[str, Any]], hud: Optional[Any] = None,
                 hud_path: str = "", clock: Callable[[], float] = time.monotonic,
                 journal: Optional[Callable[..., None]] = None, telemetry: Optional[Any] = None,
                 split: Optional[SplitControl] = None, gclock: Optional[GpuClockControl] = None,
                 book: Optional[GameBook] = None, display: Optional[Callable[[], bool]] = None,
                 boost: Optional[Boost] = None) -> None:
        self._lifecycle_lock = threading.RLock()
        self._stopped = False
        self._stop_complete = False
        self.settings = settings
        self.ctl = ctl
        self.clock = clock
        self.journal = journal
        self.telemetry = telemetry
        self.sensors = sensors
        self.battery = battery
        self.hud = hud
        self.hud_path = hud_path
        self.coord = PolicyCoordinator(ctl, clock=clock, journal=self._j("policy"))
        self.pilot = PointPilot(clock=clock, journal=self._j("pilot"))
        self.bench = Benchmark(clock=clock)
        self.autopilot = Autopilot(clock=clock)
        self.governor = PowerGovernor(actuator, model, clock=clock, journal=self._j("governor"))
        self.split = split
        self.gclock = gclock
        self.lease = ExperimentLease(clock=clock, journal=self._j("lease"))
        self.fbudget = FilterBudget(clock=clock, journal=self._j("filters"))
        self.rates = hudmod.Rates()
        self.stats = SessionStats()
        self.last_game_at = -1e9
        self.app = 0
        self.last_policy: Optional[Policy] = None
        self.snapshot: Dict[str, Any] = {}
        self.ticks = 0
        self.hdr_off_sent = -1e9   # HDR output switched off: when hdr_enabled 0 was last sent
        self.hdr_error = ""        # the last HDR switch failure, journalled once
        self.book = book if book is not None else GameBook(None)
        self.display = display if display is not None else external_display_connected
        self.dock = False
        self.dock_at = -1e9
        self.cur = (0, 90, False)          # (app, refresh, dock) of the running session, for the book
        self.noted_point: Optional[tuple] = None
        self.light_dropped = False
        self.boost = boost
        if boost is not None:
            boost.coord = self.coord
        self.last_req: Optional[Dict[str, Any]] = None
        self.idle = False          # reset after the last game and idling: the light tick runs
        self.idle_rev: Any = None  # settings revision the idle snapshot was made with
        self.idle_sensors_at = -1e9

    def bench_start(self) -> Dict[str, Any]:
        return self.bench.start(self.app, bool(self.app))

    def bench_answer(self, accept: bool) -> Dict[str, Any]:
        res = self.bench.answer(accept)
        if res:
            self.book.note_benchmark(res["app"], res["refresh"], res["dock"], res["point"], res["medians"])
            if self.app == res["app"]:
                self.pilot.adopt(res["point"], "benchmark: the game's medians")
        return {"ok": True, "accepted": bool(res)}

    def _stable_point(self, refresh: int) -> Optional[int]:
        """The point the Governor locked as stable for this game last time (None: unknown)."""
        model = self.governor.model
        try:
            entry = model.get(f"app:{self.app}|{refresh}|stable") if model else None
            return int(entry["point"]) if entry and entry.get("point") is not None else None
        except (TypeError, ValueError, KeyError):
            return None

    def _hdr_output(self, on: bool, fresh: bool, now: float) -> None:
        """HDR output off: Gamescope's hdr_enabled 0, sent again every 10 s because Steam sets it
        back from its own setting; switched on again, it goes back to 1 once."""
        if not fresh:
            return
        try:
            # The time is taken before the call: a failing gamescopectl is retried every 10 s,
            # not every tick (3.0.10 journalled 5000 failures in an hour).
            if not on and now - self.hdr_off_sent >= HDR_RESEND_S:
                self.hdr_off_sent = now
                self.ctl.set_var("hdr_enabled", 0)
                self.hdr_error = ""
            elif on and self.hdr_off_sent > -1e9:
                self.hdr_off_sent = -1e9
                self.ctl.set_var("hdr_enabled", 1)
                self.hdr_error = ""
        except Exception as exc:
            if self.journal and str(exc) != self.hdr_error:
                self.journal("service", "hdr-output-failed", error=str(exc))
            self.hdr_error = str(exc)

    def _j(self, source: str) -> Optional[Callable[..., None]]:
        if not self.journal:
            return None
        return lambda kind, **f: self.journal(source, kind, **f)

    # ------------------------------------------------------------------ tick
    def tick(self) -> Dict[str, Any]:
        with self._lifecycle_lock:
            if self._stopped:
                return self.snapshot
            return self._tick()

    def _tick(self) -> Dict[str, Any]:
        now = self.clock()
        self.ticks += 1
        st = self.ctl.status() or {}
        fresh = bool(st.get("fresh"))
        rates = self.rates.update(st) if fresh else None
        if not fresh:
            self.rates.prev = None
        live = hudmod.live_for(st if fresh else {}, rates)
        app = int(st.get("app_id") or 0) if fresh else 0
        if app and app != STEAM_APP:
            if app != self.app:
                self._end_session("game-change")
                self.app = app
                self.stats.start(app, now)
            self.last_game_at = now
        game = bool(self.app) and now - self.last_game_at <= GAME_GRACE_S
        if not game and self.app:
            self._end_session("game-exit")
            self.app = 0
            self._reset_after_game()
        if game:
            self.idle = False
        elif self.idle and self.idle_rev == getattr(self.settings, "rev", 0):
            return self._idle_tick(now, st, fresh, live)

        data = self.settings.data
        self._hdr_output(bool(data["engine"].get("hdr_output", True)), fresh, now)
        profile_key, profile = self.settings.profile_for(self.app)
        sensors = self.sensors.sample() if self.sensors else {}
        power = self.battery() if self.battery else {}
        temp = sensors.get("temp_c")
        objective = profile["objective"]
        auto_reason = ""
        if objective == "autopilot":
            # Autopilot is its own adaptive mode (it does not switch between Battery, Balanced
            # and Quality); this only tracks the battery and the reason shown in the panel.
            ap = self.autopilot.decide(data["autopilot"]["preference"], power, temp)
            auto_reason = ap["reason"]
        refresh = round(1e9 / st["refresh_ns"]) if fresh and st.get("refresh_ns") else 90
        if now - self.dock_at >= DOCK_CHECK_S:
            self.dock_at = now
            try:
                self.dock = bool(self.display())
            except OSError:
                pass
        dock = self.dock
        gp = self.book.profile(self.app, refresh, dock) if game else {"class": None, "source": None}
        if game:
            self.cur = (self.app, refresh, dock)

        confirmed = self.coord.engine_runs(self.last_policy, st)
        gov = self.governor
        # A camera-turn boost (3.1.1) and the 2 s after it are not the point's readings: the pilot
        # and the Governor keep what they had.
        boosting = bool(game and self.boost is not None and self.boost.busy(now) and self.last_req is not None)
        benching = bool(game and self.bench.measuring and self.last_req is not None)
        req = self.last_req if boosting or benching else self.pilot.tick({
            "fg": profile["fg"], "base_fps": profile["base_fps"], "objective": objective,
            "refresh": refresh, "game": game, "app": self.app, "engine": st,
            "real": live.get("real"), "pace": live.get("pace"), "confirmed": confirmed,
            "gpu_busy": sensors.get("gpu_busy_pct"), "cpu_top": sensors.get("cpu_top_core_pct"),
            "autopilot": objective == "autopilot", "stable_point": self._stable_point(refresh) if game else None,
            "known_point": gp.get("point"), "bench_point": gp.get("bench_point"), "game_class": gp.get("class"), "dock": dock,
            "low_battery": self.autopilot.low_battery or data["autopilot"]["preference"] == "battery",
            "preference": data["autopilot"]["preference"],
            "above_x3": data["engine"].get("above_x3", False),
            # Only a TDP the Governor manages is a power reading for the pilot: paused (the player's own
            # TDP) Autopilot must still climb when the game gets lighter (Deck report 3.0.7).
            "tdp_w": gov.tdp if gov.owned and not gov.paused else None, "gov_settled": gov.settled(), "warm": gov.warm,
            "at_ceiling": gov.at_ceiling(), "settling": bool((self.split and self.split.settling()) or (self.gclock and self.gclock.settling())),
        })
        self.last_req = req if game else None
        if benching:
            # Benchmark: the game's own frames at the 15 W ceiling, generation off; the pilot keeps
            # its point for after the run.
            req = {**req, "multiplier": 0, "base_fps": 0, "auto_floor": 0, "hold_ceiling": True, "phase": "bench"}
        if game and self.pilot.measured:
            self.book.note_measure(self.app, refresh, dock, self.pilot.measured, ladder(refresh))
        self.pilot.measured = 0
        if game and self.pilot.light_failed and gp.get("class") == "light" and not self.light_dropped:
            self.book.not_light(self.app, refresh, dock)
            self.light_dropped = True
        # Real frames and watts come before the look (user, 3.0.9): a GPU that cannot carry the
        # game above x3 sheds the filters, light first, then off.
        steps = ladder(refresh)
        above_x3 = steps[2] if len(steps) > 2 else steps[-1]
        budget = self.fbudget.tick(
            app=self.app, active=bool(game and req["multiplier"] and objective != "quality"
                                      and filters.is_active(profile["filters"])),
            gpu_busy=sensors.get("gpu_busy_pct"),
            starved=bool(req["phase"] in ("locked", "fixed") and (0 < req["point"] < above_x3 or not self.pilot.holds)))
        policy = Policy(req["multiplier"], req["base_fps"], filters.to_post(shed(profile["filters"], budget)),
                        self.hud_path, req["auto_floor"], bool(data["engine"]["hdr"]))
        self.coord.want(policy)
        self.last_policy = policy
        if self.boost is not None:
            eng = data["engine"]
            self.boost.configure(
                enabled=bool(game and eng.get("boost") and req["phase"] in ("locked", "fixed")),
                mult=eng.get("boost_mult", 1.5), refresh=refresh, wanted=policy,
                headroom=Boost.estimate_headroom(refresh, live.get("real"), sensors.get("gpu_busy_pct"),
                                                 st.get("gpu_us_avg"), st.get("gpu_analysis_us_avg")))
        coord = self.coord.tick(st)

        gov.enabled = bool(data["power"]["enabled"])
        # `confirmed` above refers to last tick's policy, used by the pilot to
        # evaluate its observations. It cannot acknowledge a newly selected point.
        locked = req["phase"] in ("locked", "fixed") and self.coord.engine_runs(policy, st)
        split_status: Dict[str, Any] = {}
        if self.split is not None:
            gs = gov.status()
            split_status = self.split.tick({
                "enabled": bool(data["power"]["split"]), "game": game, "app": f"app:{self.app}",
                "steady": bool(locked and not boosting and gs.get("owned") and gov.settled() and not (self.gclock and self.gclock.owned)),
                "menu": bool(st.get("steam_ui")), "point": req["point"], "real": live.get("real"),
                "sensors": sensors, "draw_w": gs.get("draw_w"), "tdp_w": gs.get("tdp_w")})
        gclock_status: Dict[str, Any] = {}
        if self.gclock is not None:
            gs = gov.status()
            gclock_status = self.gclock.tick({
                "enabled": bool(data["power"]["gpu_clock"]), "game": game, "app": f"app:{self.app}",
                "steady": bool(locked and not boosting and gs.get("owned") and gov.settled()),
                "other_busy": bool(self.split and (self.split.cpu.owned or self.split.settling())),
                "menu": bool(st.get("steam_ui")), "point": req["point"], "real": live.get("real"),
                "sensors": sensors, "draw_w": gs.get("draw_w")})
        sample = {
            "game": game, "key": f"app:{self.app}|{refresh}", "objective": objective,
            "cap": req["point"] if locked else 0, "hold_ceiling": req["hold_ceiling"],
            # A CPU clock change (Power Split) restarts the TDP windows like a new policy does.
            "epoch": (coord["epoch"], self.split.changes if self.split else 0, self.gclock.changes if self.gclock else 0),
            "real": live.get("real"), "pace": live.get("pace"),
            "probe_active": bool((self.split and self.split.settling()) or (self.gclock and self.gclock.settling())),
            "over_budget": (rates or {}).get("over_budget", 0.0), "ring_miss": 0.0, "temp_c": temp,
            "draw_w": gov.status().get("draw_w"),
            "ac": power.get("external_power"),
            "experiment": bool((self.split and self.split.settling()) or (self.gclock and self.gclock.settling())),
            "dock": dock, "start_w": CLASS_START_W.get(gp.get("class") or ""),
        }
        # The counters are read every fresh tick: after a stale engine status (a hitch, the QAM) the
        # next delta is that one second, not everything the engine counted while it was stale.
        ring_miss, over_budget = (self._delta(st, "ring_miss"), self._delta(st, "over_budget")) if fresh else (0.0, 0.0)
        if rates is not None:
            sample["ring_miss"], sample["over_budget"] = ring_miss, over_budget
        g = gov.status() if boosting else gov.tick(sample)
        self.bench.tick({"app": self.app, "game": game, "steam_ui": bool(st.get("steam_ui")), "real": live.get("real"),
                         "gpu": sensors.get("gpu_busy_pct"), "cpu": sensors.get("cpu_top_core_pct"),
                         "draw_w": g.get("draw_w"), "temp_c": temp, "at_ceiling": gov.at_ceiling(), "refresh": refresh,
                         "dock": dock, "above_x3": data["engine"].get("above_x3", False)})
        # A point forced by the Battery mode or a low battery is not what the game holds: it is
        # not the next launch's starting point (3.3.0).
        if (game and g.get("stable") and req["point"] and objective != "battery"
                and not self.autopilot.low_battery and not benching):
            mark = (self.app, refresh, dock, req["point"], g.get("tdp_w"))
            if mark != self.noted_point:
                self.noted_point = mark
                self.book.note_point(self.app, refresh, dock, req["point"], g.get("tdp_w"), ladder(refresh)[-1])
        lease = self.lease.observe({"tdp": bool(g.get("probing")) and game,
                                    "cpu": bool(self.split and self.split.settling()),
                                    "gpu": bool(self.gclock and self.gclock.settling())})

        hours = self._hours_left(power)
        hud_cfg = data["hud"]
        if self.hud is not None:
            # Not managed (paused, or Governor off): the HUD still shows the TDP the Deck runs at.
            extra = {"tdp_w": g.get("tdp_w") if g.get("owned") else g.get("observed_tdp_w"), "draw_w": g.get("draw_w"),
                     "ceiling_w": g.get("ceiling_w"), "temp_c": temp, "managing": g.get("managing"),
                     "battery": {**power, "hours_left": hours}}
            try:
                panel = self.bench.panel() if game else None
                if panel:
                    self.hud.message(st if fresh else None, panel, self.bench.view())
                else:
                    self.hud.tick(bool(hud_cfg["enabled"]) and game, st if fresh else None, extra,
                                  hud_cfg["position"], hud_cfg["detail"])
            except OSError:
                pass
        if game:
            self.stats.add(now, output=live.get("fps"), real=live.get("real"), tdp=g.get("tdp_w"),
                           draw=g.get("draw_w"), reference_w=g.get("ceiling_w"), temp_c=temp,
                           stuttering=(live.get("pace") is not None and live["pace"] < 0.97), hot=(temp or 0) >= 85,
                           mode=objective)
        self.snapshot = {
            "time": now, "game": game, "app": self.app, "profile": profile_key, "profile_name": profile["name"],
            "fg": profile["fg"], "objective": objective, "objective_setting": profile["objective"],
            "autopilot_reason": auto_reason, "refresh": refresh, "engine": self._engine_view(st),
            "live": live, "pilot": req, "policy": coord, "governor": g, "split": split_status, "gpu_clock": gclock_status,
            "lease": lease, "filter_budget": self.fbudget.status(), "game_profile": gp, "dock": dock,
            "boost": self.boost.status() if self.boost is not None else None,
            "sensors": sensors, "power": power,
            "filters": filters.summary(profile["filters"]),
            "bench": self.bench.view(),
        }
        if self.telemetry is not None and game:
            self.telemetry.sample({"t": round(time.time(), 1), "app": self.app, "fps": live.get("fps"),
                                   "real": live.get("real"), "pace": live.get("pace"), "point": req["point"],
                                   "phase": req["phase"], "objective": objective, "tdp": g.get("tdp_w"),
                                   "draw": g.get("draw_w"), "temp": temp, "policy": coord["state"],
                                   "mult": st.get("multiplier"), "cap": st.get("cap_fps"), "hz": refresh,
                                   "ceiling": g.get("ceiling_w"), "cpu": sensors.get("cpu_top_core_pct"),
                                   "gpu": sensors.get("gpu_busy_pct"), "gpu_mhz": sensors.get("gpu_clock_mhz"),
                                   "fan": sensors.get("fan_rpm"), "bat": power.get("percent"),
                                   "ac": power.get("external_power"), "hours": None if hours is None else round(hours, 2),
                                   "cpu_cap_khz": split_status.get("cap_khz"), "gpu_limit_mhz": gclock_status.get("limit_mhz")})
            self.telemetry.engine(st)
        if not game and self._idle_ready():
            self.idle, self.idle_rev = True, getattr(self.settings, "rev", 0)
            self.idle_sensors_at = now
        return self.snapshot

    def _reset_after_game(self) -> None:
        """The game is gone: nothing of it is kept in memory (the book and the model have it)."""
        self.pilot.reset()
        self.pilot.context = None
        self.autopilot.reset()
        self.fbudget.tick(app=None, active=False, gpu_busy=None, starved=False)
        self.rates = hudmod.Rates()
        self._prev_counters = {}
        self._drain = None
        self.noted_point = None
        self.cur = (0, 90, self.dock)

    def _idle_ready(self) -> bool:
        """Everything handed back to the player: TDP, CPU and GPU clocks."""
        g = self.governor
        return (not g.owned and not g.restore_pending and not (self.split and (self.split.cpu.owned or getattr(self.split.cpu, "restore_pending", False)))
                and not (self.gclock and self.gclock.owned))

    def _idle_tick(self, now: float, st: Dict[str, Any], fresh: bool, live: Dict[str, Any]) -> Dict[str, Any]:
        self._hdr_output(bool(self.settings.data["engine"].get("hdr_output", True)), fresh, now)
        if self.last_policy is not None:
            self.coord.want(self.last_policy)
        coord = self.coord.tick(st)
        snap = dict(self.snapshot)
        snap.update({"time": now, "engine": self._engine_view(st), "live": live, "policy": coord, "idle": True})
        if now - self.idle_sensors_at >= IDLE_SENSORS_S:
            self.idle_sensors_at = now
            snap["sensors"] = self.sensors.sample() if self.sensors else {}
            snap["power"] = self.battery() if self.battery else {}
        self.snapshot = snap
        return snap

    def _hours_left(self, power: Dict[str, Any]) -> Optional[float]:
        """Hours to empty from a smoothed discharge power (None on AC or without readings)."""
        e, w = power.get("energy_uwh"), power.get("power_uw")
        if not power.get("discharging") or not isinstance(e, (int, float)) or not isinstance(w, (int, float)) or w <= 0:
            self._drain = None
            return None
        prev = getattr(self, "_drain", None)
        self._drain = w if prev is None else prev + 0.08 * (w - prev)
        return e / self._drain

    def _delta(self, st: Dict[str, Any], key: str) -> float:
        prev = getattr(self, "_prev_counters", {})
        value = st.get(key)
        out = 0.0
        if isinstance(value, (int, float)) and isinstance(prev.get(key), (int, float)) and value >= prev[key]:
            out = float(value - prev[key])
        prev[key] = value
        self._prev_counters = prev
        return out

    @staticmethod
    def _engine_view(st: Dict[str, Any]) -> Dict[str, Any]:
        keys = ("engine", "fresh", "state", "multiplier", "cap_fps", "base_fps", "auto", "auto_floor",
                "post", "post_active", "post_error", "gpu_us_avg", "gpu_post_us_avg", "gpu_analysis_us_avg", "analysis_pairs", "last_problem",
                "steam_ui", "width", "height", "message", "trace", "transfer", "hdr_enabled", "post_hdr_limited")
        return {k: st.get(k) for k in keys if k in st}

    def _end_session(self, reason: str) -> None:
        summary = self.stats.finish()
        if summary and self.cur[0] == self.app:
            self.book.end_session(*self.cur, summary)
        self.light_dropped = False
        if summary and self.telemetry is not None:
            self.telemetry.session({"app": self.app, "reason": reason, "end": round(time.time()), **summary})
        if summary and self.journal:
            self.journal("service", "session-summary", app=self.app, **summary)

    def stop(self) -> None:
        with self._lifecycle_lock:
            if self._stop_complete:
                return
            self._stopped = True
            self._stop()
            self._stop_complete = True

    def _stop(self) -> None:
        """Plugin unload: TDP back to the player's, HDR output back on, HUD hidden, generation left
        as the engine runs it."""
        actions = [lambda: self._end_session("unload")]
        if self.boost is not None:
            actions.append(self.boost.stop)
        actions.extend([lambda: self._hdr_output(True, True, self.clock()),
                        lambda: self.governor.stop("unload")])
        if self.split is not None:
            actions.append(self.split.stop)
        if self.gclock is not None:
            actions.append(self.gclock.stop)
        if self.hud is not None:
            actions.append(lambda: self.hud.tick(False, None))
        errors = []
        for action in actions:
            try:
                action()
            except Exception as error:
                errors.append(error)
        if errors:
            raise RuntimeError("Service restoration failed: " + "; ".join(map(str, errors))) from errors[0]
