"""One-time migration of GFG Extreme 2.x (MAKO) settings to 3.0.

Reads ~/.config/mako-render (never writes it: a rollback to 2.1.6 finds its settings intact),
saves a backup zip of everything 3.0 replaces, and writes 3.0 settings:

  2.x profile "mako" (Default)          -> profile "default"
  2.x profile with a Steam app id        -> profile "app:<id>"
  2.x profile matched by process only    -> not carried over (3.0 profiles follow the Steam app
                                            id the engine reports); listed in the report
  frame generation off / adaptive / x2 / x3 / x4+ / fixed base cap
                                         -> off / auto / x2 / x3 / auto / custom
  Governor mode budget / balanced / quality / autopilot (extreme) and Governor off
                                         -> battery / balanced / quality / autopilot, quality
  vkBasalt effects, CAS/DLS, FXAA/SMAA   -> 3.0 filters (now after frame generation)
  HUD on/off and corner, Autopilot preference -> the same
  Learned TDP points                     -> not used: they were measured with MAKO's costs.
                                            Kept in the backup; the Governor learns again.
"""
from __future__ import annotations

import json
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import filters, settings as settings_mod
from .paths import LEGACY_DATA_DIRNAME, LEGACY_WRAPPERS
from .user_files import ensure_dir, own

LEGACY_DEFAULT = "mako"
BACKUP_MAX_FILE = 4 * 1024 * 1024
HUD_CORNERS = {"top-left", "top-right", "bottom-left", "bottom-right"}


def _parse_toml(text: str) -> Dict[str, Any]:
    try:
        import tomllib  # Python 3.11+
        return tomllib.loads(text)
    except ImportError:
        pass
    except Exception:
        return {}
    # Small fallback for the flat files 2.x wrote: [section], [[profile]], key = scalar.
    out: Dict[str, Any] = {}
    cur: Dict[str, Any] = out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[\[\s*([\w.-]+)\s*\]\]", line)
        if m:
            cur = {}
            out.setdefault(m.group(1), []).append(cur)
            continue
        m = re.fullmatch(r"\[\s*([\w.-]+)\s*\]", line)
        if m:
            cur = out.setdefault(m.group(1), {})
            continue
        m = re.fullmatch(r"([\w-]+)\s*=\s*(.+?)\s*(#.*)?", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if value.startswith('"') and value.endswith('"'):
            cur[key] = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        elif value in ("true", "false"):
            cur[key] = value == "true"
        else:
            try:
                cur[key] = int(value)
            except ValueError:
                try:
                    cur[key] = float(value)
                except ValueError:
                    cur[key] = value
    return out


def fg_from_legacy(engine: Dict[str, Any], wrapper: Dict[str, Any]) -> Tuple[str, int]:
    """(fg mode, base fps) from a 2.x profile's conf.toml entry and launcher settings."""
    backend = str(wrapper.get("fg_backend") or "gfg")
    if backend in ("off", "native", "optiscaler"):
        return "off", 45
    try:
        cap = int(engine.get("base_fps_cap") or 0)
    except (TypeError, ValueError):
        cap = 0
    if engine.get("adaptive"):
        return "auto", 45
    if 20 <= cap <= 90:
        return "custom", cap
    try:
        mult = int(float(engine.get("multiplier") or 2))
    except (TypeError, ValueError):
        mult = 2
    if mult == 2:
        return "x2", 45
    if mult == 3:
        return "x3", 30
    return "auto", 45  # x4 and up do not exist in 3.0: Auto picks x2/x3


OBJECTIVE_FROM_MODE = {"budget": "battery", "balanced": "balanced", "quality": "quality",
                       "autopilot": "autopilot", "extreme": "autopilot"}


def objective_from_legacy(gov: Dict[str, Any]) -> str:
    if not isinstance(gov, dict) or not gov:
        return "autopilot"
    if not gov.get("enabled", False):
        return "quality"  # the Governor was off: the player's own TDP stays
    return OBJECTIVE_FROM_MODE.get(str(gov.get("mode")), "autopilot")


class Migration:
    def __init__(self, home: Path, data_dir: Path, owner: Optional[Tuple[int, int]] = None,
                 clock=time.time) -> None:
        self.home = Path(home)
        self.legacy = self.home / LEGACY_DATA_DIRNAME
        self.data = Path(data_dir)
        self.owner = owner
        self.clock = clock

    def needed(self, current: settings_mod.Settings) -> bool:
        return (not current.exists) and self.legacy.is_dir()

    def _json(self, name: str) -> Dict[str, Any]:
        try:
            value = json.loads((self.legacy / name).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, UnicodeError):
            return {}

    def backup(self) -> Optional[Path]:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self.clock()))
        dest = self.data / "backups" / f"gfg-2.x-backup-{stamp}.zip"
        ensure_dir(dest.parent, self.owner, stop=self.home)
        sources: List[Tuple[Path, str]] = []
        if self.legacy.is_dir():
            for p in sorted(self.legacy.rglob("*")):
                if p.is_file() and not p.is_symlink():
                    sources.append((p, "config/mako-render/" + str(p.relative_to(self.legacy))))
        for rel in (*LEGACY_WRAPPERS, ".local/bin/gfg-diagnostics"):
            p = self.home / rel
            if p.is_file() and not p.is_symlink():
                sources.append((p, rel))
        layers = self.home / ".local/share/vulkan/implicit_layer.d"
        if layers.is_dir():
            for p in sorted(layers.glob("VkLayer_*.json")):
                if p.is_file():
                    sources.append((p, f".local/share/vulkan/implicit_layer.d/{p.name}"))
        overrides = self.home / ".local/share/flatpak/overrides"
        if overrides.is_dir():
            for p in sorted(overrides.iterdir()):
                if p.is_file():
                    sources.append((p, f".local/share/flatpak/overrides/{p.name}"))
        if not sources:
            return None
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            for path, arc in sources:
                try:
                    if path.stat().st_size <= BACKUP_MAX_FILE:
                        z.write(path, arc)
                except OSError:
                    continue
            z.writestr("README.txt",
                       "GFG Extreme 2.x files saved by GFG Extreme 3.0 before its migration.\n"
                       "Paths are relative to the home folder. ~/.config/mako-render itself was not changed.\n")
        own(dest, self.owner)
        return dest

    def convert(self) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """(3.0 settings, report). Pure: reads the 2.x files, writes nothing."""
        report: Dict[str, Any] = {"profiles": [], "skipped": [], "notes": []}
        out = settings_mod.defaults()
        conf: Dict[str, Any] = {}
        try:
            conf = _parse_toml((self.legacy / "conf.toml").read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            report["notes"].append("No 2.x conf.toml: frame generation starts at Auto")
        engine_profiles = {str(p.get("name")): p for p in conf.get("profile", []) if isinstance(p, dict) and p.get("name")}
        wrapper = (self._json("profile-wrapper-settings.json").get("profiles") or {})
        meta = (self._json("profile-metadata.json").get("profiles") or {})
        governor = (self._json("gfg-governor.json").get("profiles") or {})
        names = list(dict.fromkeys([LEGACY_DEFAULT, *engine_profiles, *wrapper, *meta, *governor]))
        for name in names:
            eng = engine_profiles.get(name) or {}
            wrap = wrapper.get(name) if isinstance(wrapper.get(name), dict) else {}
            gov = governor.get(name) if isinstance(governor.get(name), dict) else {}
            info = meta.get(name) if isinstance(meta.get(name), dict) else {}
            if name == LEGACY_DEFAULT:
                key = settings_mod.DEFAULT_PROFILE
            else:
                key = settings_mod.profile_key(info.get("steam_app_id"))
                if key is None:
                    if eng or wrap or gov:
                        report["skipped"].append({"name": info.get("display_name") or name,
                                                  "reason": "no Steam app id (matched by process name in 2.x)"})
                    continue
            if not (eng or wrap or gov) and name != LEGACY_DEFAULT:
                continue
            fg, base = fg_from_legacy(eng, wrap)
            look = filters.from_legacy(wrap)
            objective = objective_from_legacy(gov)
            display = "Default" if key == settings_mod.DEFAULT_PROFILE else str(info.get("display_name") or name)
            out["profiles"][key] = settings_mod.normalize_profile(
                {"name": display, "fg": fg, "base_fps": base, "objective": objective, "filters": look}, display,
                schema=settings_mod.SCHEMA)
            report["profiles"].append({"from": name, "to": key, "name": display, "fg": fg, "base_fps": base,
                                       "objective": objective, "filters": filters.summary(look)})
        default_gov = governor.get(LEGACY_DEFAULT) if isinstance(governor.get(LEGACY_DEFAULT), dict) else {}
        pref = default_gov.get("autopilot_preference")
        if pref in settings_mod.AUTOPILOT_PREFERENCES:
            out["autopilot"]["preference"] = pref
        hud = default_gov.get("hud") if isinstance(default_gov.get("hud"), dict) else {}
        if hud:
            out["hud"]["enabled"] = bool(hud.get("enabled", True))
            if hud.get("position") in HUD_CORNERS:
                out["hud"]["position"] = hud["position"]
        gfg = self._json("gfg-governor.json")
        if gfg.get("power_split") is False:
            out["power"]["split"] = False
            report["notes"].append("Power Split stays off, as you had it in 2.x")
        if gfg.get("power_split_games"):
            report["notes"].append("Power Split's per-game measurements from 2.x are not used (measured "
                                   "with MAKO's costs); it measures again on the KEDAVRA points")
        if (self.legacy / "gfg-game-models.json").exists():
            report["notes"].append("Learned TDP points from 2.x are not used (measured with MAKO's costs); "
                                   "they are in the backup, and the Governor learns again")
        out["migrated_from"] = "2.x"
        return settings_mod.normalize(out), report

    def run(self, current: settings_mod.Settings) -> Dict[str, Any]:
        backup = self.backup()
        data, report = self.convert()
        current.replace(data)
        report["backup"] = str(backup) if backup else None
        report["time"] = self.clock()
        from .user_files import write_json_atomic
        write_json_atomic(self.data / "migration-report.json", report, self.owner)
        return report
