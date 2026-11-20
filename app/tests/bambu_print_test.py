"""P1S query card, using simulated replies and no hardware connections."""
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from miniapps import bambu, registry
from miniapps.service import MiniAppService

CREDENTIALS = {"host": "192.168.1.50", "serial": "TESTSERIAL1", "access_code": "FAKE2468"}
REPORT = {
    "command": "push_status", "gcode_state": "RUNNING", "mc_percent": 25,
    "mc_remaining_time": 20, "nozzle_temper": 219, "bed_temper": 55,
    "layer_num": 1, "total_layer_num": 62, "subtask_name": "Stand",
}
try:
    HAS_MQTT = importlib.util.find_spec("paho.mqtt.client") is not None
except ModuleNotFoundError:
    HAS_MQTT = False


class BambuTest(unittest.TestCase):
    def test_secret_write_only_and_host_change_requires_reentry(self):
        engine = SimpleNamespace(config={}, _save_config=Mock())
        printer = bambu.BambuPrinter(engine)
        self.assertEqual(printer.save(CREDENTIALS), {
            "host": "192.168.1.50", "serial": "TESTSERIAL1", "has_access_code": True,
        })
        self.assertEqual(printer.save({"access_code": ""}), printer.settings())
        with self.assertRaises(ValueError):
            printer.save({"host": "192.168.1.51"})
        with self.assertRaises(ValueError):
            printer.save({"host": "127.0.0.1"})
        self.assertEqual(engine.config[bambu.APP_ID], CREDENTIALS)

    def test_task_boundary_and_explicit_zero(self):
        state = dict(REPORT)
        bambu._merge(state, {"mc_remaining_time": 0})
        self.assertEqual(state["mc_percent"], 25)
        self.assertEqual(state["mc_remaining_time"], 0)
        bambu._merge(state, {"subtask_name": "Next"})
        self.assertNotIn("mc_percent", state)
        self.assertEqual(state["nozzle_temper"], 219)
        self.assertEqual(bambu.result({"gcode_state": "IDLE", "mc_percent": 100})
                         .data["card"]["printer"]["progress_text"], "—")

    def test_query_routes_displays_and_announces_progress(self):
        engine = SimpleNamespace(
            config={"app_store": {"installed": {bambu.APP_ID: True}}, bambu.APP_ID: CREDENTIALS},
            emit=Mock(),
        )
        service = MiniAppService(engine, provider=Mock())
        with patch.object(bambu, "read_snapshot", return_value=REPORT):
            answer = service.handle_text("若琪，打印还要多久")
        self.assertEqual(answer["app_id"], bambu.APP_ID)
        self.assertEqual(engine.emit.call_count, 2)
        args, payload = engine.emit.call_args_list[0]
        self.assertEqual(engine.emit.call_args_list[1].args, ("speak",))
        self.assertEqual(engine.emit.call_args_list[1].kwargs["text"],
                         "打印到百分之25，剩余20分钟")
        self.assertEqual(engine.emit.call_args_list[1].kwargs["card_id"], payload["card_id"])
        self.assertEqual(engine.emit.call_args_list[1].kwargs["expires_at_ms"],
                         payload["expires_at_ms"])
        self.assertEqual(args, ("miniapp_card",))
        self.assertEqual(payload["card"]["printer"]["remaining"], "20 分钟")
        self.assertEqual(payload["card"]["printer"]["nozzle"], "219°C")
        engine.config["app_store"]["installed"][bambu.APP_ID] = False
        self.assertIsNone(service.handle_text("查询打印进度"))
        self.assertEqual(registry.default_operation(bambu.APP_ID), bambu.OPERATION)

    def test_printer_deadline_restarts_and_does_not_leak_to_other_cards(self):
        engine = SimpleNamespace(
            config={"app_store": {"installed": {bambu.APP_ID: True}}}, emit=Mock(),
        )
        service = MiniAppService(engine, provider=Mock(), config={"card_ttl_ms": 30_000})
        printer = bambu.result(REPORT)
        other = bambu.QueryResult("network_speed", "speed", data={"card": {"title": "网络速度"}})
        for moment, result, ttl in ((100, other, 30_000), (105, printer, 50_000),
                                    (110, printer, 50_000), (125, other, 30_000)):
            with patch("miniapps.service.time.time", return_value=moment):
                service._present(result, "")
                payload = service.current_command()
                self.assertEqual(payload["ttl_ms"], ttl)
                self.assertEqual(payload["expires_at_ms"], moment * 1000 + ttl)

    @unittest.skipUnless(HAS_MQTT, "optional paho-mqtt dependency")
    def test_connection_subscribes_and_requests_status_only(self):
        import json
        from paho.mqtt import client as mqtt
        client = Mock()
        client.subscribe.return_value = (mqtt.MQTT_ERR_SUCCESS, 1)
        topic = "device/" + CREDENTIALS["serial"]
        success = SimpleNamespace(is_failure=False)

        def loop(timeout):
            client.on_connect(client, None, None, success, None)
            client.on_subscribe(client, None, 1, [success], None)
            client.on_message(client, None, SimpleNamespace(
                topic=topic + "/report", retain=False, payload=json.dumps({"print": REPORT})))
            return mqtt.MQTT_ERR_SUCCESS

        client.loop.side_effect = loop
        with patch.object(mqtt, "Client", return_value=client):
            self.assertEqual(bambu.read_snapshot(CREDENTIALS)["mc_percent"], 25)
        client.username_pw_set.assert_called_once_with("bblp", "FAKE2468")
        self.assertEqual(client.tls_set_context.call_args.args[0].verify_mode, bambu.ssl.CERT_REQUIRED)
        client.subscribe.assert_called_once_with(topic + "/report")
        published_topic, body = client.publish.call_args.args
        self.assertEqual(published_topic, topic + "/request")
        self.assertEqual(json.loads(body)["pushing"]["command"], "pushall")
        client.publish.assert_called_once()
        client.disconnect.assert_called_once()
