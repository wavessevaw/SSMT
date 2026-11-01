import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from helpers import Clock

from gfg_plugin import autopilot, filters, pilot, settings
from gfg_plugin.governor import PowerGovernor, healthy
from gfg_plugin.policy import Policy, PolicyCoordinator


class FakeCtl:
    def __init__(self):
        self.vars = {}
        self.calls = []
        self.fail = False

    def set_var(self, name, value):
        if self.fail:
            raise RuntimeError("gamescopectl failed")
        self.calls.append((name, str(value)))
        self.vars[name] = str(value)


def engine_status(ctl, t, fresh=True, old=False):
    st = {"engine": "running", "fresh": fresh, "time_ns": int(t * 1e9)}
    if not old:
        st.update({"policy": int(ctl.vars.get("kfg_policy", 0)), "req_multiplier": int(ctl.vars.get("kfg_multiplier", 0)),
                   "req_base_fps": int(ctl.vars.get("kfg_base_fps", 0)),
                   "req_auto_floor": int(ctl.vars.get("kfg_auto_floor", 0)), "post": ctl.vars.get("kfg_post", ""),
                   "hdr_enabled": ctl.vars.get("kfg_hdr", "1") == "1"})
    return st


class FiltersTest(unittest.TestCase):
    def test_post_string_and_presets(self):
        self.assertEqual(filters.to_post(filters.OFF), "")
        self.assertEqual(filters.to_post(filters.preset("vivid")), "vibrance,cas=0.35")
        cfg = filters.normalize({"effects": ["vignette", "bogus"], "sharpen": "dls", "sharpness": 2, "aa": "smaa"})
        self.assertEqual(filters.to_post(cfg), "vignette,dls=1:0.17,smaa")

    def test_legacy_vkbasalt(self):
        cfg = filters.from_legacy({"external_vulkan_layer": "vkbasalt", "vkbasalt_shader": "vibrance:noir",
                                   "vkbasalt_sharpening": "cas", "vkbasalt_sharpness": 0.5, "vkbasalt_antialiasing": "fxaa"})
        self.assertEqual(filters.to_post(cfg), "vibrance,noir,cas=0.5,fxaa")
        self.assertEqual(filters.from_legacy({"external_vulkan_layer": "mangohud"}), filters.OFF)


class SettingsTest(unittest.TestCase):
    def test_normalize_and_profiles(self):
        with tempfile.TemporaryDirectory() as d:
            s = settings.Settings(Path(d) / "s.json")
            self.assertFalse(s.exists)
            s.update_profile("default", {"fg": "x3.75", "base_fps": 500, "objective": "nope"})
            p = s.data["profiles"]["default"]
            self.assertEqual((p["fg"], p["base_fps"], p["objective"]), ("x3.75", 90, "autopilot"))
            key = s.make_game_profile(1245620, "Elden Ring")
            self.assertEqual(key, "app:1245620")
            self.assertEqual(s.profile_for(1245620)[0], "app:1245620")
            self.assertEqual(s.profile_for(7)[0], "default")
            with self.assertRaises(ValueError):
                s.make_game_profile(0)
            again = settings.Settings(Path(d) / "s.json")
            self.assertEqual(again.data["profiles"]["app:1245620"]["name"], "Elden Ring")


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.ctl = FakeCtl()
        self.c = PolicyCoordinator(self.ctl, clock=self.clock)
        self.want = Policy(1, 0, "vibrance", "/dev/shm/h", 24)

    def tick(self, **kw):
        self.clock.t += 1
        return self.c.tick(engine_status(self.ctl, self.clock.t, **kw))

    def test_send_order_and_confirm(self):
        self.c.want(self.want)
        self.assertEqual(self.tick()["state"], "applying")
        names = [n for n, _ in self.ctl.calls]
        self.assertEqual(names, ["kfg_hud", "kfg_hdr", "kfg_post", "kfg_auto_floor", "kfg_base_fps", "kfg_multiplier", "kfg_policy"])
        self.assertEqual(self.tick()["state"], "confirmed")
        sent = len(self.ctl.calls)
        for _ in range(5):
            self.tick()
        self.assertEqual(len(self.ctl.calls), sent, "nothing is re-sent while confirmed")
        # 3.1.2: a new point sends only what changed, the revision last.
        self.c.want(Policy(1, 30, "vibrance", "/dev/shm/h", 24))
        self.assertEqual(self.tick()["state"], "applying")
        self.assertEqual([n for n, _ in self.ctl.calls[sent:]], ["kfg_base_fps", "kfg_policy"])
        self.assertEqual(self.tick()["state"], "confirmed")

    def test_stale_holds_and_old_engine(self):
        self.c.want(self.want)
        self.assertEqual(self.tick(fresh=False)["state"], "hold")
        self.assertEqual(self.ctl.calls, [])
        self.assertEqual(self.tick(old=True)["state"], "outdated")
        self.assertEqual(self.ctl.calls, [])

    def test_errors_back_off(self):
        self.c.want(self.want)
        self.ctl.fail = True
        self.assertEqual(self.tick()["state"], "error")
        self.ctl.fail = False
        self.tick()  # inside the 1 s back-off? (1 s passed: retried)
        self.assertIn(self.c.s.state, ("applying", "error"))

    def test_restart_reapplies_and_rival_conflicts(self):
        self.c.want(self.want)
        self.tick(), self.tick()
        self.ctl.vars = {}  # Gamescope restarted
        self.tick()
        self.assertEqual(self.tick()["state"], "confirmed")
        for i in range(40):  # someone keeps switching to x3
            if i % 2 == 0:
                self.ctl.vars["kfg_multiplier"] = "3"
            self.tick()
            if self.c.s.state == "conflict":
                break
        self.assertEqual(self.c.s.state, "conflict")
        self.c.take_over()
        self.tick()
        self.assertEqual(self.tick()["state"], "confirmed")


class PilotTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.p = pilot.PointPilot(clock=self.clock)

    def run_ctx(self, n, **ctx):
        base = {"fg": "auto", "objective": "balanced", "refresh": 90, "game": True, "app": 1, "confirmed": True,
                "real": 45.0, "pace": 1.0, "at_ceiling": True, "engine": {"auto": True, "cap_fps": 45}}
        base.update(ctx)
        out = None
        for _ in range(n):
            self.clock.t += 1
            out = self.p.tick(base)
        return out

    def test_ladder(self):
        self.assertEqual(pilot.ladder(90), [24, 30, 35, 40, 45])
        self.assertEqual(pilot.ladder(60), [20, 25, 30])
        self.assertEqual(pilot.mult_label(90, 24), "x3.75")

    def test_measure_then_lock(self):
        out = self.run_ctx(3)
        self.assertEqual(out["phase"], "measure")
        self.assertTrue(out["hold_ceiling"])
        self.assertEqual((out["multiplier"], out["base_fps"], out["auto_floor"]), (1, 0, 30))  # Balanced: x3 lowest
        out = self.run_ctx(15)
        self.assertEqual((out["phase"], out["point"], out["base_fps"]), ("locked", 45, 45))
        self.assertFalse(out["hold_ceiling"])

    def test_lowest_point_per_objective(self):
        # User, 3.0.10: Balanced works down to x3, Quality to x2.5; Battery and Autopilot keep x3.75.
        self.assertEqual(pilot.lowest_point(90, "balanced"), 30)
        self.assertEqual(pilot.lowest_point(90, "quality"), 40)
        self.assertEqual(pilot.lowest_point(60, "quality"), 25)
        self.assertEqual(pilot.lowest_point(90, "battery"), 24)
        self.assertEqual(pilot.lowest_point(90, "autopilot"), 24)

    def test_quality_never_below_x2_5(self):
        self.run_ctx(15, objective="quality", real=33.0, engine={"auto": True, "cap_fps": 30})
        out = self.run_ctx(60, objective="quality", real=33.0, engine={"auto": True, "cap_fps": 30})
        self.assertEqual(out["point"], 40)
        self.assertFalse(out["holds"])

    def test_balanced_never_below_x3(self):
        self.run_ctx(15, real=26.0, engine={"auto": True, "cap_fps": 24})
        out = self.run_ctx(60, real=26.0, engine={"auto": True, "cap_fps": 24})
        self.assertEqual(out["point"], 30)

    def test_stale_auto_cap_does_not_lock(self):
        self.run_ctx(2)
        out = self.run_ctx(13, confirmed=False, real=None, engine={})
        self.assertEqual(out["phase"], "measure", "silence is not a stable measurement")

    def test_autopilot_keeps_x2_below_the_whole_15w(self):
        # 3.3.0: the multiplier follows the game; x2 is kept up to the 15 W ceiling.
        self.run_ctx(20)                       # locked 45
        out = self.run_ctx(100, autopilot=True, tdp_w=13.5, gov_settled=True)
        self.assertEqual(out["point"], 45)

    def test_autopilot_keeps_x2_even_at_the_whole_15w(self):
        # 3.3.0: above x3 the multiplier follows the game; only the guard steps down.
        self.run_ctx(20)                       # locked 45
        out = self.run_ctx(120, autopilot=True, tdp_w=14.8, gov_settled=True)
        self.assertEqual(out["point"], 45)

    def test_autopilot_battery_keeps_x3_until_low_battery(self):
        out = self.run_ctx(2, objective="battery", autopilot=True)
        self.assertEqual(out["point"], 30)
        self.p.reset()
        self.p.context = None
        out = self.run_ctx(2, objective="battery", autopilot=True, low_battery=True, above_x3=True)
        self.assertEqual(out["point"], 24)

    def test_autopilot_auto_low_battery_preserves_322_floor(self):
        self.run_ctx(20, objective="autopilot", autopilot=True, above_x3=True)
        out = self.run_ctx(2, objective="autopilot", autopilot=True, low_battery=True, above_x3=True)
        self.assertEqual(out["point"], 24)

    def test_autopilot_smoothness_preference_retains_game_point_on_low_battery(self):
        self.run_ctx(20, objective="autopilot", autopilot=True, preference="smoothness")
        out = self.run_ctx(2, objective="autopilot", autopilot=True, low_battery=True, preference="smoothness")
        self.assertEqual(out["point"], 45)

    def test_failed_climbs_wait_longer_each_time(self):
        # 3.3.1: a point that did not hold waits 1, 2, 4 ... min before the next climb into it.
        p = self.p
        p.retry_at.clear()
        self.assertEqual(p._failed_climb(35, 100.0), 60.0)
        self.assertEqual(p._failed_climb(35, 200.0), 120.0)
        self.assertEqual(p.retry_at[35], 320.0)
        for _ in range(6):
            p._failed_climb(35, 0.0)
        self.assertEqual(p.retry_at[35], pilot.RETRY_MAX_S)

    def test_short_trial_ends_early(self):
        self.run_ctx(15, real=31.0, engine={"auto": True, "cap_fps": 30})   # locked 30
        p = self.p
        self.assertEqual(p.point, 30)
        p.trial_cap, p.trial_from = 35, 30
        p._go("trial", "trying point 35")
        out = self.run_ctx(int(pilot.TRIAL_EARLY_S) + 2, real=31.0, engine={"auto": True, "cap_fps": 35})
        self.assertEqual((out["phase"], out["point"]), ("locked", 30))
        self.assertGreater(p.retry_at.get(35, 0.0), self.clock.t)

    def test_battery_locks_x375(self):
        out = self.run_ctx(2, objective="battery", above_x3=True)
        self.assertEqual((out["point"], out["base_fps"], out["label"]), (24, 24, "x3.75"))

    def test_x3_is_the_limit_until_unlocked(self):
        # User, 3.1.1: "above x3 plays very badly"; x3.75 only with the setting.
        out = self.run_ctx(2, objective="battery")
        self.assertEqual((out["point"], out["label"]), (30, "x3"))
        self.p.reset()
        self.p.context = None
        out = self.run_ctx(2, objective="battery", autopilot=True, low_battery=True)
        self.assertEqual(out["point"], 30)
        self.p.reset()
        self.p.context = None
        self.assertEqual(self.run_ctx(1, fg="x3.75")["multiplier"], 3)
        self.assertEqual(self.run_ctx(1, fg="custom", base_fps=24)["base_fps"], 30)
        self.run_ctx(15, objective="autopilot", autopilot=True, real=26.0, engine={"auto": True, "cap_fps": 24})
        out = self.run_ctx(60, objective="autopilot", autopilot=True, real=26.0, engine={"auto": True, "cap_fps": 24})
        self.assertEqual(out["point"], 30)
        self.assertFalse(out["holds"])

    def test_demotes_only_at_full_power(self):
        self.run_ctx(20)
        out = self.run_ctx(12, real=38.0, at_ceiling=False)
        self.assertEqual(out["point"], 45, "below the ceiling the Governor answers, not the pilot")
        out = self.run_ctx(12, real=38.0, at_ceiling=True)
        self.assertEqual(out["point"], 35)

    def test_fixed_choices(self):
        self.assertEqual(self.run_ctx(1, fg="x3")["multiplier"], 3)
        out = self.run_ctx(1, fg="x3.75", above_x3=True)
        self.assertEqual((out["multiplier"], out["base_fps"]), (1, 24))
        out = self.run_ctx(1, fg="custom", base_fps=40)
        self.assertEqual(out["base_fps"], 40)
        self.assertEqual(self.run_ctx(1, fg="off")["multiplier"], 0)


class FakeActuator:
    def __init__(self, tdp=15.0):
        self.tdp, self.initial, self.owned, self.external, self.writes = tdp, None, False, False, []

    def claim(self):
        self.initial, self.owned = self.tdp, True
        return self.status()

    def set_tdp_w(self, w):
        self.tdp = w
        self.writes.append(w)
        return {"success": True, "state": self.status()}

    def verify_ownership(self):
        return self.status()

    def restore_if_owned(self):
        self.tdp, self.owned = self.initial, False
        return {"restored": True}

    def status(self):
        return {"owned": self.owned, "external_change": self.external, "initial_tdp_w": self.initial,
                "ceiling_tdp_w": self.initial, "minimum_tdp_w": 3.0, "draw_w": 9.0}


class GovernorTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.act = FakeActuator()
        self.g = PowerGovernor(self.act, None, clock=self.clock)

    def feed(self, n, **kw):
        s = {"game": True, "key": "app:1|90", "objective": "balanced", "cap": 45, "epoch": 1, "real": 45.0,
             "pace": 1.0, "over_budget": 0.0, "ring_miss": 0.0, "temp_c": 70.0}
        s.update(kw)
        for _ in range(n):
            self.clock.t += 1
            self.g.tick(s)

    def test_verdicts(self):
        self.assertTrue(healthy([{"real": 45, "pace": 1.0}] * 10, 45, True)["ok"])
        self.assertEqual(healthy([{"real": 40, "pace": 1.0}] * 10, 45, True)["reason"], "below-cap")
        self.assertEqual(healthy([{"real": 45, "pace": 0.98}] * 10, 45, True)["reason"], "pace")

    def test_steps_down_then_insures(self):
        self.feed(40)
        self.assertLess(self.act.tdp, 15.0)
        low = self.act.tdp
        self.feed(4, real=30.0)  # severe shortfall at a binding limit
        self.assertGreater(self.act.tdp, low, "a clear shortfall gets watts back within seconds")
        self.feed(24, real=30.0)
        self.assertEqual(self.act.tdp, 15.0)

    def test_failed_probe_leaves_an_expiring_floor(self):
        self.feed(1)
        start = self.act.tdp
        self.assertEqual(start, 12.0, "Balanced starts at 12 W")
        self.feed(9)
        probed = self.act.tdp
        self.assertLess(probed, start)
        self.feed(3, real=40.0)  # the step down does not hold
        self.assertGreater(self.act.tdp, probed)
        self.assertEqual(self.g.floor_fail[45], probed)
        self.clock.t += 301
        self.assertNotIn(45, self.g.floor_fail, "a floor expires")

    def test_calm_after_a_raise(self):
        self.feed(10, objective="autopilot")
        probed = self.act.tdp
        self.feed(6, real=38.0, objective="autopilot")  # the step down does not hold: back up
        restored = self.act.tdp
        self.assertGreater(restored, probed)
        self.feed(25, objective="autopilot")
        self.assertEqual(self.act.tdp, restored, "no probe down within 30 s of a raise")
        self.feed(30, objective="autopilot")
        self.assertEqual(self.act.tdp, restored - 1.0, "then a watt at a time, above the failed level")

    def test_calm_doubles_when_raises_repeat(self):
        self.feed(1)
        for _ in range(3):
            self.g._write(10.0, "window-held")
            self.g._write(12.0, "fast-restore")
        self.assertEqual(self.g.calm_until - self.clock.t, 120.0)

    def test_two_held_windows_per_step(self):
        self.feed(1)
        self.g.fast = False
        before = len(self.act.writes)
        self.feed(30)
        self.assertEqual(len(self.act.writes) - before, 1, "one step per two 8 s windows")

    def test_autopilot_one_watt_near_a_failed_level(self):
        self.feed(1, objective="autopilot")
        self.g.fast = False
        self.g.failed_at[45] = 10.0
        self.g._write(13.0, "test")
        self.feed(25, objective="autopilot")
        self.assertEqual(self.act.writes[-1], 12.0, "a watt at a time within 2 W of a failure")

    def test_draw_headroom_jumps_down(self):
        self.act.draw = 6.0
        self.feed(30, objective="battery", draw_w=6.0)
        self.assertLessEqual(self.act.tdp, 7.5, "the limit follows the draw when it is not binding")

    def test_quality_never_probes_and_hold(self):
        self.feed(60, objective="quality")
        self.assertEqual(self.act.writes, [])
        self.feed(30)
        self.assertLess(self.act.tdp, 15.0)
        self.feed(2, hold_ceiling=True)
        self.assertEqual(self.act.tdp, 15.0)

    def test_restore_on_exit(self):
        self.feed(40)
        self.feed(1, game=False)
        self.assertEqual(self.act.tdp, 15.0)
        self.assertFalse(self.act.owned)


class AutopilotTest(unittest.TestCase):
    def test_one_adaptive_mode(self):
        clock = Clock()
        a = autopilot.Autopilot(clock=clock)
        for power in ({"external_power": True}, {"external_power": False, "percent": 60}):
            self.assertEqual(a.decide("auto", power, 60)["objective"], "autopilot")
        a.decide("auto", {"external_power": False, "percent": 19}, 60)
        self.assertTrue(a.low_battery)
        a.decide("auto", {"external_power": False, "percent": 23}, 60)
        self.assertTrue(a.low_battery, "back only above 25 %")
        a.decide("auto", {"external_power": False, "percent": 26}, 60)
        self.assertFalse(a.low_battery)

    def test_heat_does_not_flap(self):
        clock = Clock()
        a = autopilot.Autopilot(clock=clock)
        for temp in (84.9, 85.1, 84.9, 85.1, 84.0, 83.0):
            a.decide("auto", {"external_power": True}, temp)
            clock.t += 1
        self.assertTrue(a.hot)
        for _ in range(40):
            clock.t += 1
            a.decide("auto", {"external_power": True}, 75)
        self.assertFalse(a.hot, "cool for 30 s")

    def test_governor_never_above_15_in_any_objective(self):
        for objective in ("quality", "balanced", "battery", "autopilot"):
            clock = Clock()
            act = FakeActuator()
            act.tdp = act.initial = 20.0
            g = PowerGovernor(act, None, clock=clock)
            s = {"game": True, "key": "app:1|90", "objective": objective, "cap": 40, "epoch": 1,
                 "real": 30.0, "pace": 1.0, "over_budget": 0.0, "ring_miss": 0.0, "temp_c": 70.0}
            for _ in range(120):
                clock.t += 1
                g.tick(s)
                self.assertLessEqual(act.tdp, 15.0, objective)

    def test_governor_autopilot_starts_at_15_and_tolerates_small_dips(self):
        clock = Clock()
        act = FakeActuator()
        act.tdp = act.initial = 20.0
        g = PowerGovernor(act, None, clock=clock)
        s = {"game": True, "key": "app:1|90", "objective": "autopilot", "cap": 40, "epoch": 1,
             "real": 40.0, "pace": 1.0, "over_budget": 0.0, "ring_miss": 0.0, "temp_c": 70.0}
        clock.t += 1
        g.tick(s)
        self.assertEqual(act.tdp, 15.0)
        for _ in range(12):
            clock.t += 1
            g.tick(s)                     # holds: the TDP goes down
        self.assertLess(act.tdp, 15.0)
        checked = g.probe_from or act.tdp   # the last level that held a whole window
        for _ in range(30):
            clock.t += 1
            g.tick({**s, "real": 37.6})   # 6 % short: tolerated, no watts for it (5-8 %, user 3.1.1);
        self.assertLessEqual(act.tdp, checked)   # only the step that caused it is taken back
        low = act.tdp
        for _ in range(3):
            clock.t += 1
            g.tick({**s, "real": 20.0})   # a short sharp drop: no watts for it
        self.assertEqual(act.tdp, low)
        for _ in range(20):
            clock.t += 1
            g.tick({**s, "real": 33.0})   # 17 % short and lasting, confirmed by a second window: watts back
        self.assertGreater(act.tdp, low)
