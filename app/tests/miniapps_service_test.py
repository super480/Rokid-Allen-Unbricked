"""Mini-app runtime: voice routing, cache, merge, card lifecycle, reconnect."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
from engine import Engine  # noqa: E402
from miniapps import cards, registry  # noqa: E402
from miniapps.codex_usage import (  # noqa: E402
    CodexError, normalise_rate_limits, normalise_usage)
from miniapps.service import MiniAppService  # noqa: E402

NOW = time.time()


def payload():
    return {
        "rate_limits": normalise_rate_limits({"rateLimitsByLimitId": {"codex": {
            "limitId": "codex",
            "primary": {"usedPercent": 32, "windowDurationMins": 300,
                        "resetsAt": int(NOW) + 14760},
            "secondary": {"usedPercent": 58, "windowDurationMins": 10080,
                          "resetsAt": int(NOW) + 205200}}}}),
        "usage": normalise_usage({
            "summary": {"lifetimeTokens": 123456789},
            "dailyUsageBuckets": [{"startDate": "2026-11-09", "tokens": 42000}]}),
    }


class FakeEngine:
    def __init__(self):
        self.commands = []
        self.speeches = []
        self.selected = None

    def emit(self, kind, **fields):
        command = dict(fields, type=kind)
        self.commands.append(command)
        return command

    def deliver_speech(self, text, agent_id, request_id=""):
        self.speeches.append((text, agent_id, request_id))
        return text

    def cards(self):
        return [c for c in self.commands if c["type"] == "miniapp_card"]


class FakeProvider:
    def __init__(self, error=None, delay=0.0):
        self.calls = []
        self.error = error
        self.delay = delay

    def read(self, operation):
        self.calls.append(operation)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return payload()


class RegistryTest(unittest.TestCase):
    def test_recommended_longnv_phrases(self):
        for phrase in ("查询龙女用量", "查询 龙女 额度", "若琪查询龙女额度",
                       "查一下龙女用量", "看看龙女额度", "龙女额度还剩多少"):
            hit = registry.match(phrase)
            self.assertIsNotNone(hit, phrase)
            self.assertEqual(hit.operation, "quota")

    def test_exact_phrases_and_normalisation(self):
        for phrase, operation in (
            ("查询 Codex 额度", "quota"),
            ("Codex 额度还剩多少？", "quota"),
            ("看看 Codex 用量", "quota"),
            ("查询Ｃｏｄｅｘ token 用量", "tokens"),
            ("Codex 今天用了多少 token", "tokens"),
            ("若琪查询 Codex 额度", "quota"),
            ("查询龙女的额度", "quota"),
            ("查询一下龙女用量", "quota"),
            ("龙女 今天用了多少 token", "tokens"),
        ):
            hit = registry.match(phrase)
            self.assertIsNotNone(hit, phrase)
            self.assertEqual(hit.operation, operation)

    def test_chinese_asr_spellings_fold_to_one_phrase(self):
        # The factory ASR may pick a near-homophone or the much more common
        # "小龙女" for a short Chinese provider word.
        for phrase in ("查询龙女额度", "查询小龙女额度", "查询 龙 女 额度",
                       "查询龙吕额度", "查询隆女额度", "龙女查询龙女额度"):
            hit = registry.match(phrase)
            self.assertIsNotNone(hit, phrase)
            self.assertEqual(hit.operation, "quota")
            self.assertEqual(hit.phrase, "查询龙女额度")

    def test_legacy_gpt_spellings_still_fold(self):
        # D038: "GPT" is also a device wake-word alias, so the user switched
        # to 龙女. Keep the old spellings as a compatibility fold anyway.
        for phrase in ("查询GPT额度", "查询 g p t 额度", "查询G.P.T.额度",
                       "查询cpt额度", "查询 c p t 额度",
                       "查询计屁替额度", "查询鸡屁踢额度",
                       "计屁替查询GPT额度"):
            hit = registry.match(phrase)
            self.assertIsNotNone(hit, phrase)
            self.assertEqual(hit.phrase, "查询龙女额度")

    def test_provider_less_phrases_are_gone(self):
        # A second app ("Claude 额度") is planned. These bare phrases must
        # stay unclaimed so they can be wired to that app without a fight.
        for phrase in ("查询用量", "看看用量", "查询剩余额度",
                       "额度还剩多少", "刷新用量", "查询 Claude 额度",
                       "龙女"):
            self.assertIsNone(registry.match(phrase), phrase)

    def test_actions(self):
        self.assertEqual(registry.match("刷新 龙女 用量").action, "refresh")
        self.assertEqual(registry.match("刷新 Codex 用量").action, "refresh")
        self.assertEqual(registry.match("关闭卡片").action, "hide")
        self.assertTrue(registry.match("龙女 今天用了多少 token").wants_today)

    def test_negative_examples_do_not_trigger(self):
        for phrase in ("token 是什么", "帮我写 Codex 插件", "找 Codex",
                       "找老寇", "Codex", "关闭", "Codex 帮我改个 bug",
                       "今天用了多少", "查询用量", ""):
            self.assertIsNone(registry.match(phrase), phrase)

    def test_app_metadata(self):
        apps = registry.list_apps()
        self.assertEqual(apps[0]["id"], "codex_usage")
        self.assertEqual(apps[0]["kind"], "query")


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.provider = FakeProvider()
        self.service = MiniAppService(self.engine, provider=self.provider)

    def test_one_command_carries_the_whole_layout_and_one_summary(self):
        result = self.service.query(request_id="r1")
        self.assertEqual(result.status, "ok")
        cards_sent = self.engine.cards()
        self.assertEqual(len(cards_sent), 1)
        command = cards_sent[0]
        self.assertEqual(command["action"], "show")
        self.assertEqual(command["card"]["template"], "rings")
        self.assertNotIn("image", command)
        self.assertNotIn("image_path", command)
        self.assertTrue(result.speech)          # console text, never TTS
        self.assertEqual(self.engine.speeches, [])
        self.assertEqual([c["type"] for c in self.engine.commands],
                         ["miniapp_card"])
        self.assertEqual(self.engine.selected, None)

    def test_cache_avoids_a_second_upstream_read(self):
        self.service.query(request_id="r1")
        self.service.query(request_id="r2")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.engine.cards()), 2)  # each turn presents

    def test_force_refresh_minimum_gap(self):
        self.service.query(request_id="r1")
        self.service.query(force_refresh=True, request_id="r2")
        self.service.query(force_refresh=True, request_id="r3")
        self.assertEqual(len(self.provider.calls), 2)

    def test_error_backoff_does_not_hammer_upstream(self):
        self.service.query(request_id="r1")
        self.provider.error = CodexError("timeout", "slow")
        key = ("codex_usage", "quota")
        self.service._fetched[key] = time.monotonic() - 120
        self.service._last_force[key] = time.monotonic() - 60
        result = self.service.query(force_refresh=True, request_id="r2")
        self.assertEqual(result.status, "stale")
        self.assertIn("缓存", result.speech)
        self.assertEqual(result.data["card"]["status"], "stale")
        self.service.query(request_id="r3")
        self.assertEqual(len(self.provider.calls), 2)

    def test_very_old_cache_is_not_shown_as_fresh(self):
        self.service.query(request_id="r1")
        self.provider.error = CodexError("timeout", "slow")
        key = ("codex_usage", "quota")
        self.service._fetched[key] = time.monotonic() - 3600
        self.service._last_force[key] = time.monotonic() - 60
        result = self.service.query(force_refresh=True, request_id="r2")
        self.assertEqual(result.status, "error")

    def test_concurrent_same_query_merges_with_one_display_and_no_speech(self):
        self.provider.delay = 0.15
        results = []
        threads = [threading.Thread(
            target=lambda: results.append(self.service.query(request_id="r%d" % i)))
            for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.engine.cards()), 1)
        self.assertEqual(self.engine.speeches, [])
        self.assertEqual(len(results), 3)

    def test_queries_are_display_only_on_every_route(self):
        result = self.service.query(request_id="r-http")
        self.assertTrue(result.speech)          # text summary still available
        self.assertEqual(self.engine.speeches, [])
        self.service.handle_text("查询 Codex token 用量", "r-voice")
        self.assertEqual(self.engine.speeches, [])
        types = [c["type"] for c in self.engine.commands]
        self.assertIn("miniapp_card", types)
        self.assertNotIn("speak", types)

    def test_default_card_ttl_is_30_seconds(self):
        self.assertEqual(config_module.DEFAULTS["miniapps"]["card_ttl_ms"], 30000)
        self.assertEqual(self.engine.cards(), [])
        self.service.query(request_id="r-ttl")
        self.assertEqual(self.engine.cards()[-1]["ttl_ms"], 30000)

    def test_generation_increases_but_ttl_is_not_extended(self):
        self.service.query(request_id="r1")
        first = self.engine.cards()[-1]
        self.service.query(force_refresh=True, request_id="r2")
        second = self.engine.cards()[-1]
        self.assertGreater(second["generation"], first["generation"])
        self.assertEqual(second["card_id"], first["card_id"])
        self.assertEqual(second["expires_at_ms"], first["expires_at_ms"])

    def test_card_ttl_honours_the_documented_config_key(self):
        engine = FakeEngine()
        service = MiniAppService(engine, provider=FakeProvider(),
                                 config={"card_ttl_ms": 65000})
        before_ms = int(time.time() * 1000)
        service.query(request_id="r1")
        command = engine.cards()[-1]
        self.assertEqual(command["ttl_ms"], 65000)
        self.assertGreaterEqual(command["expires_at_ms"], before_ms + 64000)
        self.assertLessEqual(command["expires_at_ms"],
                             int(time.time() * 1000) + 65000)

    def test_legacy_ttl_key_still_works(self):
        engine = FakeEngine()
        service = MiniAppService(
            engine, provider=FakeProvider(),
            config={"miniapp_card_ttl_ms": 42000})
        service.query(request_id="r1")
        self.assertEqual(engine.cards()[-1]["ttl_ms"], 42000)

    def test_generation_survives_a_host_restart(self):
        first = self.service.query(request_id="r1")
        command = self.engine.cards()[-1]
        self.assertIn("server_instance_id", command)
        fresh_engine = FakeEngine()
        fresh_engine.instance_id = "new-host-instance"
        restarted = MiniAppService(fresh_engine, provider=FakeProvider())
        second = restarted.query(request_id="r2")
        self.assertGreaterEqual(fresh_engine.cards()[-1]["generation"],
                                command["generation"])
        self.assertEqual(second.status, "ok")

    def test_hide_uses_the_current_card_id(self):
        self.service.query(request_id="r1")
        card_id = self.engine.cards()[-1]["card_id"]
        self.engine.commands.clear()
        self.service.hide()
        self.assertEqual(self.engine.cards()[-1]["action"], "hide")
        self.assertEqual(self.engine.cards()[-1]["card_id"], card_id)

    def test_reconnect_returns_unexpired_card_only(self):
        self.service.query(request_id="r1")
        command = self.service.current_command()
        self.assertEqual(command["action"], "show")
        self.service._current = (command, time.time() * 1000 - 1)
        self.assertIsNone(self.service.current_command())

    def test_voice_route(self):
        for phrase, operation in (("查询龙女额度", "quota"),
                                  ("查询龙女token 用量", "tokens")):
            answer = self.service.handle_text(phrase, "voice-1")
            self.assertEqual(answer["intent"], "miniapp")
            self.assertEqual(answer["operation"], operation)
        self.assertEqual(self.service.handle_text("token 是什么"), None)

    def test_refresh_keeps_the_last_operation(self):
        self.service.handle_text("查询龙女token 用量", "r1")
        answer = self.service.handle_text("刷新龙女用量", "r2")
        self.assertEqual(answer["operation"], "tokens")

    def test_today_question_never_claims_zero(self):
        answer = self.service.handle_text("Codex 今天用了多少 token", "r1")
        self.assertIn("没有今天", answer["speech"])
        self.assertNotIn("今天用了零", answer["speech"])

    def test_hide_phrase_closes_the_card(self):
        self.service.handle_text("查询龙女额度", "r1")
        self.engine.commands.clear()
        answer = self.service.handle_text("关闭卡片", "r2")
        self.assertEqual(answer["action"], "hide")
        self.assertEqual(self.engine.cards()[-1]["action"], "hide")

    def test_status_lists_apps_and_protocol(self):
        self.service.query(request_id="r1")
        status = self.service.status()
        self.assertEqual(status["device_card"]["protocol"], "miniapp_card.v1")
        self.assertTrue(status["device_card"]["active"])
        self.assertEqual(status["apps"][0]["id"], "codex_usage")


class EngineRoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config_module.create_config(
            Path(self.tmp.name) / "config.json")
        self.config["_root"] = self.tmp.name
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.engine = Engine(self.config,
                             state_file=str(Path(self.tmp.name) / "state.json"))
        self.engine.miniapps.provider = FakeProvider()
        self.backend_calls = []
        self.engine.backend_for = lambda agent: self._backend()

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def _backend(self):
        class Backend:
            def __init__(self, outer):
                self.outer = outer

            def run(self, *args, **kwargs):
                self.outer.backend_calls.append(args)
                return "assistant answer"
        return Backend(self)

    def event(self, text, request_id):
        return {"type": "text", "text": text, "request_id": request_id,
                "source": "console"}

    def test_query_phrase_is_answered_without_an_assistant(self):
        answer = self.engine.handle_event(
            self.event("若琪查询龙女额度", "route-1"))
        self.assertEqual(answer["intent"], "miniapp")
        self.assertEqual(self.backend_calls, [])
        self.assertIsNone(self.engine.selected)
        types = [c["type"] for c in self.engine.commands]
        self.assertIn("miniapp_card", types)
        self.assertNotIn("speak", types)

    def test_unrelated_text_still_reaches_the_assistant(self):
        self.engine.select("hermes")
        answer = self.engine.handle_event(
            self.event("帮我写个排序函数", "route-2"))
        self.assertNotEqual(answer["intent"], "miniapp")
        self.assertEqual(answer["intent"], "command")

    def test_miniapp_does_not_change_the_selected_assistant(self):
        self.engine.select("hermes")
        self.engine.handle_event(self.event("查询 Codex token 用量", "route-3"))
        self.assertEqual(self.engine.selected, "hermes")


if __name__ == "__main__":
    unittest.main()
