"""Home Assistant batch 1: URL validation, typed transport errors,
settings redaction and the read-only console API.

Everything is mocked at the transport boundary -- 00-START-HERE §5.8:
no test may reach a real HA, let alone move a real device.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from homeassistant import (  # noqa: E402
    HaAuthError, HaClient, HaNetworkError, HaNotFoundError, HaProtocolError,
    HomeAssistantService, normalise_base_url,
)
from homeassistant import client as ha_client_module  # noqa: E402
from homeassistant.models import CARD_STATUS, HaResult  # noqa: E402


class FakeTransport:
    """Scripted (status, body) queue; records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url,
                           "headers": headers, "body": body,
                           "timeout": timeout})
        item = self.responses.pop(0) if self.responses else (200, b"{}")
        if isinstance(item, Exception):
            raise item
        status, raw = item
        return status, raw if isinstance(raw, bytes) else raw.encode("utf-8")


class BaseUrlTest(unittest.TestCase):
    def test_normalises_and_strips_slash(self):
        self.assertEqual(normalise_base_url("http://192.0.2.4:8123/"),
                         "http://192.0.2.4:8123")
        self.assertEqual(normalise_base_url("https://ha.example:8123"),
                         "https://ha.example:8123")

    def test_empty_stays_empty(self):
        self.assertEqual(normalise_base_url(""), "")
        self.assertEqual(normalise_base_url(None), "")

    def test_rejects_credentials_in_url(self):
        with self.assertRaises(ValueError):
            normalise_base_url("http://user:pass@192.0.2.4:8123")

    def test_rejects_scheme_and_path_and_query(self):
        for bad in ("192.0.2.4:8123", "ftp://x", "http://x/api/states",
                    "http://x?foo=1", "http://"):
            with self.assertRaises(ValueError):
                normalise_base_url(bad)


class ClientErrorsTest(unittest.TestCase):
    def test_auth_error(self):
        for code in (401, 403):
            client = HaClient("http://ha", "t", transport=FakeTransport(
                (code, b'{"message":"no"}')))
            with self.assertRaises(HaAuthError):
                client.api_check()

    def test_not_found(self):
        client = HaClient("http://ha", "t", transport=FakeTransport(
            (404, b"")))
        with self.assertRaises(HaNotFoundError):
            client.state("light.x")

    def test_server_error_and_bad_json(self):
        client = HaClient("http://ha", "t", transport=FakeTransport(
            (500, b"oops")))
        with self.assertRaises(HaProtocolError):
            client.api_check()
        client = HaClient("http://ha", "t", transport=FakeTransport(
            (200, b"<html>")))
        with self.assertRaises(HaProtocolError):
            client.api_check()

    def test_api_check_requires_message_field(self):
        client = HaClient("http://ha", "t", transport=FakeTransport(
            (200, b"{}")))
        with self.assertRaises(HaProtocolError):
            client.api_check()

    def test_oversized_response_rejected(self):
        saved = ha_client_module.MAX_RESPONSE_BYTES
        ha_client_module.MAX_RESPONSE_BYTES = 10
        try:
            client = HaClient("http://ha", "t", transport=FakeTransport(
                (200, b"x" * 11)))
            with self.assertRaises(HaProtocolError):
                client.api_check()
        finally:
            ha_client_module.MAX_RESPONSE_BYTES = saved

    def test_get_retries_once_but_post_does_not(self):
        boom = HaNetworkError("down")
        transport = FakeTransport(boom, (200, b'{"message":"API running."}'))
        client = HaClient("http://ha", "t", transport=transport)
        self.assertEqual(client.api_check()["message"], "API running.")
        self.assertEqual(len(transport.calls), 2)

        transport = FakeTransport(boom)
        client = HaClient("http://ha", "t", transport=transport)
        with self.assertRaises(HaNetworkError):
            client.process("打开茶几灯")
        self.assertEqual(len(transport.calls), 1)   # no write retry

    def test_process_payload_shape(self):
        transport = FakeTransport(
            (200, b'{"response_type":"action_done","data":{}}'))
        client = HaClient("http://ha", "t", transport=transport)
        client.process("打开茶几灯", language="zh-CN", agent_id="a1")
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(call["url"].endswith("/api/conversation/process"))
        self.assertIn(b'"text": "\u6253\u5f00\u8336\u51e0\u706f"', call["body"])
        self.assertIn(b'"agent_id": "a1"', call["body"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer t")

    def test_network_error_keeps_message_short(self):
        boom = HaNetworkError("连不上 Home Assistant：boom")
        client = HaClient("http://ha", "t", transport=FakeTransport(boom, boom))
        with self.assertRaises(HaNetworkError) as caught:
            client.api_check()
        self.assertIn("连不上", caught.exception.message)


class ServiceSettingsTest(unittest.TestCase):
    def make(self, transport=None):
        self.saved = []
        config = {"enabled": False, "base_url": "http://ha:8123",
                  "token": "secret-token", "language": "zh-CN",
                  "agent_id": "", "request_timeout_seconds": 15,
                  "speak_results": True, "overview_entities": [],
                  "aliases": {}, "shortcuts": []}
        return HomeAssistantService(
            config=config, save=lambda: self.saved.append(1),
            transport=transport)

    def test_settings_never_expose_token(self):
        service = self.make()
        settings = service.settings()
        self.assertNotIn("token", settings)
        self.assertTrue(settings["token_configured"])

    def test_empty_token_keeps_value(self):
        service = self.make()
        service.update_settings(token="")
        self.assertEqual(service.config["token"], "secret-token")

    def test_clear_token_flag_wipes(self):
        service = self.make()
        service.update_settings(token="", clear_token=True)
        self.assertEqual(service.config["token"], "")
        self.assertFalse(service.settings()["token_configured"])
        self.assertEqual(self.saved, [1])

    def test_switching_instance_drops_old_token(self):
        service = self.make()
        service.update_settings(base_url="http://other:8123")
        self.assertEqual(service.config["token"], "")
        self.assertEqual(service.config["base_url"], "http://other:8123")

    def test_same_instance_keeps_token(self):
        service = self.make()
        service.update_settings(base_url="http://ha:8123/")
        self.assertEqual(service.config["token"], "secret-token")

    def test_new_lan_ip_keeps_token_when_asked(self):
        # 唤醒词页：同一台 HA 换了 DHCP 地址，令牌仍然有效。
        service = self.make()
        service.update_settings(base_url="http://192.168.1.9:8123",
                                keep_token=True)
        self.assertEqual(service.config["base_url"],
                         "http://192.168.1.9:8123")
        self.assertEqual(service.config["token"], "secret-token")

    def test_bare_lan_ip_is_accepted(self):
        service = self.make()
        service.update_settings(base_url="192.0.2.4:8123",
                                keep_token=True)
        self.assertEqual(service.config["base_url"],
                         "http://192.0.2.4:8123")
        self.assertEqual(service.config["token"], "secret-token")

    def test_explicit_new_token_always_wins(self):
        service = self.make()
        service.update_settings(base_url="http://other:8123",
                                token="fresh", keep_token=True)
        self.assertEqual(service.config["token"], "fresh")

    def test_overview_capped_at_four(self):
        service = self.make()
        service.update_settings(
            overview_entities=["a.1", "b.2", "c.3", "d.4", "e.5", "a.1"])
        self.assertEqual(service.config["overview_entities"],
                         ["a.1", "b.2", "c.3", "d.4"])

    def test_alias_table_cleaning(self):
        service = self.make()
        service.update_settings(aliases={
            "light.unknown_21": "茶几灯|小几灯",
            "bad-key": "x",          # no dot -> dropped
            "sensor.unknown": "  ",
        })
        self.assertEqual(service.config["aliases"],
                         {"light.unknown_21": "茶几灯|小几灯"})


class ServiceReadOnlyTest(unittest.TestCase):
    def states_body(self):
        import json
        return json.dumps([
            {"entity_id": "light.lamp", "state": "on",
             "attributes": {"friendly_name": "茶几灯"}},
            {"entity_id": "sensor.temp", "state": "28.9",
             "attributes": {"friendly_name": "主卧温度"}},
            {"entity_id": "update.core", "state": "on",
             "attributes": {"friendly_name": "Core"}},
        ]).encode()

    def make(self, *responses):
        transport = FakeTransport(*responses)
        config = {"enabled": True, "base_url": "http://ha:8123",
                  "token": "t", "request_timeout_seconds": 15,
                  "speak_results": True, "overview_entities": [],
                  "aliases": {}, "shortcuts": []}
        return HomeAssistantService(config=config, transport=transport), transport

    def test_test_connection_ok(self):
        service, transport = self.make(
            (200, b'{"message":"API running."}'), (200, self.states_body()))
        result = service.test_connection()
        self.assertTrue(result["ok"])
        self.assertTrue(result["reachable"])
        self.assertTrue(result["auth_ok"])
        self.assertEqual(result["entity_count"], 3)
        # GET-only: no request may be a POST.
        self.assertTrue(all(c["method"] == "GET" for c in transport.calls))

    def test_test_connection_auth_failure(self):
        service, _ = self.make((401, b""))
        result = service.test_connection()
        self.assertFalse(result["ok"])
        self.assertTrue(result["reachable"])
        self.assertFalse(result["auth_ok"])

    def test_test_connection_network_failure(self):
        # Two failures: _get retries a read once before giving up.
        boom = HaNetworkError("连不上 Home Assistant：x")
        service, _ = self.make(boom, boom)
        result = service.test_connection()
        self.assertFalse(result["ok"])
        self.assertFalse(result["reachable"])
        self.assertIn("连不上", result["message"])

    def test_entities_filter_and_domain(self):
        service, _ = self.make((200, self.states_body()))
        rows = service.entities()
        ids = [row["entity_id"] for row in rows]
        self.assertIn("light.lamp", ids)
        self.assertIn("sensor.temp", ids)
        self.assertNotIn("update.core", ids)     # not a voiced domain
        service, _ = self.make((200, self.states_body()))
        rows = service.entities(query="茶几")
        self.assertEqual([r["entity_id"] for r in rows], ["light.lamp"])


class HaResultTest(unittest.TestCase):
    def test_unknown_never_maps_to_ok(self):
        self.assertEqual(CARD_STATUS["unknown"], "stale")
        result = HaResult(status="unknown", message="没有收到执行结果")
        self.assertNotEqual(result.card_status, "ok")

    def test_mapping_table(self):
        self.assertEqual(CARD_STATUS["reported_success"], "ok")
        self.assertEqual(CARD_STATUS["partial"], "partial")
        self.assertEqual(CARD_STATUS["failed"], "error")
        self.assertEqual(CARD_STATUS["running"], "loading")

    def test_to_dict_shape(self):
        data = HaResult(request_id="ha-1", kind="control",
                        status="reported_success",
                        message="HA 已反馈书房灯开启").to_dict()
        self.assertEqual(data["request_id"], "ha-1")
        self.assertEqual(data["verification"], "not_checked")
        self.assertEqual(data["card_status"], "ok")
        self.assertTrue(data["received_at"])


if __name__ == "__main__":
    unittest.main()
