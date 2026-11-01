# SPDX-License-Identifier: GPL-3.0-or-later
"""Install, rollback and diagnostics of the KEDAVRA Gamescope runtime, run from the
Decky backend (as root). Everything comes from the plugin zip: runtime/ holds the
patched gamescope, gamescopectl and their bundled libraries.

Install never modifies the read-only system image:
  * the runtime is copied to /var/lib/kedavra/runtime (writable, not nosuid);
  * a boot unit bind-mounts runtime/bin/gamescope over /usr/bin/gamescope before the
    Gaming Mode session starts; the mount lives in memory only;
  * boot watchdog: a boot that mounts KEDAVRA starts a "trial". If the patched
    Gamescope never writes its status file during that boot, the next boot skips
    the mount and records an automatic rollback. So a Gaming Mode that does not come
    up is fixed by holding the power button and rebooting.
"""
import base64
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

from .system_profile import LIMITS, LIMITS_NAME, restore_script, tune_script

UNIT_NAME = "kedavra-gamescope.service"
LOADER = "ld-linux-x86-64.so.2"
SYS_NICE_BIT = 23
DISPLAY_SCRIPTS = b"/usr/share/gamescope/scripts"

BOOT_SH = """#!/bin/sh
# KEDAVRA boot step (written by the Decky plugin). Bind-mounts the patched Gamescope
# over /usr/bin/gamescope for this boot only, with an automatic rollback guard.
S={state}
BIN={runtime}/bin/gamescope
LOADER={runtime}/lib/{loader}
mkdir -p "$S"
[ -f "$S/disabled" ] && exit 0
# A terminated update must boot stock Gamescope until recovery finishes.
[ -e "$S/../install-transaction.json" ] && exit 0
if [ -f "$S/trial" ] && [ ! -f "$S/healthy" ]; then
	date -u +%Y-%m-%dT%H:%M:%SZ > "$S/auto_rollback"
	echo "previous boot with KEDAVRA never became healthy" >> "$S/auto_rollback"
	touch "$S/disabled"
	rm -f "$S/trial"
	exit 0
fi
rm -f "$S/healthy"
[ -x "$BIN" ] && [ -x "$LOADER" ] || exit 0
if ! "$LOADER" --list "$BIN" > "$S/ldd" 2>&1 || grep -q 'not found' "$S/ldd"; then
	mv "$S/ldd" "$S/ldd_failed"
	exit 0
fi
"$BIN" --version > "$S/version" 2>&1 || exit 0
findmnt -n /usr/bin/gamescope >/dev/null 2>&1 || mount --bind "$BIN" /usr/bin/gamescope
touch "$S/trial"
"""

UNIT = """[Unit]
Description=KEDAVRA frame generation: patched Gamescope for this boot
Before=display-manager.service graphical.target
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/bin/sh {boot}
ExecStart=-/bin/sh {tune}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""


# Decky's own runtime sets these for its bundled libraries (an older libstdc++ among
# them). Inherited by our tools they override the runtime's RUNPATH: gamescopectl then
# fails with "undefined symbol ... GLIBCXX_3.4.35".
LOADER_VARS = ("LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT", "PYTHONHOME", "PYTHONPATH")


def clean_env(env=None):
    env = dict(os.environ if env is None else env)
    for k in LOADER_VARS:
        env.pop(k, None)
    return env


def _run(argv, timeout=20, env=None, **kw):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=clean_env(env), **kw)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


class Installer:
    def __init__(self, plugin_dir, home, uid=1000, root=Path("/"), run=_run):
        self.plugin_dir = Path(plugin_dir)
        self.bundle = self.plugin_dir / "runtime"
        self.home = Path(home)
        self.uid = uid
        self.root = Path(root)
        self.var = self.root / "var/lib/kedavra"
        self.runtime = self.var / "runtime"
        self.state = self.var / "state"
        self.unit = self.root / "etc/systemd/system" / UNIT_NAME
        self.boot = self.var / "boot.sh"
        # System profile (3.3.0): installed and removed with the engine, saved values per boot.
        self.tune = self.var / "profile.sh"
        self.untune = self.var / "profile-restore.sh"
        self.profile_orig = self.root / "run/kedavra/profile-orig"
        self.profile_done = self.root / "run/kedavra/profile-applied"
        self.limits = self.root / "etc/security/limits.d" / LIMITS_NAME
        self.target = self.root / "usr/bin/gamescope"
        self.status_file = self.root / f"run/user/{uid}/kedavra-status.json"
        self.run = run

    # ------------------------------------------------------------------ helpers

    def bundled_version(self):
        p = self.bundle / "VERSION"
        return p.read_text().strip() if p.exists() else "unknown"

    def installed_version(self):
        p = self.runtime / "VERSION"
        return p.read_text().strip() if p.exists() else None

    @staticmethod
    def _loader(runtime, *args):
        # The runtime carries its own glibc. The system ldd would check it against the
        # system glibc, so the bundled loader resolves it instead, from the runtime's own
        # lib/ (its interpreter and RUNPATH name the installed location only).
        lib = Path(runtime) / "lib"
        return [str(lib / LOADER), "--library-path", str(lib), *map(str, args)]

    @staticmethod
    def _fix_modes(runtime):
        # Decky unpacks the zip without file modes; the loader must be executable.
        for d in ("bin", "lib"):
            for f in (Path(runtime) / d).iterdir():
                f.chmod(0o755)

    def _missing_libs(self, runtime):
        runtime = Path(runtime)
        if not (runtime / "lib" / LOADER).exists():
            return [f"runtime has no lib/{LOADER}: the zip was built by an old version"]
        rc, out = self.run(self._loader(runtime, "--list", runtime / "bin/gamescope"))
        missing = [l.strip() for l in out.splitlines() if "not found" in l]
        if rc != 0 and not missing:
            missing = [out.strip()[-400:] or f"the loader exited with code {rc}"]
        return missing

    def _mounted(self):
        rc, out = self.run(["findmnt", "-n", "-o", "SOURCE", str(self.target)])
        return rc == 0 and out.strip() != ""

    def _gamescope_procs(self):
        procs = []
        proc = self.root / "proc"
        try:
            entries = list(proc.iterdir())
        except OSError:
            return procs
        for d in entries:
            if not d.name.isdigit():
                continue
            try:
                if (d / "comm").read_text().strip() not in ("gamescope", "gamescope-wl"):
                    continue
                st = os.stat(d / "exe")
                cap = 0
                for line in (d / "status").read_text().splitlines():
                    if line.startswith("CapEff:"):
                        cap = int(line.split()[1], 16)
                procs.append({"pid": int(d.name), "dev": st.st_dev, "ino": st.st_ino,
                              "sys_nice": bool(cap >> SYS_NICE_BIT & 1)})
            except (OSError, ValueError):
                continue
        return procs

    def _ours(self, p):
        try:
            st = os.stat(self.runtime / "bin/gamescope")
        except OSError:
            return False
        return p["dev"] == st.st_dev and p["ino"] == st.st_ino

    def status_age(self):
        try:
            return time.time() - self.status_file.stat().st_mtime
        except OSError:
            return None

    def mark_healthy(self):
        """Called periodically by the plugin: the patched Gamescope is writing status."""
        age = self.status_age()
        if age is not None and age < 5 and (self.state / "trial").exists():
            (self.state / "healthy").touch()
            return True
        return False

    # ------------------------------------------------------------------ actions

    @contextmanager
    def _install_lock(self):
        self.var.mkdir(parents=True, exist_ok=True)
        lock = self.var / "install.lock"
        if lock.is_symlink():
            raise RuntimeError("installer lock is a symlink")
        with lock.open("a") as stream:
            if os.name == "posix":
                import fcntl
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError("another installer operation is in progress") from exc
            yield  # closing releases the lock, including after process termination

    def _install_paths(self):
        return [self.boot, self.tune, self.untune, self.unit, self.limits] + [
            self.state / name for name in ("disabled", "trial", "healthy", "auto_rollback", "ldd_failed")]

    @staticmethod
    def _encode_snapshot(saved):
        kind, data, extra = saved
        if kind == "file":
            data = base64.b64encode(data).decode("ascii")
        elif kind == "symlink":
            extra = Installer._encode_snapshot(extra)
        return [kind, data, extra]

    @staticmethod
    def _decode_snapshot(saved):
        kind, data, extra = saved
        if kind == "file":
            data = base64.b64decode(data, validate=True)
            if len(extra) != 3 or not all(type(n) is int for n in extra):
                raise ValueError("invalid file metadata")
        elif kind == "symlink":
            if not isinstance(data, str):
                raise ValueError("invalid symlink")
            extra = Installer._decode_snapshot(extra)
        elif kind != "missing" or data is not None or extra is not None:
            raise ValueError("invalid snapshot")
        return kind, data, extra

    def _sync_install_dir(self):
        if os.name == "posix":
            fd = os.open(self.var, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _sync_runtime_tree(root):
        """Flush staged files (including setcap metadata), then directories.

        No symlink targets outside the staged tree are traversed. This is an
        installation-only operation, never a periodic backend task.
        """
        if os.name != "posix":
            return
        for directory, _, files in os.walk(root, topdown=False, followlinks=False):
            for name in files:
                path = Path(directory) / name
                if path.is_symlink():
                    continue
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise RuntimeError(f"unexpected runtime entry: {path}")
                    os.fsync(fd)
                finally:
                    os.close(fd)
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _save_install_transaction(self, transaction):
        pending = self.var / "install-transaction.new"
        if pending.is_symlink():
            raise RuntimeError("installer transaction staging path is a symlink")
        with pending.open("w", encoding="utf-8") as stream:
            pending.chmod(0o600)
            json.dump(transaction, stream)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(self.var / "install-transaction.json")
        self._sync_install_dir()

    def _recover_install_transaction(self):
        """Finish cleanup or roll back an interrupted attempt before starting another.

        This is restart/retry recovery, not a guarantee of boot or power-loss safety.
        Never infer ownership of a runtime.rollback tree without our journal.
        """
        journal = self.var / "install-transaction.json"
        if not journal.exists() and not journal.is_symlink():
            return
        if journal.is_symlink():
            raise RuntimeError("installer transaction is a symlink")
        try:
            tx = json.loads(journal.read_text(encoding="utf-8"))
            paths = self._install_paths()
            if (tx["version"] != 1 or type(tx["committed"]) is not bool
                    or type(tx["had_runtime"]) is not bool or len(tx["snapshots"]) != len(paths)
                    or tx["prior_enabled"] not in ("enabled", "enabled-runtime", "disabled", "not-found")):
                raise ValueError("invalid transaction fields")
            snapshots = [self._decode_snapshot(s) for s in tx["snapshots"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("invalid installer transaction; recovery data retained") from exc
        backup = self.var / "runtime.rollback"
        staged = self.var / "runtime.new"
        for tree in (backup, staged, self.runtime):
            if tree.is_symlink():
                raise RuntimeError(f"installer recovery refuses runtime symlink: {tree}")
        if not tx["committed"]:
            if tx["had_runtime"]:
                if backup.exists():
                    if self.runtime.exists():
                        shutil.rmtree(self.runtime)
                    backup.rename(self.runtime)
                    self._sync_install_dir()
                elif not self.runtime.exists():
                    raise RuntimeError("previous runtime missing; recovery journal retained")
                # No backup means the first rename did not happen, or rollback already did.
            elif self.runtime.exists():
                shutil.rmtree(self.runtime)
            prior = tx["prior_enabled"]
            _, current = self.run(["systemctl", "is-enabled", UNIT_NAME])
            # Remove partial enablement while the candidate unit still exists.
            if prior in ("disabled", "not-found", "enabled-runtime") and current.strip() not in ("disabled", "not-found"):
                rc, out = self.run(["systemctl", "disable", UNIT_NAME])
                if rc:
                    raise RuntimeError("restore service state: " + out.strip()[-300:])
            for path, saved in zip(paths, snapshots):
                self._restore_file(path, saved)
            rc, out = self.run(["systemctl", "daemon-reload"])
            if rc:
                raise RuntimeError("reload restored unit: " + out.strip()[-300:])
            _, current = self.run(["systemctl", "is-enabled", UNIT_NAME])
            if current.strip() != prior and not (prior == "not-found" and current.strip() == "disabled"):
                commands = ([["systemctl", "disable", UNIT_NAME], ["systemctl", "enable", "--runtime", UNIT_NAME]]
                            if prior == "enabled-runtime" else
                            [["systemctl", "enable" if prior == "enabled" else "disable", UNIT_NAME]])
                for argv in commands:
                    rc, out = self.run(argv)
                    if rc:
                        raise RuntimeError("restore service state: " + out.strip()[-300:])
        else:
            # Service setup committed successfully. The new boot script also
            # checks the journal, so it remains guarded throughout cleanup.
            (self.state / "disabled").unlink(missing_ok=True)
            if backup.exists():
                shutil.rmtree(backup)
        if staged.exists():
            shutil.rmtree(staged)
        journal.unlink()
        self._sync_install_dir()

    @staticmethod
    def _snapshot_file(path):
        """Snapshot an installer-owned file without following symlinks."""
        if path.is_symlink():
            return ("symlink", os.readlink(path), Installer._snapshot_file(path.resolve(strict=False)))
        if not path.exists():
            return ("missing", None, None)
        if not path.is_file():
            raise RuntimeError(f"installer path is not a file: {path}")
        info = path.stat()
        return ("file", path.read_bytes(), (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid))

    @staticmethod
    def _write_file_atomic(path, data, metadata=None):
        """Persist a complete replacement before exposing it to boot/systemd.

        Keep existing symlink representation and target metadata, matching the
        installer's snapshot/restore contract. This alone does not flush runtime
        libraries or make the entire transaction power-loss safe.
        """
        target = path.resolve(strict=False) if path.is_symlink() else path
        previous = target.stat() if target.exists() else None
        mode, uid, gid = metadata if metadata is not None else (
            (stat.S_IMODE(previous.st_mode), previous.st_uid, previous.st_gid)
            if previous else (0o644, None, None))
        pending = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".kedavra-", delete=False) as stream:
                pending = Path(stream.name)
                stream.write(data)
                stream.flush()
                if uid is not None and hasattr(os, "chown"):
                    current = pending.stat()
                    if (current.st_uid, current.st_gid) != (uid, gid):
                        os.chown(pending, uid, gid)
                pending.chmod(mode)
                os.fsync(stream.fileno())
            pending.replace(target)
            if os.name == "posix":
                fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)

    @staticmethod
    def _restore_file(path, saved):
        kind, data, extra = saved
        if kind == "symlink":
            if not path.is_symlink() or os.readlink(path) != data:
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.exists():
                    raise RuntimeError(f"cannot restore installer symlink over directory: {path}")
                path.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(data, path)
            Installer._restore_file(path.resolve(strict=False), extra)
            return
        if kind == "missing":
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                raise RuntimeError(f"cannot remove installer file over directory: {path}")
            return
        if path.is_symlink():
            path.unlink()
        elif path.exists() and not path.is_file():
            raise RuntimeError(f"cannot restore installer file over directory: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        Installer._write_file_atomic(path, data, metadata=extra)

    def install(self):
        with self._install_lock():
            return self._install_locked()

    def recover_pending(self):
        """Recover once at backend startup, without installing the bundled version.

        An ordinary startup performs no systemd calls or filesystem writes.
        Pending transactions use the same cross-process lock as Install/Remove.
        Errors propagate to the caller; recovery metadata is retained for diagnosis.
        """
        journal = self.var / "install-transaction.json"
        if not journal.exists() and not journal.is_symlink():
            return False
        with self._install_lock():
            self._recover_install_transaction()
        return True

    def _install_locked(self):
        # Recovery cannot own a symlinked runtime. Reject it before creating a
        # journal or touching staging, including dangling symlinks.
        for tree in (self.runtime, self.var / "runtime.new", self.var / "runtime.rollback"):
            if tree.is_symlink():
                raise RuntimeError(f"installer refuses runtime symlink: {tree}")
        self._recover_install_transaction()
        if not (self.bundle / "bin/gamescope").exists():
            raise RuntimeError("the plugin zip has no runtime/bin/gamescope")
        if not self.target.exists():
            raise RuntimeError(f"{self.target} not found: is this SteamOS Gaming Mode?")
        self.var.mkdir(parents=True, exist_ok=True)
        tmp = self.var / "runtime.new"
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(self.bundle, tmp)
        self._fix_modes(tmp)
        missing = self._missing_libs(tmp)
        if missing:
            shutil.rmtree(tmp)
            raise RuntimeError("Gamescope KEDAVRA would not start, missing libraries: " + "; ".join(missing[:6]))
        rc, out = self.run(self._loader(tmp, tmp / "bin/gamescope", "--version"))
        if rc != 0:
            shutil.rmtree(tmp)
            raise RuntimeError("Gamescope KEDAVRA --version failed: " + out.strip()[-400:])
        # Apply capabilities before replacing an existing runtime. File capabilities stay
        # attached to the staged executable when its directory is renamed on the same filesystem.
        rc, out = self.run(["setcap", "cap_sys_nice=eip", str(tmp / "bin/gamescope")])
        if rc != 0:
            shutil.rmtree(tmp)
            raise RuntimeError("setcap: " + out.strip()[-300:])
        try:
            self._sync_runtime_tree(tmp)
            self._sync_install_dir()
        except Exception:
            shutil.rmtree(tmp)
            raise
        backup = self.var / "runtime.rollback"
        if backup.exists() or backup.is_symlink():
            shutil.rmtree(tmp)
            raise RuntimeError(f"previous runtime rollback still exists: {backup}")
        paths = self._install_paths()
        try:
            snapshots = {path: self._snapshot_file(path) for path in paths}
            if self.unit.exists() or self.unit.is_symlink():
                _, prior_enabled = self.run(["systemctl", "is-enabled", UNIT_NAME])
                prior_enabled = prior_enabled.strip()
            else:
                prior_enabled = "not-found"
            if prior_enabled not in ("enabled", "enabled-runtime", "disabled", "not-found"):
                raise RuntimeError("cannot safely establish prior service state: " + prior_enabled[-300:])
        except Exception:
            shutil.rmtree(tmp)
            raise
        transaction = {"version": 1, "committed": False, "had_runtime": self.runtime.exists(),
                       "prior_enabled": prior_enabled,
                       "snapshots": [self._encode_snapshot(snapshots[p]) for p in paths]}
        self._save_install_transaction(transaction)
        try:
            # Historical boot scripts already honor this marker. Persist it
            # before the first runtime rename, including upgrades from 3.2.2.
            self.state.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.state, prefix="disabled-", delete=False) as stream:
                marker = Path(stream.name)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                marker.replace(self.state / "disabled")
            finally:
                marker.unlink(missing_ok=True)
            if os.name == "posix":
                fd = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self._sync_install_dir()
            # Keep the old tree until every mandatory setup action has succeeded.
            if self.runtime.exists():
                self.runtime.rename(backup)
                self._sync_install_dir()
            tmp.rename(self.runtime)
            self._sync_install_dir()
            self.state.mkdir(parents=True, exist_ok=True)
            for f in ("trial", "healthy", "auto_rollback", "ldd_failed"):
                (self.state / f).unlink(missing_ok=True)
            self._write_file_atomic(self.boot, BOOT_SH.format(state=self.state, runtime=self.runtime, loader=LOADER).encode())
            self._write_file_atomic(self.tune, tune_script(self.state, self.profile_orig, self.profile_done).encode())
            self._write_file_atomic(self.untune, restore_script(self.profile_orig, self.profile_done).encode())
            self.unit.parent.mkdir(parents=True, exist_ok=True)
            self._write_file_atomic(self.unit, UNIT.format(boot=self.boot, tune=self.tune).encode())
            try:
                self.limits.parent.mkdir(parents=True, exist_ok=True)
                self._write_file_atomic(self.limits, LIMITS.encode())
            except OSError:
                pass
            rc, out = self.run(["systemctl", "daemon-reload"])
            if rc != 0:
                raise RuntimeError("systemctl daemon-reload: " + out.strip()[-300:])
            rc, out = self.run(["systemctl", "enable", UNIT_NAME])
            if rc != 0:
                raise RuntimeError("systemctl enable: " + out.strip()[-300:])
        except Exception as exc:
            try:
                self._recover_install_transaction()
            except Exception as restore_error:
                raise RuntimeError(f"{exc}; rollback incomplete: {restore_error}") from exc
            raise
        transaction["committed"] = True
        self._save_install_transaction(transaction)
        self._recover_install_transaction()  # committed: only remove obsolete backups
        # Do not change runtime kernel settings before the service setup succeeds.
        self.run(["sh", str(self.tune)])
        return {"installed": self.bundled_version(), "needs_reboot": True,
                "message": "Installed. Reboot the Deck: Gaming Mode will start with Gamescope KEDAVRA."}

    def uninstall(self):
        with self._install_lock():
            self._recover_install_transaction()
            return self._uninstall_locked()

    def _uninstall_locked(self):
        restore_error = ""
        if self.profile_orig.exists():
            # An installed script may predate this fix. Always use the current restorer.
            self.untune.write_text(restore_script(self.profile_orig, self.profile_done))
            rc, output = self.run(["sh", str(self.untune)])
            if rc:
                restore_error = output or "System profile restoration failed; saved settings retained for retry."
        self.limits.unlink(missing_ok=True)
        self.run(["systemctl", "disable", UNIT_NAME])
        self.unit.unlink(missing_ok=True)
        self.run(["systemctl", "daemon-reload"])
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "disabled").touch()
        mounted = self._mounted()
        if restore_error:
            return {"ok": False, "needs_reboot": True, "error": restore_error,
                    "message": "Engine disabled. Some system settings could not be restored; retry Remove or reboot.",
                    "log": restore_error}
        return {"ok": True, "needs_reboot": mounted,
                "message": "Removed. Reboot the Deck to return to the stock Gamescope." if mounted
                else "Removed. The stock Gamescope is active."}

    @staticmethod
    def _finds_display_scripts(binary):
        """True when the build looks for /usr/share/gamescope/scripts, where SteamOS keeps the
        Steam Deck display scripts (the OLED one turns HDR on). A /usr/local build never does."""
        try:
            return DISPLAY_SCRIPTS in Path(binary).read_bytes()
        except OSError:
            return False

    def reboot(self):
        self.run(["systemctl", "reboot"])
        return {"rebooting": True}

    # ------------------------------------------------------------------ diagnostics

    def diagnose(self):
        checks = []

        def add(key, ok, text, detail=""):
            checks.append({"key": key, "ok": ok, "text": text, "detail": detail})

        transaction = self.var / "install-transaction.json"
        if transaction.exists() or transaction.is_symlink():
            add("install_transaction", False,
                "Installation has not finished. Wait for the current operation; if it stopped, "
                "the next Install or Remove will attempt recovery.")
        rc, osr = self.run(["cat", str(self.root / "etc/os-release")])
        osv = {k: v.strip('"') for k, _, v in (l.partition("=") for l in osr.splitlines()) if k}
        add("os", osv.get("ID") == "steamos", f"OS: {osv.get('PRETTY_NAME', '?')} {osv.get('VERSION_ID', '')} {osv.get('BUILD_ID', '')}")
        rc, pkg = self.run(["pacman", "-Q", "gamescope", "mesa", "vulkan-radeon"])
        add("packages", rc == 0, "Packages: " + " · ".join(pkg.split("\n")[:3]))

        bundled = self.bundle / "bin/gamescope"
        try:
            self._fix_modes(self.bundle)
        except OSError:
            pass
        add("bundle", bundled.exists(), f"Plugin carries Gamescope KEDAVRA {self.bundled_version()}"
            if bundled.exists() else "The plugin zip has no runtime/: build the full zip")
        if bundled.exists():
            missing = self._missing_libs(self.bundle)
            add("libs", not missing, "Libraries on this SteamOS: all found" if not missing
                else "Missing libraries", "\n".join(missing))

        inst = self.installed_version()
        add("installed", inst is not None, f"Installed: {inst}" if inst else "Not installed")
        if inst and inst != self.bundled_version():
            add("outdated", False, f"Installed Gamescope {inst}, plugin carries {self.bundled_version()}: "
                "press Reinstall and reboot")
        if inst:
            missing = self._missing_libs(self.runtime)
            add("installed_libs", not missing, "Installed Gamescope finds its libraries" if not missing
                else "Installed Gamescope is missing libraries", "\n".join(missing))
        if inst:
            ok = self._finds_display_scripts(self.runtime / "bin/gamescope")
            add("scripts", ok, "Gamescope reads SteamOS's display scripts (Deck OLED HDR)" if ok
                else "Gamescope does not read SteamOS's display scripts: no HDR on Deck OLED. "
                "Install the runtime from GFG Extreme 3.0.2 or newer")
        rc, en = self.run(["systemctl", "is-enabled", UNIT_NAME])
        add("unit", en.strip() == "enabled", f"Boot step: {en.strip() or 'none'}")
        for f, text in (("auto_rollback", "An automatic rollback happened"), ("ldd_failed", "Libraries were missing at boot")):
            p = self.state / f
            if p.exists():
                add(f, False, text, p.read_text()[-600:])
        if (self.state / "disabled").exists():
            add("disabled", False, "KEDAVRA is disabled until reinstalled")

        if inst:
            try:
                done = dict(kv.split("=", 1) for kv in self.profile_done.read_text().split())
                add("profile", done.get("failed") == "0", f"System profile: {done.get('applied', '?')} kernel settings for games"
                    + ("" if done.get("failed") == "0" else f", {done.get('failed')} refused"))
            except (OSError, ValueError):
                add("profile", False, "System profile not applied in this boot (reinstall and reboot)")
        mounted = self._mounted()
        add("mounted", mounted, "Gamescope KEDAVRA is mounted for this boot" if mounted
            else "The stock Gamescope is active now (reboot after installing)")
        procs = self._gamescope_procs()
        if not procs:
            add("running", False, "No gamescope process found")
        for p in procs:
            ours = self._ours(p)
            add("running", ours, f"gamescope pid {p['pid']}: " + ("KEDAVRA" if ours else "stock"))
            add("sys_nice", p["sys_nice"], "CAP_SYS_NICE present (realtime GPU queue)" if p["sys_nice"]
                else "No CAP_SYS_NICE: the GPU queue is not realtime")
        age = self.status_age()
        add("status", age is not None and age < 3, "Engine status is fresh" if age is not None and age < 3
            else ("Engine status is %.0f s old" % age if age is not None else "No engine status file"))
        return {"checks": checks, "ok": all(c["ok"] for c in checks if c["key"] in ("libs", "mounted", "running", "status"))}

    def report(self, out_dir, dest_dir=None, name="GFG-Extreme-report", extra=()):
        """Diagnostics text in out_dir, plus one zip for sending on the Desktop (dest_dir):
        the diagnostics, the latest pacing traces with their analyses and the benchmark."""
        d = self.diagnose()
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        path = out_dir / f"diagnostics-{stamp}.txt"
        lines = [("OK  " if c["ok"] else "FAIL ") + c["text"] + (("\n     " + c["detail"].replace("\n", "\n     ")) if c["detail"] else "")
                 for c in d["checks"]]
        # A Gaming Mode restart is a Gamescope (or Steam) crash: its core dump record has the
        # stack, and the kernel log tells a GPU reset from a crash in the compositor.
        for argv in (["findmnt", "-n", str(self.target)], ["journalctl", "-b", "-u", UNIT_NAME, "--no-pager"],
                     ["journalctl", "-b", "--no-pager", "-n", "300", "-g", "gamescope|kfg"],
                     ["coredumpctl", "--no-pager", "list"],
                     ["coredumpctl", "--no-pager", "info", "gamescope"],
                     ["journalctl", "-b", "-k", "--no-pager", "-n", "80", "-g", "amdgpu|drm|gpu|segfault"]):
            rc, out = self.run(argv)
            lines.append(f"\n$ {' '.join(argv)}\n{out[-6000:]}")
        st = self.status_file
        if st.exists():
            lines.append("\nstatus: " + st.read_text())
        path.write_text("\n".join(lines) + "\n")
        self._own(out_dir, path)
        dest = Path(dest_dir) if dest_dir else self.home / "Desktop"
        try:
            dest.mkdir(parents=True, exist_ok=True)
            bundle = dest / f"{name}-{stamp}.zip"
            traces = sorted((out_dir / "captures").glob("*.csv"), reverse=True)[:3]
            with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(path, path.name)
                for t in traces:
                    z.write(t, "captures/" + t.name)
                    if t.with_suffix(".json").exists():
                        z.write(t.with_suffix(".json"), "captures/" + t.with_suffix(".json").name)
                if (out_dir / "bench.json").exists():
                    z.write(out_dir / "bench.json", "bench.json")
                for arc, src in extra:
                    try:
                        z.write(src, arc)
                    except OSError:
                        pass
            self._own(dest, bundle)
        except OSError:
            # No Desktop folder we can write: the text report in out_dir still exists.
            return {"path": str(path), "diagnose": d}
        return {"path": str(bundle), "diagnose": d}

    def _own(self, *paths):
        for p in paths:
            try:
                os.chown(p, self.uid, self.uid)
            except OSError:
                pass


def bench(plugin_dir, out_dir, run=_run):
    b = Path(plugin_dir) / "bench"
    out = Path(out_dir) / "bench.json"
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    try:
        (b / "kfg_vktest").chmod(0o755)  # Decky unpacks the zip without file modes
    except OSError:
        return {"ok": False, "log": "the plugin zip has no bench/kfg_vktest", "json": ""}
    rc, text = run([str(b / "kfg_vktest"), "--spv", str(b), "--bench", "200", "--json", str(out)], timeout=600)
    summary = {"ok": rc == 0, "log": text[-3000:], "json": str(out)}
    for line in text.splitlines():
        if line.startswith("analysis "):
            parts = line.split()
            try:
                summary["analysis_ms"] = float(parts[1])
                summary["two_outputs_ms"] = float(parts[4])
            except (IndexError, ValueError):
                pass
        if line.startswith("PASS:"):
            summary["correct"] = "0 failure" in line
    try:
        summary["device"] = json.loads(out.read_text()).get("device")
    except (OSError, ValueError):
        pass
    return summary
