"""Check preservation of saved settings without touching SteamOS or user files."""
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py_modules"))
from gfg_plugin import settings
from sandbox.run import make_rig
from sandbox.vdeck import VGame


class SavedSettingsUpgradeTest(unittest.TestCase):
    def test_legacy_fixed_profile_migration_matches_322(self):
        self.assertEqual(settings.normalize_profile({"fg": "x3"})["fg"], "auto")
        for schema in (0, 1, 2, 3):
            for mode in ("x2", "x3", "x3.75", "custom", "off", "auto"):
                with self.subTest(schema=schema, mode=mode):
                    saved = {"schema": schema, "profiles": {
                        "default": {"fg": mode}, "app:123": {"fg": mode}}}
                    migrated = settings.normalize(saved)
                    for profile in migrated["profiles"].values():
                        self.assertEqual(profile["fg"], "off" if mode == "off" else "auto")
                    self.assertEqual(settings.normalize(migrated), migrated)

    def test_new_fixed_selection_survives_legacy_migration_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"schema": 3, "profiles": {"default": {"fg": "x3"}}}), encoding="utf-8")
            store = settings.Settings(path)
            self.assertEqual(store.profile_for(None)[1]["fg"], "auto")
            for mode in ("x2", "x3", "x3.75", "custom"):
                store.update_profile("default", {"fg": mode})
                key = store.make_game_profile(123, "Game")
                store.update_profile(key, {"fg": mode})
                store = settings.Settings(path)
                self.assertEqual(store.profile_for(None)[1]["fg"], mode)
                self.assertEqual(store.profile_for(123)[1]["fg"], mode)

    def test_322_explicit_power_choices_survive_schema_upgrade(self):
        for split in (False, True):
            for gpu_clock in (False, True):
                with self.subTest(split=split, gpu_clock=gpu_clock):
                    saved = {"schema": 3, "power": {"enabled": True, "split": split,
                                                    "gpu_clock": gpu_clock}}
                    result = settings.normalize(saved)
                    self.assertEqual(result["power"], saved["power"])

    def test_later_settings_save_does_not_replace_322_power_choices(self):
        saved = {"schema": 3, "power": {"enabled": True, "split": True, "gpu_clock": True}}
        with patch.object(settings, "read_json", return_value=saved), \
             patch.object(settings, "write_json_atomic") as write:
            store = settings.Settings(Path("nonexistent-settings.json"))
            store.update_profile("default", {"fg": "off"})
        written = write.call_args.args[1]
        self.assertEqual(written["power"], saved["power"])

    def test_defaults_are_versioned_and_individual_absent_keys_use_322_defaults(self):
        self.assertEqual(settings.normalize({})["power"],
                         {"enabled": True, "split": False, "gpu_clock": False})
        self.assertEqual(settings.normalize({"schema": 3, "power": {"split": False}})["power"],
                         {"enabled": True, "split": False, "gpu_clock": True})
        self.assertEqual(settings.normalize({"schema": 3, "power": {"gpu_clock": False}})["power"],
                         {"enabled": True, "split": True, "gpu_clock": False})
        self.assertEqual(settings.normalize({"schema": 4, "power": {}})["power"],
                         {"enabled": True, "split": False, "gpu_clock": False})

    def test_explicit_disabled_power_and_both_controls_survive_real_file_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            saved = {"schema": 3, "power": {"enabled": False, "split": True, "gpu_clock": True}}
            path.write_text(json.dumps(saved), encoding="utf-8")
            first = settings.Settings(path)
            self.assertEqual(first.data["power"], saved["power"])
            first.update_profile("default", {"fg": "off"})
            second = settings.Settings(path)
            self.assertEqual(second.data["power"], saved["power"])
            self.assertEqual(second.data["schema"], settings.SCHEMA)
            self.assertEqual(second.data["profiles"]["default"]["fg"], "off")

    def test_migration_is_idempotent(self):
        for split in (False, True):
            for gpu_clock in (False, True):
                with self.subTest(split=split, gpu_clock=gpu_clock):
                    migrated = settings.normalize({"schema": 3, "power": {"split": split, "gpu_clock": gpu_clock}})
                    self.assertEqual(settings.normalize(migrated), migrated)

    def test_migrated_controls_use_service_interlocks(self):
        # Exercise the real service with a virtual engine and instrumented controller endpoints.
        # These checks cover arbitration inputs, not SteamOS actuator or timing behaviour.
        rig = make_rig(VGame("steady", 90, noise=0), fg="x3", objective="balanced", user_tdp=12)
        rig.service.split.stop()
        rig.service.gclock.stop()

        class Split:
            changes = 0
            def __init__(self):
                self.cpu = SimpleNamespace(owned=False, restore_pending=False)
                self.busy = False
                self.ctx = {}
            def tick(self, ctx):
                self.ctx = dict(ctx)
                return {"phase": "observe"}
            def settling(self): return self.busy
            def stop(self): pass

        class Clock:
            changes = 0
            owned = False
            def __init__(self): self.ctx = {}
            def tick(self, ctx):
                self.ctx = dict(ctx)
                return {"phase": "observe"}
            def settling(self): return False
            def stop(self): pass

        split, clock = Split(), Clock()
        rig.service.split, rig.service.gclock = split, clock
        rig.settings.data["power"] = settings.normalize({"schema": 3, "power": {
            "enabled": True, "split": True, "gpu_clock": True}})["power"]

        def tick():
            rig.engine.step()
            rig.sensors.step()
            rig.service.tick()
            rig.clock.t += 1
            self.assertLessEqual(rig.power.tdp, 12.0, "virtual user's TDP ceiling was exceeded")

        try:
            for _ in range(1800):
                tick()
                if split.ctx.get("steady") and clock.ctx.get("steady"):
                    break
            self.assertTrue(split.ctx.get("enabled"))
            self.assertTrue(clock.ctx.get("enabled"))
            self.assertTrue(split.ctx.get("steady"), "virtual rig never reached a settled point")
            self.assertTrue(clock.ctx.get("steady"))
            self.assertFalse(clock.ctx.get("other_busy"))
            clock.owned = True
            tick()
            self.assertFalse(split.ctx["steady"], "split must wait while GPU clock is owned")
            clock.owned = False
            split.cpu.owned = True
            tick()
            self.assertTrue(clock.ctx["other_busy"], "GPU clock must wait while CPU cap is owned")
            split.cpu.owned = False
            split.busy = True
            tick()
            self.assertTrue(clock.ctx["other_busy"], "GPU clock must wait while split is settling")
        finally:
            rig.service.stop()

    def test_migrated_disabled_power_does_not_claim_virtual_controls(self):
        rig = make_rig(VGame("disabled", 90, noise=0), fg="x3", objective="balanced", user_tdp=12)
        rig.settings.data["power"] = settings.normalize({"schema": 3, "power": {
            "enabled": False, "split": True, "gpu_clock": True}})["power"]
        try:
            for _ in range(300):
                rig.engine.step()
                rig.sensors.step()
                rig.service.tick()
                rig.clock.t += 1
                self.assertFalse(rig.service.split.cpu.owned)
                self.assertFalse(rig.service.gclock.owned)
                self.assertEqual(rig.power.tdp, 12)
        finally:
            rig.service.stop()
    def test_322_off_profile_remains_off(self):
        saved = {"schema": 3, "profiles": {"default": {"fg": "off"},
                                           "app:123": {"fg": "off"}}}
        profiles = settings.normalize(saved)["profiles"]
        self.assertEqual(profiles["default"]["fg"], "off")
        self.assertEqual(profiles["app:123"]["fg"], "off")

    def test_saved_fixed_modes_are_preserved_when_supported(self):
        for mode in ("auto", "x2", "x3", "x3.75", "custom"):
            with self.subTest(mode=mode):
                saved = {"schema": settings.SCHEMA, "profiles": {"default": {"fg": mode}}}
                self.assertEqual(settings.normalize(saved)["profiles"]["default"]["fg"], mode)


if __name__ == "__main__":
    unittest.main()
