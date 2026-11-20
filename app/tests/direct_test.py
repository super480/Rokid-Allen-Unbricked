"""Direct mode: the split between the factory and the model."""
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
import intent  # noqa: E402
from backends.base import BackendError  # noqa: E402
from backends.direct import (FULL, HYBRID, OFF, DirectBackend,  # noqa: E402
                             OpenAiCompatBackend, normalise_mode)
from engine import Engine  # noqa: E402


class FakeBackend:
    """A backend that answers with a fixed line."""

    def __init__(self, settings=None, state_store=None):
        self.answers = []
        self.settings = settings or {}
        self.state_store = state_store

    def run(self, text, request_id, on_progress=None):
        self.answers.append(text)
        if on_progress:
            on_progress("thinking", "")
        return "收到：" + text

    def cancel(self, request_id):
        return True

    def close(self):
        pass

    def health(self):
        return True, "ok"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload.encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FactorySkillTest(unittest.TestCase):
    """The rule that says which turns belong to the factory."""

    def setUp(self):
        self.pattern = config_module.DEFAULTS["direct"]["factory_owns"]

    def test_the_clock_and_calendar_belong_to_the_factory(self):
        for said in ("现在几点了", "今天几号", "今天星期几", "报时"):
            self.assertTrue(intent.factory_owns(said, self.pattern), said)

    def test_the_system_skills_belong_to_the_factory(self):
        # Music is no longer here: the bridge owns the MediaPlayer and its
        # own local/online library, so "放一首周杰伦" must reach the host.
        # Volume used to be here too; S6 made it a bridge command so it
        # controls our own STREAM_MUSIC rather than the factory's player.
        # Weather left with D047: the factory cloud answers it with the
        # service-expiry notice, so weather.py takes the turn itself.
        for said in ("明天八点提醒我", "打开蓝牙", "重启一下"):
            self.assertTrue(intent.factory_owns(said, self.pattern), said)

    def test_weather_left_the_factory_with_d047(self):
        for said in ("今天天气怎么样", "外面气温多少度", "明天会下雨吗"):
            self.assertFalse(intent.factory_owns(said, self.pattern), said)

    def test_music_turns_belong_to_the_bridge(self):
        for said in ("放一首周杰伦", "来首歌", "听歌", "播放音乐",
                     "暂停", "下一首", "停止播放",
                     "调大音量", "静音", "音量调到50"):
            self.assertFalse(intent.factory_owns(said, self.pattern), said)

    def test_other_questions_do_not(self):
        for said in ("帮我写一首关于秋天的诗", "列一下目录", "这段代码有什么问题"):
            self.assertFalse(intent.factory_owns(said, self.pattern), said)

    def test_the_wake_word_and_the_case_do_not_matter(self):
        # The device normalises the sentence the same way, so the host has
        # to normalise it too: otherwise "WiFi" would not match "wifi".
        self.assertTrue(intent.factory_owns("若琪，打开WIFI", self.pattern))
        self.assertTrue(intent.factory_owns("若琪。现在几点了。", self.pattern))

    def test_a_broken_rule_is_ignored(self):
        self.assertFalse(intent.factory_owns("现在几点了", "(unclosed"))
        self.assertFalse(intent.factory_owns("现在几点了", ""))


class DirectSplitTest(unittest.TestCase):
    """Who answers a turn: the factory, the model, or the assistant."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config_module.create_config(Path(self.tmp.name) / "config.json")
        self.config["_root"] = self.tmp.name
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.engine = Engine(self.config, state_file=str(Path(self.tmp.name) / "s.json"))
        self.backend = FakeBackend()
        self.engine.backend_for = lambda agent_id: self.backend

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def ask(self, text, **extra):
        event = {"type": "asr_final", "text": text, "request_id": text}
        event.update(extra)
        return self.engine.handle_event(event)

    def test_off_keeps_the_factory_skills_to_the_factory(self):
        self.engine.set_direct_mode(OFF)
        result = self.ask("现在几点了")
        # The factory answers, so the host stays quiet: two replies at once
        # would be worse than no answer.
        self.assertEqual(result["intent"], "factory")
        self.assertEqual(self.backend.answers, [])

    def test_off_asks_for_an_assistant_as_before(self):
        self.engine.set_direct_mode(OFF)
        result = self.ask("帮我写首诗")
        self.assertEqual(result["intent"], "command")
        self.assertEqual(self.backend.answers, [])

    def test_hybrid_leaves_the_factory_skills_alone(self):
        self.engine.set_direct_mode(HYBRID)
        result = self.ask("现在几点了")
        self.assertEqual(result["intent"], "factory")
        self.assertEqual(self.backend.answers, [])

    def test_hybrid_hands_the_rest_to_the_model(self):
        self.engine.set_direct_mode(HYBRID)
        result = self.ask("帮我写首诗")
        self.assertEqual(result["intent"], "direct")
        self.engine.jobs.join()
        self.assertEqual(self.backend.answers, ["帮我写首诗"])

    def test_full_hands_everything_to_the_model(self):
        self.engine.set_direct_mode(FULL)
        result = self.ask("现在几点了")
        self.assertEqual(result["intent"], "direct")
        self.engine.jobs.join()
        self.assertEqual(self.backend.answers, ["现在几点了"])

    def test_a_selected_assistant_still_wins(self):
        # Choosing an assistant is a deliberate act; direct mode only
        # covers the turns nobody claimed.
        self.engine.set_direct_mode(FULL)
        self.engine.select("hermes")
        result = self.ask("帮我写首诗")
        self.assertEqual(result["intent"], "command")
        self.engine.jobs.join()
        self.assertEqual(self.backend.answers, ["帮我写首诗"])

    def test_the_mode_is_reported_to_the_device(self):
        self.engine.set_direct_mode(HYBRID)
        status = self.engine.status()
        self.assertEqual(status["direct"]["mode"], "hybrid")
        # The device needs the rule as well, so both sides decide alike.
        self.assertEqual(status["direct"]["factory_owns"],
                         self.engine.factory_pattern())

    def test_the_voice_word_switches_the_mode_on(self):
        self.engine.set_direct_mode(OFF)
        result = self.ask("用大模型")
        self.assertEqual(result["intent"], "direct")
        self.assertEqual(self.engine.direct_mode(), HYBRID)

    def test_the_default_mode_is_the_hybrid_one(self):
        self.assertEqual(config_module.DEFAULTS["direct"]["mode"], HYBRID)

    def test_an_unknown_mode_falls_back_to_off(self):
        self.assertEqual(normalise_mode("nonsense"), OFF)
        self.assertEqual(normalise_mode(None), OFF)
        self.assertEqual(normalise_mode(" FULL "), FULL)


class OpenAiCompatTest(unittest.TestCase):
    """Calling an OpenAI-compatible endpoint."""

    def make(self, **settings):
        base = {
            "base_url": "https://example.test/v1",
            "api_key": "secret",
            "model": "some-model",
            "timeout_seconds": 5,
        }
        base.update(settings)
        return OpenAiCompatBackend(base, state_store={})

    def test_health_needs_a_url_a_key_and_a_model(self):
        ok, detail = self.make(base_url="").health()
        self.assertFalse(ok)
        self.assertIn("接口地址", detail)
        ok, detail = self.make(api_key="").health()
        self.assertFalse(ok)
        self.assertIn("api key", detail)
        ok, detail = self.make(model="").health()
        self.assertFalse(ok)

    def test_the_endpoint_is_built_from_the_base_url(self):
        self.assertEqual(self.make().endpoint(),
                         "https://example.test/v1/chat/completions")
        # A full endpoint is accepted as it is.
        self.assertEqual(
            self.make(base_url="https://example.test/v1/chat/completions").endpoint(),
            "https://example.test/v1/chat/completions")

    def test_the_answer_is_read_from_the_response(self):
        backend = self.make()
        payload = {"choices": [{"message": {"content": " 回答在这里 "}}]}

        import backends.direct as direct_module
        original = direct_module.urllib.request.urlopen

        def fake_urlopen(request, timeout=None):
            fake_urlopen.last = request
            return FakeResponse(json.dumps(payload))

        direct_module.urllib.request.urlopen = fake_urlopen
        try:
            answer = backend.run("你好", "r1")
        finally:
            direct_module.urllib.request.urlopen = original
        self.assertEqual(answer, "回答在这里")
        # The api key goes into the header, the model into the body.
        self.assertEqual(fake_urlopen.last.get_header("Authorization"),
                         "Bearer secret")
        body = json.loads(fake_urlopen.last.data.decode("utf-8"))
        self.assertEqual(body["model"], "some-model")
        self.assertEqual(body["messages"][-1]["content"], "你好")

    def test_the_conversation_is_kept_for_the_next_turn(self):
        backend = self.make()
        store = {}
        backend.state_store = store

        import backends.direct as direct_module
        original = direct_module.urllib.request.urlopen
        answers = ["第一句", "第二句"]

        def fake_urlopen(request, timeout=None):
            return FakeResponse(json.dumps(
                {"choices": [{"message": {"content": answers.pop(0)}}]}))

        direct_module.urllib.request.urlopen = fake_urlopen
        try:
            backend.run("第一个问题", "r1")
            backend.run("第二个问题", "r2")
        finally:
            direct_module.urllib.request.urlopen = original

        messages = store["direct_messages"]
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[1]["role"], "assistant")

    def test_a_failing_endpoint_is_reported(self):
        backend = self.make()

        import backends.direct as direct_module
        original = direct_module.urllib.request.urlopen

        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError("no route to host")

        direct_module.urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(BackendError):
                backend.run("你好", "r1")
        finally:
            direct_module.urllib.request.urlopen = original

    def test_an_endpoint_that_is_not_configured_is_refused(self):
        backend = self.make(base_url="")
        with self.assertRaises(BackendError):
            backend.run("你好", "r1")


class DirectBackendTest(unittest.TestCase):
    """Picking the source of the answer."""

    def test_the_assistant_source_uses_a_session_of_its_own(self):
        store = {}
        backend = DirectBackend({"source": "assistant", "assistant": "hermes",
                                 "assistant_settings": {"command": ["hermes"]}},
                                store)
        inner = backend.inner()
        # The assistant's own conversation must not be the direct one.
        self.assertEqual(inner.session_key, "direct_session")

    def test_the_custom_source_is_used_when_it_is_chosen(self):
        backend = DirectBackend({"source": "custom",
                                 "custom": {"base_url": "https://x.test/v1",
                                            "api_key": "k", "model": "m"}}, {})
        inner = backend.inner()
        self.assertIsInstance(inner, OpenAiCompatBackend)
        self.assertEqual(inner.endpoint(), "https://x.test/v1/chat/completions")

    def test_switching_the_model_reaches_the_inner_backend(self):
        """The direct conversation has its own backend and session.

        Changing the model of the assistant the direct mode reuses does
        not reach it on its own, so the direct thread would keep
        answering with the old model.
        """
        store = {}
        backend = DirectBackend({"source": "assistant", "assistant": "hermes",
                                 "assistant_settings": {"command": ["hermes"],
                                                        "model": "model-old"}},
                                store)
        inner = backend.inner()
        self.assertEqual(inner.model, "model-old")
        backend.switch_model("model-new")
        self.assertEqual(inner.model, "model-new")

    def test_switching_the_model_drops_the_direct_session(self):
        """The old session runs on the old model and can not be kept."""
        store = {"direct_session": "local-old",
                 "direct_session_model": "model-old"}
        backend = DirectBackend({"source": "assistant", "assistant": "hermes",
                                 "assistant_settings": {"command": ["hermes"],
                                                        "model": "model-old"}},
                                store)
        inner = backend.inner()
        backend.switch_model("model-new")
        self.assertNotIn("direct_session", store)
        # The next turn starts a fresh session that carries the model.
        self.assertEqual(inner.session_for_model(), "")
        self.assertIn("--model", inner.build_argv("你好", inner.session_for_model()))

    def test_the_settings_snapshot_keeps_the_new_model(self):
        """A rebuilt inner backend must get the model that was chosen."""
        backend = DirectBackend({"source": "assistant", "assistant": "hermes",
                                 "assistant_settings": {"command": ["hermes"],
                                                        "model": "model-old"}},
                                {})
        backend.switch_model("model-new")
        self.assertEqual(backend.settings["assistant_settings"]["model"], "model-new")

    def test_switching_the_custom_model_rebuilds_the_inner_backend(self):
        backend = DirectBackend({"source": "custom",
                                 "custom": {"base_url": "https://x.test/v1",
                                            "api_key": "k", "model": "m-old"}},
                                {})
        backend.switch_model("m-new")
        inner = backend.inner()
        self.assertEqual(inner.model, "m-new")


if __name__ == "__main__":
    unittest.main()
