"""D044 (2026-11-16): start-cap names / arrows and console voice phrases.

Covers the three user requests in one place:
  1. resources card start labels are "C" / "R" (bottom rows keep CPU / RAM);
  2. network arrows are drawn at the ARC START, upright and fixed;
  3. each 小应用 card can save its own spoken phrases, effective at once.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))

from miniapps import registry, system_cards  # noqa: E402
from miniapps.service import MiniAppService  # noqa: E402

JAVA = (ROOT / "android/src/cn/rokid/bridge/MiniAppCardView.java").read_text(
    encoding="utf-8")
CONSOLE = (ROOT / "host/web/console.html").read_text(encoding="utf-8")
SERVER = (ROOT / "host/server.py").read_text(encoding="utf-8")


class StubEngine:
    def __init__(self):
        self.saved = 0
        self.commands = []

    def emit(self, kind, **fields):
        command = dict(fields, type=kind)
        self.commands.append(command)
        return command

    def _save_config(self):
        self.saved += 1


class StubProvider:
    """These tests never read Codex / Mac metrics."""

    def read(self, operation):
        raise AssertionError("provider must not run in a voice-phrase test")


def make_service(config=None):
    return MiniAppService(StubEngine(), provider=StubProvider(),
                          config=config if config is not None else {})


def resources_payload():
    return {"cpu_percent": 12.3, "mem_percent": 44.5,
            "mem_used_bytes": 8 * 1024 ** 3,
            "mem_total_bytes": 16 * 1024 ** 3,
            "sampled_at": 1763222400.0}


def speed_payload():
    return {"down_bps": 168000.0, "up_bps": 97000.0,
            "down_percent": 65.0, "up_percent": 37.0,
            "full_scale_mbps": 20.0, "full_scale_kbps": 256.0,
            "interface": "en0", "sampled_at": 1763222400.0}


class StartCapTest(unittest.TestCase):
    def test_resources_start_labels_are_single_letters(self):
        card = system_cards.resources_result(resources_payload(), "r").data["card"]
        self.assertEqual([r["label"] for r in card["rings"]], ["C", "R"])
        # Only the ring captions change; the two bottom rows stay explicit.
        self.assertEqual([r["label"] for r in card["rows"]], ["CPU", "RAM"])

    def test_network_card_keeps_value_text_arrows(self):
        card = system_cards.speed_result(speed_payload(), "s").data["card"]
        self.assertEqual([r["value_text"] for r in card["rings"]], ["↓", "↑"])
        self.assertEqual([r["label"] for r in card["rings"]], ["", ""])
        for ring in card["rings"]:
            self.assertNotIn("start_icon", ring)

    def test_device_draws_arrow_at_the_start_cap(self):
        self.assertIn("if (!valueText.isEmpty())", JAVA)
        self.assertIn("canvas.drawText(valueText, sx, sy + 7f, textPaint)", JAVA)
        block = JAVA.split("if (!valueText.isEmpty())", 1)[1].split(
            "// The spring", 1)[0]
        self.assertIn("return;", block)
        self.assertNotIn("canvas.rotate", block)
        self.assertNotIn("endRad", block)
        self.assertNotIn("fraction", block)

    def test_percent_caption_is_pure_number_now(self):
        self.assertIn('String number = Math.round(displayed) + "%"', JAVA)
        self.assertIn("if (fraction < 0.02f) return;", JAVA)


class CustomPhraseRegistryTest(unittest.TestCase):
    def test_split_accepts_chinese_and_ascii_separators(self):
        self.assertEqual(
            registry.split_custom_phrases("看看宽带，网速快不快, 快不快\n查一下|测速"),
            ["看看宽带", "网速快不快", "快不快", "查一下", "测速"])

    def test_normalise_dedupes_limits_and_drops_unknown_apps(self):
        cleaned = registry.normalise_custom_phrases({
            "network_speed": ["看看宽带", "看看宽带", "", "x" * 60],
            "not_an_app": ["随便说说"],
            "system_resources": "电脑累不累；看看处理器",
        })
        self.assertEqual(cleaned, {
            "network_speed": ["看看宽带"],
            "system_resources": ["电脑累不累", "看看处理器"],
        })

    def test_rules_are_canonical_and_open_the_default_operation(self):
        rules = registry.build_custom_rules(
            {"network_speed": ["若琪，看看宽带！"]})
        self.assertIn("看看宽带", rules)
        self.assertEqual(rules["看看宽带"][:3],
                         ("network_speed", "speed", "query"))

    def test_custom_phrase_matches_with_wake_word_and_punctuation(self):
        rules = registry.build_custom_rules({"network_speed": ["看看宽带"]})
        hit = registry.match("若琪，看看宽带！", rules)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.app_id, "network_speed")
        self.assertEqual(hit.operation, "speed")
        self.assertEqual(hit.action, "query")

    def test_builtin_phrase_always_wins(self):
        rules = registry.build_custom_rules(
            {"system_resources": ["查询网速"]})
        self.assertEqual(registry.match("查询网速", rules).app_id,
                         "network_speed")

    def test_unrelated_sentence_still_does_not_open_a_card(self):
        rules = registry.build_custom_rules({"network_speed": ["看看宽带"]})
        self.assertIsNone(registry.match("帮我查一下 codex 是什么", rules))

    def test_console_listings(self):
        self.assertIn("查询网速", registry.builtin_phrases_for(
            registry.NETWORK_APP_ID))
        self.assertIn("查询系统资源", registry.builtin_phrases_for(
            registry.RESOURCES_APP_ID))
        self.assertIn("关闭卡片", registry.card_action_phrases())
        # Hide/refresh phrases must not appear as a card's query phrases.
        self.assertNotIn("关闭卡片", registry.builtin_phrases_for(
            registry.APP_ID))


class CustomPhraseServiceTest(unittest.TestCase):
    def test_save_takes_effect_without_restart(self):
        svc = make_service()
        out = svc.set_voice_phrases("network_speed", "看看宽带，网速快不快")
        self.assertEqual(out["phrases"], ["看看宽带", "网速快不快"])
        self.assertEqual(svc.engine.saved, 1)
        with patch.object(svc, "query",
                          return_value=SimpleNamespace(status="ok", speech="")) as q:
            result = svc.handle_text("看看宽带")
        self.assertEqual(result["app_id"], "network_speed")
        self.assertEqual(result["operation"], "speed")
        self.assertEqual(q.call_args.args[0], "network_speed")
        self.assertEqual(q.call_args.args[1], "speed")
        # D034: display-only. A custom phrase must never add a speak command.
        self.assertNotIn("speak", result)

    def test_save_writes_the_live_config_dict(self):
        config = {"voice_phrases": {}}
        svc = make_service(config)
        svc.set_voice_phrases("system_resources", ["电脑累不累"])
        self.assertEqual(config["voice_phrases"],
                         {"system_resources": ["电脑累不累"]})

    def test_config_phrases_are_loaded_on_startup(self):
        config = {"voice_phrases": {"system_resources": ["电脑累不累"]}}
        svc = make_service(config)
        with patch.object(svc, "query",
                          return_value=SimpleNamespace(status="ok", speech="")):
            self.assertEqual(svc.handle_text("电脑累不累")["app_id"],
                             "system_resources")

    def test_empty_input_clears_only_that_card(self):
        svc = make_service()
        svc.set_voice_phrases("network_speed", ["看看宽带"])
        svc.set_voice_phrases("system_resources", ["电脑累不累"])
        out = svc.set_voice_phrases("network_speed", "")
        self.assertEqual(out["phrases"], [])
        self.assertEqual(out["voice_phrases"],
                         {"system_resources": ["电脑累不累"]})
        self.assertIsNone(svc.handle_text("看看宽带"))
        with patch.object(svc, "query",
                          return_value=SimpleNamespace(status="ok", speech="")):
            self.assertEqual(svc.handle_text("电脑累不累")["app_id"],
                             "system_resources")

    def test_unknown_app_is_rejected(self):
        with self.assertRaises(ValueError):
            make_service().set_voice_phrases("not_an_app", ["hi"])

    def test_two_services_do_not_share_phrases(self):
        first, second = make_service(), make_service()
        first.set_voice_phrases("network_speed", ["看看宽带"])
        self.assertIsNone(second.handle_text("看看宽带"))

    def test_status_exposes_builtin_and_custom(self):
        svc = make_service()
        svc.set_voice_phrases("network_speed", ["看看宽带"])
        status = svc.status()
        self.assertEqual(status["voice_phrases"],
                         {"network_speed": ["看看宽带"]})
        self.assertIn("查询网速", status["builtin_phrases"]["network_speed"])
        self.assertIn("关闭卡片", status["card_actions"])
        self.assertEqual([app["id"] for app in status["apps"]],
                         ["codex_usage", "network_speed", "system_resources", "bambu_print"])


class ConsoleSurfaceTest(unittest.TestCase):
    def test_every_card_has_an_input_and_a_save_button(self):
        for app_id in ("codex_usage", "network_speed", "system_resources"):
            self.assertIn('id="minivoice-%s"' % app_id, CONSOLE)
            self.assertIn('data-voice-save="%s"' % app_id, CONSOLE)
            self.assertIn('id="minibuiltin-%s"' % app_id, CONSOLE)

    def test_loading_and_saving_are_wired(self):
        # The mini-apps cards and the weather card load together when
        # the 小应用 tab opens (the weather card is a D048 addition).
        self.assertIn("if (isMiniapps) { showAppCatalog(); loadAppStore(); loadMiniappVoice(); loadWeather(); loadClock(); }",
                      CONSOLE)
        self.assertIn('apiGet("/api/miniapps")', CONSOLE)
        self.assertIn('apiPost("/api/miniapps/voice"', CONSOLE)
        self.assertIn('url.path == "/api/miniapps/voice"', SERVER)
        self.assertIn('"voice_phrases": {}',
                      (ROOT / "host/config.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
