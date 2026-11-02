import asyncio
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

import helpers  # noqa: F401
sys.modules.setdefault('decky', types.SimpleNamespace(logger=Mock()))
from gfg_plugin.plugin import Plugin
from sandbox.run import make_rig, play
from sandbox.vdeck import VGame


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_startup_recovery_prevents_controller_initialization(self):
        from gfg_plugin import plugin as mod
        inst = Mock()
        inst.recover_pending.side_effect = RuntimeError("retained recovery journal")
        log = Mock()
        with patch.object(mod.decky, "DECKY_USER_HOME", "/home/deck", create=True), \
                patch.object(mod, "home_owner", return_value=None), \
                patch.object(mod, "ensure_dir"), \
                patch.object(mod, "ActivityLog", return_value=log), \
                patch.object(mod, "Installer", return_value=inst), \
                patch.object(mod, "Telemetry") as telemetry, \
                patch.object(mod, "Service") as service:
            p = Plugin()
            with self.assertRaisesRegex(RuntimeError, "retained recovery journal"):
                await p._main()
            telemetry.assert_not_called()
            service.assert_not_called()
            self.assertEqual(p.tasks, [])
            log.record.assert_called_once_with("installer", "recovery-failed", error="retained recovery journal")
            await p._unload()  # partially initialized backend must still unload safely

    async def test_startup_recovery_runs_once_and_reports_recovery(self):
        p = Plugin()
        p.log = Mock()
        p.inst = Mock()
        p.inst.recover_pending.return_value = True
        await p._recover_installer()
        p.inst.recover_pending.assert_called_once_with()
        p.log.record.assert_called_once_with("installer", "recovered")

    async def test_worker_preserves_keywords_and_errors(self):
        p = Plugin()
        self.assertEqual(await p._worker(lambda *, value: value, value=42), 42)
        def fail():
            raise ValueError("worker failure")
        with self.assertRaisesRegex(ValueError, "worker failure"):
            await p._worker(fail)

    async def test_unload_drains_tick_and_health_even_with_repeated_cancellation(self):
        for trial in range(200):
            with self.subTest(trial=trial):
                entered, release = threading.Event(), threading.Event()
                health_entered, health_release = threading.Event(), threading.Event()
                events = []
                def tick():
                    entered.set()
                    if not release.wait(5): raise RuntimeError('test timed out')
                    events.append('tick')
                    return {}
                def health():
                    health_entered.set()
                    if not health_release.wait(5): raise RuntimeError('test timed out')
                    events.append('health')
                p = Plugin()
                p.lock = asyncio.Lock(); p.stopping = False
                p.service = types.SimpleNamespace(tick=tick, stop=lambda: events.append('stop'))
                p.inst = types.SimpleNamespace(mark_healthy=health)
                p.log = Mock()
                p.tasks = [asyncio.create_task(p._loop()), asyncio.create_task(p._health())]
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                self.assertTrue(await asyncio.to_thread(health_entered.wait, 5))
                unloading = asyncio.create_task(p._unload())
                try:
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    for task in p.tasks: task.cancel()
                    await asyncio.sleep(0)
                    self.assertNotIn('stop', events)
                    self.assertFalse(unloading.done())
                finally:
                    release.set(); health_release.set()
                await asyncio.wait_for(unloading, 5)
                self.assertEqual(set(events[:2]), {'tick', 'health'})
                self.assertEqual(events[-1], 'stop')
                await p._unload()
                self.assertEqual(events.count('stop'), 1)
                self.assertTrue(all(t.done() for t in p.tasks))

    async def test_cancelled_unload_caller_does_not_cancel_shutdown(self):
        p = Plugin(); p.lock = asyncio.Lock(); p.stopping = False; p.tasks = []; p.log = Mock()
        entered, release = threading.Event(), threading.Event()
        def stop():
            entered.set()
            if not release.wait(5): raise RuntimeError('timeout')
        p.service = types.SimpleNamespace(stop=stop)
        task = asyncio.create_task(p._unload())
        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        release.set()
        await asyncio.wait_for(p._unload(), 5)
        p.log.record.assert_called_once_with('plugin', 'unload')

    async def test_uninstall_invalidates_caches_and_rejects_after_stop(self):
        p = Plugin(); p.lock = asyncio.Lock(); p.stopping = False; p.log = Mock()
        p._gate_cache = {'old': True}; p._host_cache = {'old': True}
        p.inst = types.SimpleNamespace(uninstall=Mock(return_value={'needs_reboot': True}))
        await p.engine_uninstall()
        self.assertEqual(p._gate_cache, {}); self.assertEqual(p._host_cache, {})
        p.stopping = True
        self.assertFalse((await p.engine_uninstall())['ok'])
        p.inst.uninstall.assert_called_once()


class ServiceLifecycleTest(unittest.TestCase):
    def test_stopped_service_never_reapplies_policy(self):
        rig = make_rig(VGame('g', 55.0))
        rig.settings.data['engine']['hdr_output'] = False
        play(rig, 5)
        rig.service.stop()
        self.assertEqual(rig.engine.vars.get('hdr_enabled'), '1')
        rig.service._tick = Mock(side_effect=AssertionError('tick after stop'))
        rig.service._stop = Mock(side_effect=AssertionError('duplicate stop'))
        for _ in range(200): rig.service.tick()
        rig.service.stop()
        rig.service._tick.assert_not_called()

    def test_failed_restorer_does_not_skip_other_restorers_or_restart_tick(self):
        rig = make_rig(VGame('g', 55.0)); s = rig.service
        s.boost = types.SimpleNamespace(stop=Mock(side_effect=OSError('boost failed')))
        s.gclock = types.SimpleNamespace(stop=Mock())
        with self.assertRaisesRegex(RuntimeError, 'boost failed'):
            s.stop()
        s.gclock.stop.assert_called_once()
        s._tick = Mock(side_effect=AssertionError('tick after failure'))
        s.tick()
        s._tick.assert_not_called()
        s.boost.stop.side_effect = None
        s.stop()  # Explicit retry can finish restoring, but never resumes ticks.
        self.assertTrue(s._stop_complete)

    def test_stop_waits_for_service_tick(self):
        rig = make_rig(VGame('g', 55.0)); s = rig.service
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        events = []
        def tick():
            entered.set()
            if not release.wait(5): raise RuntimeError('timeout')
            events.append('write')
            return {}
        s._tick = tick
        s._stop = lambda: (events.append('restore'), stopped.set())
        t = threading.Thread(target=s.tick); t.start()
        self.assertTrue(entered.wait(5))
        u = threading.Thread(target=s.stop); u.start()
        try: self.assertFalse(stopped.wait(.02))
        finally: release.set()
        t.join(5); u.join(5)
        self.assertEqual(events, ['write', 'restore'])
        s.tick(); self.assertEqual(events, ['write', 'restore'])
