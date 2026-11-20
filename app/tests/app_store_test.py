"""Persistent app availability and side-effect isolation; all device calls mocked."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
from app_store import AppStore, is_installed
from clock import ClockService

class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.json'
        self.engine = Mock()
        self.engine.config = {'miniapps': {'voice_phrases': {'network_speed': ['看网速']}},
                              'weather': {'enabled': True, 'city': '沈阳市'},
                              'clock': {'face': 'forest'}}
        self.engine._save_config.side_effect = lambda **kwargs: self.path.write_text(json.dumps(self.engine.config))
        self.engine.miniapps.current_command.return_value = None
        self.store = AppStore(self.engine)

    def test_defaults_preserve_existing_apps(self):
        apps = self.store.status()['apps']
        self.assertEqual(len(apps), 6)
        self.assertEqual([a['id'] for a in apps if not a['installed']], ['bambu_print'])

    def test_remove_restore_persist_and_preserve_configuration(self):
        self.store.set_installed('network_speed', False)
        saved = json.loads(self.path.read_text())
        self.assertFalse(saved['app_store']['installed']['network_speed'])
        self.assertEqual(saved['miniapps']['voice_phrases']['network_speed'], ['看网速'])
        self.engine.config = saved
        self.assertFalse(is_installed(self.engine, 'network_speed'))
        self.store.set_installed('network_speed', True)
        self.assertTrue(is_installed(self.engine, 'network_speed'))
        self.assertEqual(saved['weather']['city'], '沈阳市')

    def test_removed_query_cannot_reach_data_provider(self):
        from miniapps.service import MiniAppService
        self.engine.config['app_store'] = {'installed': {'network_speed': False}}
        service = MiniAppService(self.engine)
        with patch.object(service, '_compute') as compute:
            with self.assertRaises(ValueError):
                service.query('network_speed', service.default_operation('network_speed'))
            self.assertIsNone(service.handle_text('查询网速'))
            compute.assert_not_called()

    def test_clock_removal_stops_device_before_committing(self):
        self.store.set_installed('clock', False)
        self.engine.clock.set_available.assert_called_once_with(False)
        self.assertFalse(is_installed(self.engine, 'clock'))
        self.assertEqual(self.engine.config['clock']['face'], 'forest')

    def test_device_failure_keeps_app_installed(self):
        self.engine.clock.set_available.side_effect = RuntimeError('offline')
        with self.assertRaises(RuntimeError): self.store.set_installed('clock', False)
        self.assertTrue(is_installed(self.engine, 'clock'))
        self.engine._save_config.assert_not_called()

    def test_bad_requests_are_rejected(self):
        for key, installed in [('unknown', False), ('clock', 'false')]:
            with self.assertRaises(ValueError): self.store.set_installed(key, installed)
        self.engine.clock.set_available.assert_not_called()

    def test_clock_command_is_mocked_and_voice_is_disabled(self):
        self.engine.config['device'] = 'test:5555'
        clock = ClockService(self.engine)
        with patch('clock.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = 'Package example new state: disabled'
            run.return_value.stderr = ''
            clock.set_available(False)
            self.assertIn('pm disable', run.call_args.args[0][-1])
        self.store.set_installed('clock', False)
        self.assertIsNone(clock.handle_text('换个表盘'))
        with self.assertRaises(ValueError): clock.set_face('forest')

    def test_save_failure_rolls_back_state_and_clock(self):
        self.engine._save_config.side_effect = OSError('disk full')
        with self.assertRaises(OSError): self.store.set_installed('clock', False)
        self.assertTrue(is_installed(self.engine, 'clock'))
        self.assertEqual([c.args[0] for c in self.engine.clock.set_available.call_args_list], [False, True])

    def test_weather_removal_and_restore_manage_api_without_losing_settings(self):
        from weather import WeatherService
        self.engine.weather = WeatherService(self.engine, config=self.engine.config['weather'])
        self.store.set_installed('weather', False)
        self.engine.weather_api.stop.assert_called_once()
        self.assertFalse(self.engine.weather.enabled)
        self.assertTrue(self.engine.config['weather']['enabled'])
        self.store.set_installed('weather', True)
        self.engine.weather_api.start.assert_called_once()
        self.assertTrue(self.engine.weather.enabled)
