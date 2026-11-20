"""Local Mac query apps (D040): metrics, cards, phrases and service routing.

Everything that touches the machine is injected. The real psutil/CLI
samplers are smoke-tested separately on the host, never in unit tests.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from miniapps import registry, system_cards, system_metrics  # noqa: E402
from miniapps.models import validate_card  # noqa: E402
from miniapps.service import MiniAppService  # noqa: E402
from miniapps.system_metrics import (  # noqa: E402
    MB, GB, MacSystemProvider, MetricsError, Sample, format_memory,
    format_rate, pick_interfaces)

NOW = time.time()
FULL = 20 * MB


def sample(at, cpu=36.0, io=None, used=int(10.9 * GB), total=16 * 1024**3):
    return Sample(at=at, cpu_percent=cpu, mem_used_bytes=used,
                  mem_total_bytes=total, io=io or {"en0": (1000, 2000)})


class FakeSampler:
    def __init__(self, samples):
        self.samples = list(samples)
        self.calls = 0

    def read(self):
        value = self.samples[min(self.calls, len(self.samples) - 1)]
        self.calls += 1
        return value


class FakeEngine:
    def __init__(self):
        self.commands = []
        self.speeches = []
        self.instance_id = "test-instance"

    def emit(self, kind, **fields):
        command = dict(fields, type=kind)
        self.commands.append(command)
        return command

    def deliver_speech(self, text, agent_id, request_id=""):
        self.speeches.append((text, agent_id, request_id))
        return text

    def cards(self):
        return [c for c in self.commands if c["type"] == "miniapp_card"]


class FakeSystemProvider:
    def __init__(self, payload=None, error=None):
        self.calls = []
        self.error = error
        self.payload = payload or {
            "cpu_percent": 36.4, "mem_percent": 68.1,
            "mem_used_bytes": int(10.9 * GB),
            "mem_total_bytes": 16 * 1024**3,
            "down_bps": 12.8 * MB, "up_bps": 2.4 * MB,
            "down_percent": 64.0, "up_percent": 12.0,
            "full_scale_mbps": 20.0, "interface": "en0",
            "sampled_at": NOW, "partial": False,
        }

    def read(self, operation):
        self.calls.append(operation)
        if self.error:
            raise self.error
        return dict(self.payload)


class FakeCodexProvider:
    def read(self, operation):
        return {"rate_limits": None, "usage": None}


def provider(samples=None, config=None, sleep=None):
    return MacSystemProvider(
        sampler=FakeSampler(samples or [sample(100.0), sample(101.0)]),
        config=config, autostart=False,
        sleep=sleep if sleep is not None else (lambda _: None))


class MetricsTest(unittest.TestCase):
    def test_throughput_is_a_last_second_delta_not_a_running_total(self):
        first = sample(100.0, io={"lo0": (999, 999), "en0": (1000, 2000)})
        second = sample(101.0, io={"lo0": (999 + 50 * MB, 999 + 50 * MB),
                                   "en0": (1000 + 12 * MB, 2000 + 3 * MB)})
        p = provider([first, second])
        p.poll()
        p.poll()
        data = p.read("speed")
        self.assertAlmostEqual(data["down_bps"], 12 * MB, delta=1)
        self.assertAlmostEqual(data["up_bps"], 3 * MB, delta=1)
        self.assertEqual(data["interface"], "en0")
        self.assertAlmostEqual(data["down_percent"], 60.0)
        self.assertAlmostEqual(data["up_percent"], 15.0)
        self.assertAlmostEqual(data["cpu_percent"], 36.0)
        self.assertAlmostEqual(data["mem_percent"], 68.125, delta=0.2)

    def test_full_scale_is_configurable_and_clamps_at_100(self):
        first = sample(100.0)
        second = sample(101.0, io={"en0": (1000 + 50 * MB, 2000 + 50 * MB)})
        p = provider([first, second], config={"network_full_scale_mbps": 20})
        p.poll()
        p.poll()
        data = p.read("speed")
        self.assertEqual(data["down_percent"], 100.0)
        self.assertEqual(data["up_percent"], 100.0)
        self.assertEqual(data["full_scale_mbps"], 20.0)

    def test_slow_link_auto_ranges_so_the_arc_is_visible(self):
        first = sample(100.0)
        second = sample(101.0, io={"en0": (1000 + 90 * 1024, 2000 + 8192)})
        p = provider([first, second])
        p.poll()
        p.poll()
        data = p.read("speed")
        self.assertEqual(data["full_scale_kbps"], 128.0)
        self.assertEqual(data["full_scale_cap_mbps"], 20.0)
        self.assertAlmostEqual(data["down_percent"], 90.0 / 128.0 * 100,
                               delta=0.5)
        self.assertIn("满量程 128 KB/s",
                      system_cards.speed_result(data).data["card"]["footer"])

    def test_design_example_speed_still_uses_the_20_mb_step(self):
        first = sample(100.0)
        second = sample(101.0, io={"en0": (1000 + 12.8 * MB,
                                           2000 + 2.4 * MB)})
        p = provider([first, second])
        p.poll()
        p.poll()
        data = p.read("speed")
        self.assertEqual(data["full_scale_kbps"], 20480.0)
        self.assertAlmostEqual(data["down_percent"], 64.0, delta=0.1)
        self.assertIn("满量程 20 MB/s",
                      system_cards.speed_result(data).data["card"]["footer"])

    def test_polls_closer_than_half_an_interval_are_ignored(self):
        close = sample(100.001, io={"en0": (1000 + 999 * MB, 2000)})
        p = provider([sample(100.0), close])
        p.poll()
        p.poll()
        self.assertIsNone(p._reading)

    def test_counter_reset_never_shows_a_negative_speed(self):
        first = sample(100.0, io={"en0": (9000, 9000)})
        second = sample(101.0, io={"en0": (10, 10)})
        p = provider([first, second])
        p.poll()
        p.poll()
        data = p.read("speed")
        self.assertEqual(data["down_bps"], 0.0)
        self.assertEqual(data["up_bps"], 0.0)
        self.assertFalse(data["has_network"])

    def test_only_physical_interfaces_are_counted(self):
        io = {"lo0": (10**9, 10**9), "utun8": (10**9, 10**9),
              "en0": (500, 700), "en5": (0, 0)}
        self.assertEqual(pick_interfaces(io), ["en0"])
        self.assertEqual(pick_interfaces({"lo0": (10, 10)}), [])

    def test_cold_start_takes_two_samples_and_still_returns(self):
        p = provider([sample(100.0),
                      sample(101.0, io={"en0": (1000 + MB, 2000 + 2 * MB)})])
        sleeps = []
        p._sleep = lambda seconds: sleeps.append(seconds)
        data = p.read("resources")
        self.assertEqual(sleeps, [1.0])
        self.assertAlmostEqual(data["down_bps"], MB, delta=1)

    def test_unknown_operation_and_missing_reading_are_errors(self):
        p = provider()
        with self.assertRaises(MetricsError):
            p.read("quota")

    def test_format_helpers(self):
        self.assertEqual(format_rate(12.8 * MB), "12.8 MB/s")
        self.assertEqual(format_rate(4 * 1024), "4 KB/s")
        self.assertEqual(format_rate(0), "0 B/s")
        self.assertEqual(format_memory(int(10.9 * GB), 16 * 1024**3),
                         "10.9 / 16 GB")
        self.assertEqual(format_memory(None, None), "—")


class SystemCardTest(unittest.TestCase):
    def setUp(self):
        self.payload = FakeSystemProvider().payload

    def test_resources_card_matches_the_design(self):
        result = system_cards.resources_result(self.payload, "r1")
        card = result.data["card"]
        validate_card(card)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.app_id, "system_resources")
        self.assertEqual(card["template"], "rings")
        self.assertEqual(card["icon"], "system-chip.png")
        self.assertEqual(card["title"], "系统资源")
        self.assertEqual(card["rows"], [
            {"label": "CPU", "value": "36%"},
            {"label": "RAM", "value": "10.9 / 16 GB"}])
        self.assertIn("系统资源", card["footer"])
        self.assertEqual([r["color"] for r in card["rings"]],
                         ["#ffa634", "#a76dfd"])
        self.assertEqual([r["width"] for r in card["rings"]], [30, 30])
        self.assertEqual([r["radius"] for r in card["rings"]], [138, 96])
        self.assertNotIn("value_text", card["rings"][0])
        # No reset timestamp: the renderer must keep the host's row text.
        for ring in card["rings"]:
            self.assertNotIn("reset_at_ms", ring)

    def test_speed_card_matches_the_design(self):
        result = system_cards.speed_result(self.payload, "s1")
        card = result.data["card"]
        validate_card(card)
        self.assertEqual(result.app_id, "network_speed")
        self.assertEqual(card["icon"], "network-arrows.png")
        self.assertEqual(card["rows"], [
            {"label": "↓", "value": "12.8 MB/s"},
            {"label": "↑", "value": "2.4 MB/s"}])
        self.assertEqual([r["color"] for r in card["rings"]],
                         ["#07f2c6", "#2789ff"])
        self.assertEqual([r["track_color"] for r in card["rings"]],
                         ["#0f6c5e", "#305179"])
        self.assertEqual([r["value_text"] for r in card["rings"]], ["↓", "↑"])
        self.assertEqual([r["label"] for r in card["rings"]], ["", ""])
        self.assertIn("满量程 20 MB/s", card["footer"])
        self.assertIn("网络速度", card["footer"])

    def test_missing_cpu_is_partial_not_a_fake_zero(self):
        payload = dict(self.payload, cpu_percent=None)
        result = system_cards.resources_result(payload)
        card = result.data["card"]
        self.assertEqual(result.status, "partial")
        self.assertEqual(card["rows"][0]["value"], "—")
        self.assertIn("CPU", card["notice"])

    def test_no_network_interface_is_partial(self):
        payload = dict(self.payload, interface="")
        result = system_cards.speed_result(payload)
        self.assertEqual(result.status, "partial")
        self.assertIn("网络接口", result.data["card"]["notice"])

    def test_bad_payload_never_produces_a_plausible_zero_percent(self):
        payload = dict(self.payload, cpu_percent="nan", mem_percent="oops",
                       down_bps=float("inf"))
        result = system_cards.resources_result(payload)
        validate_card(result.data["card"])
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.data["card"]["rows"][0]["value"], "—")

    def test_build_result_and_error_result(self):
        self.assertEqual(system_cards.build_result(
            "speed", self.payload).operation, "speed")
        self.assertEqual(system_cards.build_result(
            "resources", self.payload).operation, "resources")
        with self.assertRaises(ValueError):
            system_cards.build_result("quota", self.payload)
        error = system_cards.error_result("speed", "unavailable", "no iface")
        self.assertEqual(error.status, "error")
        self.assertEqual(error.app_id, "network_speed")
        self.assertEqual(error.data["card"]["template"], "lines")
        validate_card(error.data["card"])
        error.validate()


class RegistryTest(unittest.TestCase):
    def test_new_apps_are_registered(self):
        apps = {a["id"]: a for a in registry.list_apps()}
        self.assertEqual(set(apps), {"codex_usage", "network_speed",
                                     "system_resources", "bambu_print"})
        self.assertEqual(apps["network_speed"]["operations"], ["speed"])
        self.assertEqual(apps["system_resources"]["operations"], ["resources"])
        self.assertEqual(registry.default_operation("network_speed"), "speed")

    def test_recommended_phrases_and_folds(self):
        for phrase, app in (
            ("查询网速", "network_speed"),
            ("看看网速", "network_speed"),
            ("查询网络速度", "network_speed"),
            ("查询下载速度", "network_speed"),
            ("若琪查询网速", "network_speed"),
            ("查询系统资源", "system_resources"),
            ("看看系统资源", "system_resources"),
            ("查询CPU内存", "system_resources"),
            ("查询处理器和内存", "system_resources"),
            ("查一下CPU", "system_resources"),
            ("若琪查询系统资源", "system_resources"),
        ):
            hit = registry.match(phrase)
            self.assertIsNotNone(hit, phrase)
            self.assertEqual(hit.app_id, app)

    def test_refresh_and_hide(self):
        self.assertEqual(registry.match("刷新网速").action, "refresh")
        self.assertEqual(registry.match("刷新系统资源").app_id,
                         "system_resources")
        self.assertEqual(registry.match("关闭卡片").action, "hide")

    def test_codex_phrases_and_providerless_gaps_survive(self):
        self.assertEqual(registry.match("查询龙女额度").app_id, "codex_usage")
        self.assertEqual(registry.match("查询GPT额度").app_id, "codex_usage")
        for phrase in ("查询用量", "查询剩余额度", "查询 Claude 额度",
                       "token 是什么", "帮我写 Codex 插件"):
            self.assertIsNone(registry.match(phrase), phrase)


class ServiceRoutingTest(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.system = FakeSystemProvider()
        self.service = MiniAppService(
            self.engine, provider=FakeCodexProvider(),
            system_provider=self.system)

    def test_voice_routes_to_the_right_app_with_no_speech(self):
        for text, app_id, operation in (
            ("若琪查询网速", "network_speed", "speed"),
            ("若琪查询系统资源", "system_resources", "resources"),
        ):
            answer = self.service.handle_text(text, "req-" + operation)
            self.assertEqual(answer["intent"], "miniapp")
            self.assertEqual(answer["app_id"], app_id)
            card = self.engine.cards()[-1]
            self.assertEqual(card["app_id"], app_id)
            self.assertEqual(card["operation"], operation)
            self.assertEqual(card["action"], "show")
            self.assertEqual(card["ttl_ms"], 30000)
        self.assertEqual([c for c in self.engine.commands
                          if c["type"] == "speak"], [])
        self.assertEqual(self.engine.speeches, [])

    def test_query_accepts_app_id_and_rejects_unknown_operation(self):
        result = self.service.query(app_id="network_speed", operation="speed")
        self.assertEqual(result.app_id, "network_speed")
        self.assertEqual(self.system.calls, ["speed"])
        with self.assertRaises(ValueError):
            self.service.query(app_id="network_speed", operation="quota")
        with self.assertRaises(ValueError):
            self.service.query(app_id="weather", operation="speed")

    def test_live_readings_have_a_short_cache(self):
        self.service.query(app_id="network_speed", operation="speed")
        self.service.query(app_id="network_speed", operation="speed")
        self.assertEqual(len(self.system.calls), 1)
        self.assertEqual(self.service.status()["cache_ttl_seconds"]["speed"],
                         2.0)
        self.assertEqual(len(self.service.status()["apps"]), 4)

    def test_refresh_reuses_the_last_operation_per_app(self):
        self.service.handle_text("查询网速", "r1")
        answer = self.service.handle_text("刷新网速", "r2")
        self.assertEqual(answer["operation"], "speed")
        self.assertEqual(answer["app_id"], "network_speed")

    def test_hide_uses_the_current_app_id(self):
        self.service.handle_text("查询系统资源", "r1")
        self.service.hide()
        hide = self.engine.cards()[-1]
        self.assertEqual(hide["action"], "hide")
        self.assertEqual(hide["app_id"], "system_resources")

    def test_provider_error_becomes_a_system_error_card(self):
        self.system.error = MetricsError("unavailable", "no interface")
        result = self.service.query(app_id="network_speed", operation="speed")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.app_id, "network_speed")
        self.assertEqual(result.data["card"]["template"], "lines")
        validate_card(result.data["card"])


class DeviceRendererTest(unittest.TestCase):
    """Source-level locks for the D040 renderer extensions."""

    def setUp(self):
        self.java = (Path(__file__).resolve().parents[1]
                     / "android/src/cn/rokid/bridge/MiniAppCardView.java"
                     ).read_text()

    def test_new_card_fields_are_supported(self):
        self.assertIn("str(ring, \"track_color\")", self.java)
        self.assertIn("str(ring, \"value_text\")", self.java)
        self.assertIn("if (!label.isEmpty())", self.java)
        self.assertIn("if (resetAt > 0) value = resetText(resetAt)", self.java)
        # Every value uses one endpoint rule; the old low-value branch put
        # "14%" right on top of the bottom "CPU 14%" row.
        self.assertNotIn("raw < 19f", self.java)
        self.assertIn("fraction < 0.02f", self.java)
        self.assertIn("!wantIcon.equals(iconName)", self.java)
        self.assertIn("iconName = \"\"", self.java)

    def test_bundled_icons_exist(self):
        assets = (Path(__file__).resolve().parents[1] / "android/assets")
        for name in ("system-chip.png", "network-arrows.png", "system-chip.png"):
            path = assets / name
            self.assertTrue(path.is_file(), name)
            self.assertGreater(path.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
