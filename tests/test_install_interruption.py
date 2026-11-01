"""Interrupted installer recovery on a temporary root; never touches host systemd."""
import unittest
import shutil
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import test_kedavra_install as fixture


class InterruptedInstallTest(unittest.TestCase):
    def setUp(self):
        self.rig = fixture.InstallerTest()
        self.rig.setUp()
        self.addCleanup(self.rig.tearDown)

    def test_runtime_symlink_is_rejected_before_transaction_or_staging(self):
        is_symlink = Path.is_symlink
        for name in ("runtime", "runtime.new", "runtime.rollback"):
            with self.subTest(name=name):
                rig = fixture.InstallerTest()
                rig.setUp()
                self.addCleanup(rig.tearDown)
                rig.inst.install()
                original = (rig.inst.runtime / "bin/gamescope").read_bytes()
                suspect = rig.inst.var / name
                with patch.object(Path, "is_symlink", lambda p: p == suspect or is_symlink(p)):
                    with self.assertRaisesRegex(RuntimeError, "runtime symlink"):
                        rig.inst.install()
                self.assertEqual((rig.inst.runtime / "bin/gamescope").read_bytes(), original)
                self.assertFalse((rig.inst.var / "install-transaction.json").exists())
                self.assertFalse((rig.inst.var / "runtime.new").exists())
                self.assertFalse((rig.inst.var / "runtime.rollback").exists())

    def test_startup_without_transaction_does_not_create_installer_files(self):
        rig = self.rig
        self.assertFalse(rig.inst.recover_pending())
        self.assertFalse(rig.inst.var.exists())
        self.assertEqual(rig.sys.calls, [])

    def test_startup_recovery_restores_old_runtime_without_installing_bundle(self):
        rig = self.rig
        rig.inst.install()
        (rig.inst.runtime / "bin/gamescope").write_text("previous-runtime")
        (rig.inst.bundle / "bin/gamescope").write_text("candidate-runtime")
        def interrupt(argv, **kw):
            if argv[:2] == ["systemctl", "enable"]:
                raise SystemExit("interrupted before commit")
            return rig.sys(argv, **kw)
        rig.inst.run = interrupt
        with self.assertRaises(SystemExit):
            rig.inst.install()
        fresh = fixture.kedavra_install.Installer(rig.inst.plugin_dir, rig.inst.home, root=rig.root, run=rig.sys)
        rig.sys.calls.clear()
        self.assertTrue(fresh.recover_pending())
        self.assertEqual((fresh.runtime / "bin/gamescope").read_text(), "previous-runtime")
        self.assertFalse(any(c[0] in ("setcap", "sh") for c in rig.sys.calls))
        self.assertFalse(fresh.recover_pending())

    def test_retry_after_interruption_between_runtime_renames(self):
        rig = self.rig
        rig.inst.install()
        (rig.inst.runtime / "bin/gamescope").write_text("previous-runtime")
        (rig.inst.bundle / "bin/gamescope").write_text("candidate-runtime")
        staged = rig.inst.var / "runtime.new"
        rename = type(staged).rename

        def interrupt(path, dest):
            if path == staged:
                raise SystemExit("simulated abrupt process termination")
            return rename(path, dest)

        with patch.object(type(staged), "rename", interrupt):
            with self.assertRaises(SystemExit):
                rig.inst.install()
        self.assertEqual((rig.inst.var / "runtime.rollback/bin/gamescope").read_text(), "previous-runtime")
        self.assertTrue((rig.inst.state / "disabled").is_file(), "historical boot must skip interrupted runtime")
        # A new installer instance must recover without manual file operations.
        fresh = fixture.kedavra_install.Installer(rig.inst.plugin_dir, rig.inst.home, root=rig.root, run=rig.sys)
        fresh.install()
        self.assertEqual((fresh.runtime / "bin/gamescope").read_text(), "candidate-runtime")
        self.assertFalse((fresh.var / "runtime.rollback").exists())
        self.assertFalse((fresh.state / "disabled").exists())

    def test_interrupted_enable_restores_previous_files_before_retry(self):
        rig = self.rig
        rig.inst.install()
        (rig.inst.runtime / "bin/gamescope").write_text("previous-runtime")
        (rig.inst.bundle / "bin/gamescope").write_text("candidate-runtime")
        (rig.inst.state / "healthy").write_text("previous healthy")
        paths = rig.inst._install_paths()
        before = {p: p.read_bytes() if p.exists() else None for p in paths}

        def interrupt(argv, **kw):
            result = rig.sys(argv, **kw)
            if argv[:2] == ["systemctl", "enable"]:
                raise SystemExit("terminated after service enablement")
            return result

        rig.inst.run = interrupt
        with self.assertRaises(SystemExit):
            rig.inst.install()
        def reject_retry(argv, **kw):
            if argv[0] == "setcap":
                return 1, "reject next candidate"
            return rig.sys(argv, **kw)
        fresh = fixture.kedavra_install.Installer(rig.inst.plugin_dir, rig.inst.home, root=rig.root, run=reject_retry)
        with self.assertRaisesRegex(RuntimeError, "setcap"):
            fresh.install()
        self.assertEqual((fresh.runtime / "bin/gamescope").read_text(), "previous-runtime")
        self.assertEqual({p: p.read_bytes() if p.exists() else None for p in paths}, before)
        self.assertTrue(rig.sys.enabled)
        self.assertFalse((fresh.var / "install-transaction.json").exists())

    def test_interrupted_first_install_restores_absence_and_service_state(self):
        rig = self.rig
        def interrupt(argv, **kw):
            result = rig.sys(argv, **kw)
            if argv[:2] == ["systemctl", "enable"]:
                raise SystemExit("terminated after first enablement")
            return result
        rig.inst.run = interrupt
        with self.assertRaises(SystemExit):
            rig.inst.install()
        rig.inst.run = rig.sys
        rig.inst._recover_install_transaction()
        rig.inst._recover_install_transaction()  # recovery is idempotent
        self.assertFalse(rig.inst.runtime.exists())
        self.assertFalse(rig.inst.unit.exists())
        self.assertFalse(rig.sys.enabled)
        self.assertFalse(any(c[0] == "sh" for c in rig.sys.calls))

    def test_committed_cleanup_does_not_restore_old_runtime(self):
        rig = self.rig
        rig.inst.install()
        (rig.inst.runtime / "bin/gamescope").write_text("previous-runtime")
        (rig.inst.bundle / "bin/gamescope").write_text("candidate-runtime")
        backup = rig.inst.var / "runtime.rollback"
        original = shutil.rmtree
        def interrupt(path, *args, **kwargs):
            if Path(path) == backup:
                raise SystemExit("terminated during committed cleanup")
            return original(path, *args, **kwargs)
        with patch.object(shutil, "rmtree", interrupt):
            with self.assertRaises(SystemExit):
                rig.inst.install()
        rig.inst._recover_install_transaction()
        self.assertEqual((rig.inst.runtime / "bin/gamescope").read_text(), "candidate-runtime")
        self.assertFalse(backup.exists())

    def test_corrupt_journal_preserves_runtime_and_backup(self):
        rig = self.rig
        rig.inst.install()
        backup = rig.inst.var / "runtime.rollback"
        shutil.copytree(rig.inst.runtime, backup)
        journal = rig.inst.var / "install-transaction.json"
        journal.write_text('{"version": 1}')
        with self.assertRaisesRegex(RuntimeError, "invalid installer transaction"):
            rig.inst.install()
        self.assertTrue(journal.exists())
        self.assertTrue((backup / "bin/gamescope").exists())
        self.assertTrue((rig.inst.runtime / "bin/gamescope").exists())
        before = journal.read_bytes()
        checks = rig.inst.diagnose()["checks"]
        self.assertFalse(next(c for c in checks if c["key"] == "install_transaction")["ok"])
        self.assertEqual(journal.read_bytes(), before, "diagnostics must not consume recovery data")

    def test_recovery_can_resume_after_another_interruption(self):
        rig = self.rig
        rig.inst.install()
        (rig.inst.runtime / "bin/gamescope").write_text("previous-runtime")
        (rig.inst.bundle / "bin/gamescope").write_text("candidate-runtime")
        def interrupt(argv, **kw):
            if argv[:2] == ["systemctl", "enable"]:
                raise SystemExit("interrupted installation")
            return rig.sys(argv, **kw)
        rig.inst.run = interrupt
        with self.assertRaises(SystemExit):
            rig.inst.install()
        with patch.object(rig.inst, "_restore_file", side_effect=SystemExit("interrupted recovery")):
            with self.assertRaises(SystemExit):
                rig.inst._recover_install_transaction()
        self.assertEqual((rig.inst.runtime / "bin/gamescope").read_text(), "previous-runtime")
        rig.inst.run = rig.sys
        rig.inst._recover_install_transaction()
        self.assertEqual((rig.inst.runtime / "bin/gamescope").read_text(), "previous-runtime")
        self.assertFalse((rig.inst.var / "install-transaction.json").exists())

    def test_abrupt_child_exit_releases_lock_and_allows_recovery(self):
        rig = self.rig
        rig.inst.install()
        (rig.inst.runtime / "bin/gamescope").write_text("previous-runtime")
        (rig.inst.bundle / "bin/gamescope").write_text("candidate-runtime")
        code = '''
import os, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import test_kedavra_install as fixture
system = fixture.FakeSystem()
system.enabled = True
inst = fixture.kedavra_install.Installer(sys.argv[2], sys.argv[3], root=Path(sys.argv[4]), run=system)
staged = inst.var / "runtime.new"
rename = type(staged).rename
def stop(path, destination):
    if path == staged:
        os._exit(77)
    return rename(path, destination)
with patch.object(type(staged), "rename", stop):
    inst.install()
'''
        result = subprocess.run([sys.executable, "-B", "-c", code, str(Path(__file__).parent),
                                 str(rig.inst.plugin_dir), str(rig.inst.home), str(rig.root)], timeout=30)
        self.assertEqual(result.returncode, 77)
        self.assertFalse(rig.inst.runtime.exists())
        self.assertTrue((rig.inst.state / "disabled").is_file())
        rig.inst.install()
        self.assertEqual((rig.inst.runtime / "bin/gamescope").read_text(), "candidate-runtime")

    def test_concurrent_operation_cannot_recover_active_transaction(self):
        if os.name != "posix":
            self.skipTest("flock is a POSIX/SteamOS check; Windows uses a fake system only")
        rig = self.rig
        with rig.inst._install_lock():
            with self.assertRaisesRegex(RuntimeError, "another installer operation"):
                rig.inst.install()
            with self.assertRaisesRegex(RuntimeError, "another installer operation"):
                rig.inst.uninstall()

    @unittest.skipUnless(os.name == "posix", "boot shell execution requires POSIX; exercised by Linux CI")
    def test_boot_skips_pending_transaction_before_runtime_execution(self):
        rig = self.rig
        rig.inst.install()
        reached = rig.tmp / "runtime-executed"
        # If a guard fails, the fake loader records it and exits before any mount.
        loader = rig.inst.runtime / "lib" / fixture.kedavra_install.LOADER
        loader.write_text(f"#!/bin/sh\ntouch '{reached}'\nexit 1\n")
        loader.chmod(0o755)
        for guard in (rig.inst.state / "disabled", rig.inst.var / "install-transaction.json"):
            with self.subTest(guard=guard.name):
                guard.write_text("pending")
                subprocess.run(["sh", str(rig.inst.boot)], check=True, timeout=5)
                self.assertFalse(reached.exists())
                self.assertFalse((rig.inst.state / "trial").exists())
                guard.unlink()
        subprocess.run(["sh", str(rig.inst.boot)], check=True, timeout=5)
        self.assertTrue(reached.exists(), "control run must reach the fake runtime")


if __name__ == "__main__":
    unittest.main()
