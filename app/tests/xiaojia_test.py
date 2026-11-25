"""The sixth assistant: 小家 (HA's conversation agent).

Covers the registry entry, its wake word, the backend that talks to HA,
and home-turn ownership: 若琪 forwards to the HA bridge, 小家 answers
through its own backend, every other assistant ignores the sentence.
Every HA call is mocked at the transport boundary
(00-START-HERE §5.8): no test reaches a real Home Assistant.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
import engine as engine_module  # noqa: E402
import intent  # noqa: E402
from backends import load_backend  # noqa: E402
from backends.ha_assistant import HaAssistantBackend, SESSION_WINDOW  # noqa: E402
from engine import Engine, HOME_REDIRECT_HINT  # noqa: E402
from homeassistant.client import HaNetworkError  # noqa: E402


class FakeTransport:
    """Scripted (status, body) queue; records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "body": body})
        item = self.responses.pop(0) if self.responses else (200, b"{}")
        if isinstance(item, Exception):
            raise item
        status, raw = item
        return status, raw if isinstance(raw, bytes) else raw.encode("utf-8")


def body(speech="好的，已打开茶几灯", conversation_id=""):
    data = {"response": {"speech": {"plain": {"speech": speech}}}}
    if conversation_id:
        data["conversation_id"] = conversation_id
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


class FakeBackend:
    def __init__(self, settings=None, state_store=None):
        self.answers = []
        # 小家 backend keeps the raw HA payload of its last turn; the
        # engine parses it to draw 小家's feedback card.
        self.last_payload = {}

    def run(self, text, request_id, on_progress=None):
        self.answers.append(text)
        self.last_payload = json.loads(
            body("好的，已打开茶几灯").decode("utf-8"))
        return "收到：" + text

    def cancel(self, request_id):
        return True

    def close(self):
        pass

    def health(self):
        return True, "ok"


class RegistryTest(unittest.TestCase):
    def test_xiaojia_is_registered(self):
        item = config_module.REGISTRY["xiaojia"]
        self.assertEqual(item["name"], "小家")
        # backend id must equal assistant id: DirectBackend.inner() calls
        # load_backend(assistant_id) when 若琪 reuses an assistant.
        self.assertEqual(item["backend"], "xiaojia")
        self.assertEqual(item["config_section"], "homeassistant")

    def test_hermes_display_name_is_xiaohe(self):
        self.assertEqual(config_module.REGISTRY["hermes"]["name"], "小赫")
        self.assertEqual(config_module.agent_name("hermes"), "小赫")

    def test_backend_sections_and_loader(self):
        self.assertIn("xiaojia", config_module.BACKEND_SECTIONS)
        backend = load_backend("xiaojia", {}, {})
        self.assertIsInstance(backend, HaAssistantBackend)

    def test_ha_section_is_shared_not_duplicated(self):
        # Only one place holds the token; the assistant section is choices.
        self.assertIn("token", config_module.DEFAULTS["homeassistant"])
        self.assertNotIn("token", config_module.DEFAULTS["xiaojia"])
        self.assertNotIn("base_url", config_module.DEFAULTS["xiaojia"])


class WakeWordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config_module.create_config(
            Path(self.tmp.name) / "config.json")
        self.config["_root"] = self.tmp.name
        self.engine = Engine(self.config,
                             state_file=str(Path(self.tmp.name) / "state.json"))

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def test_defaults_carry_the_agreed_word(self):
        row = config_module.DEFAULTS["wake"]["agents"]["xiaojia"]
        self.assertEqual(row["pinyin"], "xiao3jia1")
        self.assertEqual(row["aliases"], ["小家", "小佳", "小嘉", "小加"])

    def test_wake_rows_and_no_duplicate_vt_word(self):
        self.engine.set_wake(enabled=True)
        agents = self.engine.wake_settings()["agents"]
        self.assertEqual(set(agents), set(config_module.enabled_agents()))
        self.assertEqual(agents["xiaojia"]["vt_word"],
                         "小家|小佳|小嘉|小加")
        self.assertEqual(agents["hermes"]["vt_word"],
                         "小赫|小鹤|小贺|小荷|小褐")
        # 克劳德: display name first, then the sound-alike spellings.
        self.assertEqual(agents["claude"]["vt_word"],
                         "克劳德|柯劳德|科劳德|刻劳德|克劳得|克老德|克劳的|claude|cloud")

    def test_label_and_prefix_route_to_xiaojia(self):
        self.engine.set_wake(enabled=True)
        spellings = self.engine.wake_spellings()
        row = self.engine.wake_settings()["agents"]
        pinyins = {k: v["pinyin"] for k, v in row.items()
                   if v["enabled"] and v["pinyin"]}
        self.assertEqual(intent.wake_word_agent(
            "小家|小佳|小嘉|小加", spellings, pinyins), "xiaojia")
        self.assertEqual(intent.wake_word_agent(
            "小佳", spellings, pinyins), "xiaojia")
        agent, rest = intent.wake_prefix("小家打开茶几灯", spellings)
        self.assertEqual((agent, rest), ("xiaojia", "打开茶几灯"))
        self.assertEqual(intent.parse_intent("找小家")[0], "select")
        self.assertEqual(intent.parse_intent("退出小家")[0], "exit")


class BackendTest(unittest.TestCase):
    def make(self, transport, **extra):
        settings = {"base_url": "http://ha.test:8123", "token": "t",
                    "agent_id": "conversation.deepseek_flash",
                    "transport": transport}
        settings.update(extra)
        return HaAssistantBackend(settings, {})

    def test_one_turn_speaks_the_agents_reply(self):
        transport = FakeTransport((200, body("客厅灯已打开")))
        backend = self.make(transport)
        reply = backend.run("打开客厅灯", "r1")
        self.assertEqual(reply, "客厅灯已打开")
        self.assertEqual(len(transport.calls), 1)
        sent = json.loads(transport.calls[0]["body"].decode("utf-8"))
        self.assertEqual(sent["text"], "打开客厅灯")
        self.assertEqual(sent["agent_id"], "conversation.deepseek_flash")
        self.assertNotIn("conversation_id", sent)

    def test_conversation_id_is_kept_across_turns(self):
        transport = FakeTransport((200, body("你想打开哪个灯？", "c-1")),
                                  (200, body("茶几灯已打开", "c-2")))
        backend = self.make(transport)
        backend.run("打开灯", "r1")
        backend.run("茶几灯", "r2")
        second = json.loads(transport.calls[1]["body"].decode("utf-8"))
        self.assertEqual(second["conversation_id"], "c-1")

    def test_session_expires_and_starts_fresh(self):
        transport = FakeTransport((200, body("好的", "c-1")),
                                  (200, body("好的", "c-2")))
        backend = self.make(transport)
        backend.run("第一句", "r1")
        backend._last_turn_at = time.monotonic() - SESSION_WINDOW - 1
        backend.run("第二句", "r2")
        second = json.loads(transport.calls[1]["body"].decode("utf-8"))
        self.assertNotIn("conversation_id", second)

    def test_keep_session_off_never_continues(self):
        transport = FakeTransport((200, body("好的", "c-1")),
                                  (200, body("好的", "c-2")))
        backend = self.make(transport, keep_session=False)
        backend.run("第一句", "r1")
        backend.run("第二句", "r2")
        second = json.loads(transport.calls[1]["body"].decode("utf-8"))
        self.assertNotIn("conversation_id", second)

    def test_missing_connection_fails_without_a_request(self):
        transport = FakeTransport()
        backend = HaAssistantBackend({"transport": transport}, {})
        with self.assertRaises(Exception) as caught:
            backend.run("打开灯", "r1")
        self.assertIn("地址和令牌", str(caught.exception))
        self.assertEqual(len(transport.calls), 0)

    def test_network_error_is_a_safe_backend_error(self):
        transport = FakeTransport(HaNetworkError("连不上 Home Assistant"))
        backend = self.make(transport)
        with self.assertRaises(Exception) as caught:
            backend.run("打开灯", "r1")
        self.assertIn("连不上", str(caught.exception))

    def test_empty_reply_has_a_fallback(self):
        transport = FakeTransport((200, body("")))
        backend = self.make(transport)
        self.assertIn("没有听懂", backend.run("嗯", "r1"))

    def test_model_menu_and_health(self):
        backend = self.make(FakeTransport())
        self.assertEqual(backend.models(), ["conversation.deepseek_flash"])
        self.assertEqual(backend.current_model(),
                         "conversation.deepseek_flash")
        self.assertTrue(backend.switch_model("conversation.other"))
        self.assertEqual(backend.models(), ["conversation.other"])
        self.assertEqual(backend.health(), (True, "ok"))
        self.assertFalse(HaAssistantBackend({}, {}).health()[0])


class RoutingTest(unittest.TestCase):
    """家居语音归属（用户 2026-11-20 重述后的规则）。

    若琪（未选助手） -> 门控转发 HA；小家 -> 直接交给它自己的 HA 后端；
    其它助手 -> 不执行，只回一句「控制家居设备请找小家」。
    音乐/音量/小应用/天气/表盘链路一律不变。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config_module.create_config(
            Path(self.tmp.name) / "config.json")
        self.config["_root"] = self.tmp.name
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.engine = Engine(self.config,
                             state_file=str(Path(self.tmp.name) / "state.json"))
        self.backend = FakeBackend()
        self.engine.backend_for = lambda agent_id: self.backend
        self.counter = 0
        # The gate is a local regex: enabling it opens no socket.
        self.engine.homeassistant.config.update({
            "enabled": True, "base_url": "http://ha.test:8123",
            "token": "test-token"})
        # The forwarding path itself is always mocked (00-START-HERE §5.8).
        self.bridge_hits = []
        self.bridge_reply = None

        def bridge(text, request_id=""):
            self.bridge_hits.append(text)
            return self.bridge_reply

        self.engine.homeassistant.handle_text = bridge

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def queued_bridge(self):
        self.bridge_reply = {"accepted": True, "intent": "homeassistant",
                             "status": "queued"}

    def say(self, text):
        self.counter += 1
        result = self.engine.handle_event({
            "type": "asr_final", "text": text,
            "request_id": "r%d" % self.counter})
        self.engine.jobs.join()
        return result

    def test_ruoqi_forwards_home_sentence_to_ha(self):
        self.queued_bridge()
        self.assertIsNone(self.engine.selected)      # 若琪 = 没喊任何助手
        result = self.say("打开茶几灯")
        self.assertEqual(result["intent"], "homeassistant")
        self.assertEqual(self.bridge_hits, ["打开茶几灯"])
        self.assertEqual(self.backend.answers, [])

    def test_xiaojia_takes_home_sentence_without_forwarding(self):
        self.say("找小家")
        self.assertEqual(self.engine.selected, "xiaojia")
        result = self.say("打开茶几灯")
        self.assertEqual(result["intent"], "command")
        self.assertEqual(result["agent"], "xiaojia")
        self.assertEqual(self.backend.answers, ["打开茶几灯"])
        # 「找小家」这句还在若琪状态下，会走一次澄清窗口探测（返回
        # None），但它不是家居转发；家居句本身绝不能进 bridge。
        self.assertNotIn("打开茶几灯", self.bridge_hits)

    def test_xiaojia_home_turn_shows_a_result_card(self):
        """小家 answers through HA itself, but still needs a screen card.

        The card and the spoken line must share one card_id: the device
        then queues the speech behind any TTS still winding down
        (speakForCard) instead of submitting it into a factory cancel.
        """
        self.say("找小家")
        self.say("打开茶几灯")
        cards = [c for c in self.engine.commands
                 if c.get("type") == "miniapp_card"]
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["card"]["layout"], "home_feedback")
        # 小家 shares the HA card TTL with 若琪: no separate, longer window.
        self.assertEqual(card["ttl_ms"], 8000)
        self.assertEqual(card["ttl_ms"],
                         self.engine.homeassistant.config["card_ttl_ms"])
        speaks = [c for c in self.engine.commands
                  if c.get("type") == "speak" and c.get("text")]
        spoken = speaks[-1]                 # last speak is the home answer
        self.assertEqual(spoken["agent"], "xiaojia")
        self.assertEqual(spoken["card_id"], card["card_id"])
        self.assertEqual(spoken["expires_at_ms"], card["expires_at_ms"])
        self.assertGreater(spoken["expires_at_ms"], 0)
        # The card must reach the device before the line that binds to it.
        self.assertLess(card["seq"], spoken["seq"])
        # The HA payload from the backend is what fed the card.
        job = self.engine.homeassistant.jobs()[0]
        self.assertEqual(job["status"], "reported_success")
        self.assertEqual(job["verification"], "not_checked")
        # Still no forwarding: 小家 never goes through the bridge.
        self.assertNotIn("打开茶几灯", self.bridge_hits)

    def test_xiaojia_normal_question_has_no_home_card(self):
        """Only a home turn draws 小家's HA feedback card."""
        self.say("找小家")
        self.say("讲个笑话")
        cards = [c for c in self.engine.commands
                 if c.get("type") == "miniapp_card"]
        self.assertEqual(cards, [])
        speaks = [c for c in self.engine.commands
                  if c.get("type") == "speak" and c.get("text")]
        self.assertTrue(speaks)
        self.assertNotIn("card_id", speaks[-1])

    def test_xiaojia_gets_parameter_sentence_not_volume(self):
        # 「亮度设成70%」不能落到音量规则上。
        self.say("找小家")
        sentence = "把茶几灯调成黄色，亮度设成70%"
        result = self.say(sentence)
        self.assertEqual(result["intent"], "command")
        self.assertEqual(self.backend.answers, [sentence])
        self.assertNotIn(sentence, self.bridge_hits)

    def test_forced_prefix_is_stripped_for_xiaojia(self):
        self.say("找小家")
        self.say("智能家居打开茶几灯")
        self.assertEqual(self.backend.answers, ["打开茶几灯"])

    def test_other_assistants_redirect_home_sentences(self):
        """不执行、不转发、不进模型，只回一句固定指引。"""
        self.queued_bridge()
        for agent in ("hermes", "codex", "dsh", "workbuddy", "neko", "claude"):
            with self.subTest(agent=agent):
                self.engine.select(agent, announce=False)
                self.engine.last_utterance = ("", 0)
                result = self.say("打开茶几灯")
                self.assertEqual(result["intent"], "homeassistant")
                self.assertEqual(result["status"], "redirected")
                self.assertEqual(result["selected_agent"], agent)
                self.assertEqual(result["speech"], HOME_REDIRECT_HINT)
                self.assertEqual(self.bridge_hits, [])
                self.assertEqual(self.backend.answers, [])
        speaks = [c.get("text") for c in self.engine.commands
                  if c.get("type") == "speak"]
        self.assertEqual(speaks.count(HOME_REDIRECT_HINT), 6)
        self.assertIn(HOME_REDIRECT_HINT,
                      [line["message"] for line in self.engine.transcript])

    def test_other_assistants_still_answer_normal_questions(self):
        self.engine.select("hermes", announce=False)
        result = self.say("列一下目录")
        self.assertEqual(result["intent"], "command")
        self.assertEqual(self.backend.answers, ["列一下目录"])
        self.assertEqual(self.bridge_hits, [])

    def test_local_skills_keep_priority_for_every_assistant(self):
        calls = []

        def weather(text, request_id=""):
            if "天气" in text:
                calls.append("weather")
                return {"accepted": True, "intent": "weather"}
            return None

        def clock(text, request_id=""):
            if "表盘" in text:
                calls.append("clock")
                return {"accepted": True, "intent": "clock"}
            return None

        def mini(text, request_id=""):
            if "拓竹" in text:
                calls.append("miniapp")
                return {"accepted": True, "intent": "miniapp"}
            return None

        self.engine.weather.handle_text = weather
        self.engine.clock.handle_text = clock
        self.engine.miniapps.handle_text = mini
        self.engine.apply_music_volume = (
            lambda volume, request_id="":
            {"accepted": True, "intent": "music_volume"})

        cases = [
            ("xiaojia", "今天天气怎么样", "weather"),
            ("hermes", "明天天气如何", "weather"),
            (None, "换个表盘看看", "clock"),
            ("xiaojia", "音量调到50", "music_volume"),
            ("hermes", "拓竹打印进度", "miniapp"),
        ]
        for agent, sentence, expected in cases:
            with self.subTest(agent=agent, sentence=sentence):
                if agent:
                    self.engine.select(agent, announce=False)
                else:
                    self.engine.exit(announce=False)
                self.engine.last_utterance = ("", 0)
                result = self.say(sentence)
                self.assertEqual(result["intent"], expected)
        self.assertEqual(len(calls), 4)       # weather x2, clock, miniapp
        self.assertEqual(self.backend.answers, [])
        # 若琪 的非家居句仍会问一次 bridge（多轮澄清探测），但绝不
        # 因为本地技能被跳过而把家居句送给别的 agent。
        self.assertTrue(all("打开" not in hit for hit in self.bridge_hits))

    def test_meta_commands_still_exit_xiaojia(self):
        self.say("找小家")
        result = self.say("退出助手")
        self.assertEqual(result["intent"], "exit")
        self.assertIsNone(self.engine.selected)
        self.assertEqual(self.backend.answers, [])

    def test_ip_change_drops_cached_ha_backend(self):
        # 改 IP 必须马上生效：backend 的 settings 是构造时拷贝的。
        self.config["homeassistant"]["base_url"] = "http://ha.old:8123"
        old = Engine.backend_for(self.engine, "xiaojia")
        self.assertEqual(old.settings["base_url"], "http://ha.old:8123")
        self.engine.homeassistant.update_settings(
            base_url="http://ha.new:8123", keep_token=True)
        self.assertNotIn("xiaojia", self.engine.backends)
        new = Engine.backend_for(self.engine, "xiaojia")
        self.assertIsNot(old, new)
        self.assertEqual(new.settings["base_url"], "http://ha.new:8123")
        self.assertEqual(new.settings["token"], "test-token")

    def test_detect_shows_lan_ha_address(self):
        from unittest import mock
        self.config["homeassistant"]["base_url"] = "http://192.0.2.4:8123"
        with mock.patch.object(engine_module.socket,
                               "create_connection") as conn:
            info = self.engine.detect_local_agents()["xiaojia"]
        self.assertTrue(info["available"])
        self.assertIn("192.0.2.4:8123", info["detail"])
        self.assertTrue(conn.called)

    def test_detect_reports_unreachable_ha(self):
        from unittest import mock
        self.config["homeassistant"]["base_url"] = "http://192.0.2.4:8123"
        with mock.patch.object(engine_module.socket, "create_connection",
                               side_effect=OSError("down")):
            info = self.engine.detect_local_agents()["xiaojia"]
        self.assertFalse(info["available"])
        self.assertIn("连不上", info["detail"])

    def test_backend_for_shares_the_ha_connection(self):
        # A fresh engine (no monkeypatched backend_for) must hand the
        # homeassistant section's address/token to the new backend.
        self.config["homeassistant"]["base_url"] = "http://ha.test:8123"
        self.config["homeassistant"]["token"] = "secret-token"
        backend = Engine.backend_for(self.engine, "xiaojia")
        self.assertIsInstance(backend, HaAssistantBackend)
        self.assertEqual(backend.settings["base_url"], "http://ha.test:8123")
        self.assertEqual(backend.settings["token"], "secret-token")

    def test_direct_mode_can_reuse_xiaojia(self):
        # 若琪（直连）可选助手列表由 status() 的 agents 驱动, 小家 必在其中。
        status = self.engine.status()
        self.assertIn("xiaojia", status["agents"])
        self.assertEqual(status["agents"]["xiaojia"]["name"], "小家")

    def test_direct_mode_reuse_gets_the_ha_connection(self):
        from backends.direct import DirectBackend
        self.config["homeassistant"]["base_url"] = "http://ha.test:8123"
        self.config["homeassistant"]["token"] = "secret-token"
        self.config["direct"]["assistant"] = "xiaojia"
        self.config["direct"]["source"] = "assistant"
        settings = self.engine.direct_settings(for_backend=True)
        inner_settings = settings["assistant_settings"]
        self.assertEqual(inner_settings["base_url"], "http://ha.test:8123")
        self.assertEqual(inner_settings["token"], "secret-token")
        direct = DirectBackend(settings, {})
        self.assertIsInstance(direct.inner(), HaAssistantBackend)
        self.assertEqual(direct.inner().settings["token"], "secret-token")
        # The console-facing shape (returned by set_direct_settings) must
        # never carry the HA token.
        public = self.engine.direct_settings()
        self.assertNotIn("token", public["assistant_settings"])
        self.assertNotIn("base_url", public["assistant_settings"])
        self.assertNotIn("secret-token", json.dumps(public, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
