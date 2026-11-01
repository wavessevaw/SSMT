"""GFG Extreme 3.0: the Decky backend.

Runs as root (plugin.json "root" flag): it installs the KEDAVRA Gamescope and writes the TDP.
Files in the user's home are handed to the user; session-bus tools run as the user.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import decky  # provided by Decky Loader at runtime

from . import engine_gate, filters, paths
from .activity_log import ActivityLog
from .boost import Boost
from .coexist import Coexistence
from .cpu_freq import CpuFreqActuator
from .game_model import GameModelStore
from .governor_battery import read_battery
from .governor_power import SteamDeckPowerActuator
from .host_sensors import HostSensors, device_name
from .kedavra.ctl import Control
from .kedavra.hud import Hud
from .kedavra.install import Installer, bench
from .legacy_cleanup import LegacyCleanup
from .migration import Migration
from .service import Service
from .settings import Settings, profile_key
from .split_control import SplitControl
from .gpu_clock import GpuClock
from .gpu_clock_control import GpuClockControl
from .steamos_gpu import SteamOSGpuBackend
from .steamos_tdp import SteamOSManagerTdp
from .telemetry import Telemetry
from .user_files import ensure_dir, home_owner, read_json, write_json_atomic

PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent


class Plugin:
    async def _main(self) -> None:
        self.lock = asyncio.Lock()
        self.tasks = []
        self.stopping = False
        self.home = Path(decky.DECKY_USER_HOME)
        self.owner = home_owner(self.home)
        uid = self.owner[0] if self.owner else 1000
        self.data = paths.data_dir(self.home)
        self.runtime = paths.runtime_dir(self.home)
        ensure_dir(self.runtime, self.owner, stop=self.home)
        self.log = ActivityLog(self.runtime / "activity.jsonl", owner=self.owner)
        self.inst = Installer(PLUGIN_DIR, self.home, uid=uid)
        await self._recover_installer()
        self.telemetry = Telemetry(self.runtime / "telemetry", self.owner)
        self.settings = Settings(self.data / "settings.json", self.owner)
        self.migration_report = read_json(self.data / "migration-report.json")
        self.legacy = LegacyCleanup(self.home, owner=self.owner)
        await self._worker(self._first_start)
        self.telemetry.level = self.settings.data["telemetry"]["level"]
        self.ctl = Control(self.home, PLUGIN_DIR, uid=uid, data=self.runtime)
        self.coexist = Coexistence(self.home, owner=self.owner, version=paths.VERSION)
        manager = SteamOSManagerTdp(home=str(self.home))
        actuator = SteamDeckPowerActuator(manager=manager, marker=self.runtime / "tdp-cap.json")
        actuator.journal = lambda kind, **f: self.log.record("power", kind, **f)
        model = GameModelStore(self.data / "game-models.json", owner=self.owner)
        cpu = CpuFreqActuator(marker=self.runtime / "cpu-cap.json")
        cpu.journal = lambda kind, **f: self.log.record("power", kind, **f)
        split = SplitControl(cpu, self.data / "power-split.json", self.owner,
                             journal=lambda kind, **f: self.log.record("split", kind, **f))
        # SteamOSManager's manual GPU clock (the QAM slider) runs on the user's session bus.
        gpu = GpuClock(SteamOSGpuBackend(home=str(self.home)), writes_enabled=True,
                       receipt_path=self.runtime / "gpu-clock.json")
        gclock = GpuClockControl(gpu, journal=lambda kind, **f: self.log.record("gpu-clock", kind, **f))
        self.service = Service(
            settings=self.settings, ctl=self.ctl, actuator=actuator, model=model, sensors=HostSensors(),
            battery=read_battery, hud=Hud(paths.HUD_PATH), hud_path=str(paths.HUD_PATH),
            journal=lambda src, kind, **f: self.log.record(src, kind, **f), telemetry=self.telemetry,
            split=split, gclock=gclock,
            boost=Boost(self.ctl, journal=lambda kind, **f: self.log.record("boost", kind, **f)))
        self.lock = asyncio.Lock()
        self._gate_at, self._gate_cache = -1e9, {}
        self._host_at, self._host_cache = -1e9, {}
        self.state: Dict[str, Any] = {}
        self.stopping = False
        loop = asyncio.get_event_loop()
        self.tasks = [loop.create_task(self._loop()), loop.create_task(self._health())]
        self.log.record("plugin", "start", version=paths.VERSION)

    async def _recover_installer(self) -> None:
        try:
            recovered = await self._worker(self.inst.recover_pending)
        except Exception as error:
            self.log.record("installer", "recovery-failed", error=str(error))
            raise  # do not start controllers over an unresolved installation
        if recovered:
            self.log.record("installer", "recovered")

    def _first_start(self) -> None:
        mig = Migration(self.home, self.data, self.owner)
        if mig.needed(self.settings):
            try:
                self.migration_report = mig.run(self.settings)
                self.log.record("migration", "done", profiles=len(self.migration_report.get("profiles", [])),
                                backup=self.migration_report.get("backup"))
            except Exception as error:  # a broken 2.x file never blocks 3.0 from starting
                self.log.record("migration", "failed", error=str(error))
                self.settings.save()
        elif not self.settings.exists:
            self.settings.save()
        marker = self.data / "legacy-cleanup.json"
        if not marker.exists() and not self.legacy.pending()["clean"]:
            result = self.legacy.run(system_state=True)
            write_json_atomic(marker, {"time": time.time(), **result}, self.owner)
            self.log.record("legacy", "cleanup", ok=result["ok"], steps=len(result["steps"]))

    async def _worker(self, fn, *args, **kwargs):
        """Cancellation must not release our lock while its thread can still write."""
        worker = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
        cancelled = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if cancelled:
            # Retrieve an exception too, so the worker never becomes unobserved.
            if not worker.cancelled():
                worker.exception()
            raise asyncio.CancelledError
        return worker.result()

    async def _loop(self) -> None:
        while not self.stopping:
            started = time.monotonic()
            try:
                async with self.lock:
                    if self.stopping:
                        return
                    self.state = await self._worker(self.service.tick)
            except Exception as error:
                decky.logger.warning(f"tick: {error}")
            await asyncio.sleep(max(0.2, 1.0 - (time.monotonic() - started)))

    async def _health(self) -> None:
        # A boot that mounted KEDAVRA is confirmed only once its Gamescope writes status; an
        # unconfirmed boot is rolled back on the next boot.
        while not self.stopping:
            try:
                await self._worker(self.inst.mark_healthy)
            except Exception as error:
                decky.logger.warning(f"health: {error}")
            await asyncio.sleep(5)

    async def _unload(self) -> None:
        self.stopping = True
        if getattr(self, "_shutdown_task", None) is None:
            self._shutdown_task = asyncio.create_task(self._shutdown())
        # Keep one shutdown running even if the host cancels an unload caller.
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self) -> None:
        tasks = getattr(self, "tasks", [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        async with self.lock:
            if hasattr(self, "service"):
                await self._worker(self.service.stop)
        if hasattr(self, "log"):
            self.log.record("plugin", "unload")

    async def _uninstall(self) -> None:
        # The engine stays installed: it is reversible from Setup, and other tools may use it.
        # Generation is switched off so stock behaviour returns at once.
        try:
            await self._worker(self.ctl.set_var, "kfg_multiplier", 0)
            await self._worker(self.ctl.set_var, "kfg_post", "")
        except Exception:
            pass

    # ------------------------------------------------------------------ state
    def _gate(self) -> Dict[str, Any]:
        """First-run gate, re-checked at most every 5 s (findmnt is a subprocess)."""
        now = time.monotonic()
        if now - self._gate_at < 5.0 and self._gate_cache:
            return self._gate_cache
        self._gate_at = now
        self._gate_cache = engine_gate.gate(self.inst.installed_version(), self.inst.bundled_version(),
                                            self.inst._mounted(), engine_gate.boot_id(),
                                            engine_gate.read_record(self.data / "engine-install.json"))
        return self._gate_cache

    def _host(self) -> Dict[str, Any]:
        """Device name and the other plugins, re-read at most every 5 s: the panel asks every
        second while it is open, and both only change on a dock or a plugin install (3.1.2)."""
        now = time.monotonic()
        if now - self._host_at >= 5.0 or not self._host_cache:
            self._host_at = now
            self._host_cache = {"device": device_name(), "coexist": self.coexist.status()}
        return self._host_cache

    async def get_state(self) -> Dict[str, Any]:
        host = await self._worker(self._host)
        return {"version": paths.VERSION, "codename": paths.CODENAME, "live": self.state,
                "gate": await self._worker(self._gate),
                "device": host["device"],
                "settings": self.settings.data, "profiles": self._profiles(),
                "migration": self.migration_report, "coexist": host["coexist"],
                "capture": self.ctl._capture_info()}

    def _profiles(self) -> Dict[str, Any]:
        app = (self.state or {}).get("app") or 0
        key, _ = self.settings.profile_for(app)
        return {"active": key, "game_key": profile_key(app), "names": {k: v["name"] for k, v in self.settings.data["profiles"].items()}}

    async def get_catalog(self) -> Dict[str, Any]:
        return filters.catalog()

    async def set_profile(self, scope: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        """scope "game": the running game's own profile (made from Default when missing);
        "default": every game without its own profile."""
        allowed = {k: v for k, v in (changes or {}).items() if k in ("fg", "base_fps", "objective", "filters")}
        if "filters" in allowed and isinstance(allowed["filters"], str):
            allowed["filters"] = filters.preset(allowed["filters"])
        async with self.lock:
            if self.stopping:
                return {"ok": False, "error": "Plugin is stopping"}
            if scope == "game":
                app = (self.state or {}).get("app")
                key = self.settings.make_game_profile(app, (changes or {}).get("name", ""))
            else:
                key = "default"
            profile = await self._worker(self.settings.update_profile, key, allowed)
        self.log.record("ui", "set-profile", key=key, **allowed)
        return {"key": key, "profile": profile}

    async def drop_game_profile(self) -> Dict[str, Any]:
        async with self.lock:
            if self.stopping:
                return {"ok": False, "error": "Plugin is stopping"}
            ok = await self._worker(self.settings.drop_game_profile, (self.state or {}).get("app"))
        return {"dropped": ok}

    async def set_section(self, section: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        async with self.lock:
            if self.stopping:
                return {"ok": False, "error": "Plugin is stopping"}
            value = await self._worker(self.settings.set_section, section, changes or {})
            if section == "telemetry":
                self.telemetry.level = value["level"]
        self.log.record("ui", "set-section", section=section, **(changes or {}))
        return value

    # ------------------------------------------------------------------ engine
    async def engine_install(self) -> Dict[str, Any]:
        async with self.lock:
            if self.stopping:
                return {"ok": False, "error": "Plugin is stopping"}
            result = await self._worker(self.inst.install)
            await self._worker(self.coexist.mark_owner)
            self._host_cache = {}
            ok = result.get("ok", True) is not False
            record = {"version": self.inst.bundled_version(), "boot_id": engine_gate.boot_id() if ok else "",
                      "error": "" if ok else str(result.get("log") or result.get("error") or "install failed")}
            try:
                await self._worker(write_json_atomic, self.data / "engine-install.json", record, self.owner)
            except OSError:
                pass
            self._gate_cache = {}
        self.log.record("engine", "install", **{k: v for k, v in result.items() if k != "message"})
        return result

    async def engine_uninstall(self) -> Dict[str, Any]:
        async with self.lock:
            if self.stopping:
                return {"ok": False, "error": "Plugin is stopping"}
            try:
                result = await self._worker(self.inst.uninstall)
            finally:
                self._gate_cache = {}
                self._host_cache = {}
        self.log.record("engine", "uninstall", needs_reboot=result.get("needs_reboot"))
        return result

    async def reboot(self) -> Dict[str, Any]:
        self.log.record("engine", "reboot")
        return await self._worker(self.inst.reboot)

    async def diagnose(self) -> Dict[str, Any]:
        d = await self._worker(self.inst.diagnose)
        d["bundled"] = self.inst.bundled_version()
        d["installed"] = self.inst.installed_version()
        d["legacy"] = await self._worker(self.legacy.pending)
        return d

    async def run_bench(self) -> Dict[str, Any]:
        async with self.lock:
            if self.stopping:
                return {"ok": False, "error": "Plugin is stopping"}
            return await self._worker(bench, PLUGIN_DIR, self.runtime)

    # Game benchmark (3.3.0): 20 s in game at 15 W, generation off; answered on Dash.
    async def bench_start(self) -> Dict[str, Any]:
        async with self.lock:
            if self.stopping:
                return {"ok": False, "error": "Plugin is stopping"}
            r = self.service.bench_start()
            self.log.record("bench", "start", ok=r.get("ok"))
            return r

    async def bench_answer(self, accept: bool) -> Dict[str, Any]:
        async with self.lock:
            if self.stopping:
                return {"ok": False, "error": "Plugin is stopping"}
            r = self.service.bench_answer(bool(accept))
            self.log.record("bench", "accept" if accept else "decline")
            return r

    async def save_report(self) -> Dict[str, Any]:
        extra = [(f"gfg/{p.name}", p) for p in self.telemetry.files()]
        for name in ("activity.jsonl",):
            if (self.runtime / name).exists():
                extra.append((f"gfg/{name}", self.runtime / name))
        for name in ("settings.json", "migration-report.json", "legacy-cleanup.json"):
            if (self.data / name).exists():
                extra.append((f"gfg/{name}", self.data / name))
        snap = self.runtime / "state.json"
        try:
            snap.write_text(json.dumps(self.state, default=str, indent=1))
            extra.append(("gfg/state.json", snap))
        except OSError:
            pass
        return await self._worker(self.inst.report, self.runtime, None, "GFG-Extreme-report", extra)

    async def start_capture(self, label: str = "run") -> Dict[str, Any]:
        async with self.lock:
            if self.stopping:
                return {"ok": False, "error": "Plugin is stopping"}
            return await self._worker(self.ctl.start_capture, label)

    async def stop_capture(self) -> Dict[str, Any]:
        async with self.lock:
            if self.stopping:
                return {"ok": False, "error": "Plugin is stopping"}
            return await self._worker(self.ctl.stop_capture)

    async def list_captures(self):
        return await self._worker(self.ctl.list_captures)

    async def get_logs(self) -> str:
        return await self._worker(self.ctl.logs)

    # ------------------------------------------------------------------ coexistence / legacy
    async def take_over(self) -> Dict[str, Any]:
        async with self.lock:
            if self.stopping:
                return {"ok": False, "error": "Plugin is stopping"}
            result = await self._worker(self.coexist.take_over)
            self._host_cache = {}
            self.service.coord.take_over()
            self.log.record("coexist", "take-over", steps=result.get("steps"))
            return result

    async def legacy_cleanup(self) -> Dict[str, Any]:
        async with self.lock:
            if self.stopping:
                return {"ok": False, "error": "Plugin is stopping"}
            result = await self._worker(self.legacy.run, system_state=False)
            self.log.record("legacy", "cleanup-manual", ok=result["ok"])
            return result
