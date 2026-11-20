"""Home Assistant batch 2: the voice path.

Gate routing (nine-class utterance table), the command queue (dedup,
overflow, no retry), conversation-result parsing (unknown is not a
green success) and presentation. Everything is mocked at the transport
boundary -- 00-START-HERE §5.8: no test may reach a real HA, let alone
move a real device.
"""
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from homeassistant import (  # noqa: E402
    HaAuthError, HaNetworkError, HomeAssistantService,
)
from homeassistant import routing  # noqa: E402
from homeassistant.cards import result_card, speech_text  # noqa: E402
from homeassistant.models import CARD_STATUS, HaResult  # noqa: E402
from homeassistant.routing import Gate  # noqa: E402


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


class FakeEngine:
    """Records emit()/log_transcript() instead of touching the device."""

    def __init__(self):
        self.instance_id = "test-instance"
        self.events = []
        self.logs = []

    def emit(self, kind, **payload):
        self.events.append((kind, payload))

    def log_transcript(self, role, agent, text, request_id=""):
        self.logs.append((role, agent, text, request_id))


def conversation_body(speech="好的，已打开茶几灯", success=None, failed=None,
                     conversation_id=""):
    data = {}
    if success is not None:
        data["success"] = success
    if failed is not None:
        data["failed"] = failed
    body = {
        "response": {
            "speech": {"plain": {"speech": speech}},
            "data": data,
        },
    }
    if conversation_id:
        body["conversation_id"] = conversation_id
    return json.dumps(body).encode("utf-8")


class GateRoutingTest(unittest.TestCase):
    """The nine-class utterance audit from the planning session."""

    def setUp(self):
        self.gate = Gate([])

    def assert_route(self, sentence, taken):
        ok, rest = self.gate.route(sentence)
        self.assertEqual(ok, taken, sentence)
        if taken:
            self.assertTrue(rest)

    def test_home_control_taken(self):
        for sentence in ("打开茶几灯", "关掉书房的灯", "把书房灯调到50%",
                         "主卧多少度", "书房灯开着吗", "开灯"):
            self.assert_route(sentence, True)

    def test_volume_and_music_left_alone(self):
        for sentence in ("调到50%", "大点声", "暂停", "下一首", "打开音乐",
                         "我想听晴天"):
            self.assert_route(sentence, False)

    def test_set_become_family_taken(self):
        # 「调成/设成/换成/改成」族（2026-11-19 补丁）：颜色、亮度、
        # 模式参数句必须接管——整句转 HA，参数解析归 HA 的 AI。
        for sentence in ("把茶几灯调成黄色，亮度设成70%",
                         "把茶几灯调成黄色", "把书房灯调成暖白色",
                         "把空调换成制冷模式", "把茶几灯改成暖光",
                         "把加湿器开到最大"):
            self.assert_route(sentence, True)

    def test_set_become_family_collisions_stay_out(self):
        # 有界动词的反面：无家居词的参数句、点歌改播放模式、问句
        # 解释都不许被吞（音量/音乐/助手链路优先级不动）。
        for sentence in ("亮度设成70%", "音量调成50%",
                         "把这首歌改成单曲循环", "设置闹钟七点",
                         "怎么把茶几灯调成黄色"):
            self.assert_route(sentence, False)

    def test_chat_and_queries_left_alone(self):
        for sentence in ("查询网速", "今天天气", "换个表盘",
                         "怎么打开书房灯", "写个控制书房灯的代码"):
            self.assert_route(sentence, False)

    def test_forced_prefix_takes_anything(self):
        ok, rest = self.gate.route("智能家居，把加湿器打开")
        self.assertTrue(ok)
        self.assertEqual(rest, "把加湿器打开")

    def test_question_marker_excluded_but_query_verbs_ok(self):
        ok, _ = self.gate.route("怎么打开书房灯")
        self.assertFalse(ok)
        ok, _ = self.gate.route("书房灯开着吗")
        self.assertTrue(ok)

    def test_extra_aliases_join_the_gate(self):
        gate = Gate(["那个啥"])
        ok, _ = gate.route("打开那个啥")
        self.assertTrue(ok)
        ok, _ = self.gate.route("打开那个啥")
        self.assertFalse(ok)   # base gate has no such word

    def test_device_rule_bounded_and_compilable(self):
        extras = ["别名%03d" % i for i in range(60)]
        gate = Gate(extras)
        rule = gate.device_rule
        self.assertTrue(rule)
        self.assertLessEqual(len(rule), routing.MAX_RULE_CHARS)
        import re
        re.compile(rule)   # must compile on the host side too
        self.assertTrue(set(routing.DEVICE_WORDS + routing.ROOM_WORDS) <= set(gate.home_words))

    def test_device_rule_truncates_or_refuses(self):
        # A synthetic oversized dual-lookahead: the guard must either
        # return a compiling shrink or "" -- never a broken regex the
        # device would silently keep.
        verbs = routing._alternation(routing.HOME_VERBS)
        homes = routing._alternation(["词%03d" % i for i in range(120)])
        pattern = r"(?=[\s\S]{0,16}(?:%s))(?=[\s\S]{0,16}(?:%s))" % (
            verbs, homes)
        self.assertGreater(len(pattern), routing.MAX_RULE_CHARS)
        fitted = routing.fit_device_rule(pattern)
        self.assertLessEqual(len(fitted), routing.MAX_RULE_CHARS)
        if fitted:
            import re
            re.compile(fitted)
        else:
            self.assertEqual(fitted, "")

    def test_empty_rule_means_never(self):
        self.assertEqual(routing.fit_device_rule(""), "")
        gate = Gate([])
        self.assertTrue(gate.device_rule)


class VoicePathTest(unittest.TestCase):
    def make(self, transport=None, enabled=True, **config):
        self.engine = FakeEngine()
        settings = {"enabled": enabled, "base_url": "http://ha:8123",
                    "token": "secret-token", "language": "zh-CN",
                    "agent_id": "conversation.test",
                    "request_timeout_seconds": 15, "speak_results": True,
                    "overview_entities": [], "aliases": {},
                    "gate_extras": [], "shortcuts": []}
        settings.update(config)
        self.saved = []
        return HomeAssistantService(
            config=settings, save=lambda: self.saved.append(1),
            transport=transport, engine=self.engine)

    def test_disabled_returns_none(self):
        service = self.make(enabled=False, transport=FakeTransport())
        self.assertIsNone(service.handle_text("打开茶几灯"))
        self.assertEqual(service.device_rule(), "")

    def test_non_home_sentence_returns_none(self):
        service = self.make(transport=FakeTransport())
        self.assertIsNone(service.handle_text("调到50%"))
        self.assertIsNone(service.handle_text("今天天气"))

    def test_handle_text_queues_and_worker_runs(self):
        transport = FakeTransport((200, conversation_body()))
        service = self.make(transport=transport)
        result = service.handle_text("打开茶几灯", request_id="r1")
        self.assertEqual(result["status"], "queued")
        service._queue.join()
        jobs = service.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "reported_success")
        self.assertEqual(len(transport.calls), 1)
        self.assertIn(b"agent_id", transport.calls[0]["body"])

    def test_request_id_dedup(self):
        transport = FakeTransport((200, conversation_body()))
        service = self.make(transport=transport)
        service.handle_text("打开茶几灯", request_id="r-dup")
        service._queue.join()
        again = service.handle_text("打开茶几灯", request_id="r-dup")
        self.assertTrue(again.get("duplicate"))
        service._queue.join()
        self.assertEqual(len(transport.calls), 1)

    def test_queue_overflow_drops_and_reports(self):
        gate_ready = threading.Event()
        release = threading.Event()

        def slow_execute(sentence, request_id="", conversation_id=""):
            gate_ready.set()
            release.wait(timeout=5)
            return HaResult(request_id=request_id, status="unknown",
                            message="slow")

        service = self.make(transport=FakeTransport())
        service._execute = slow_execute
        service.handle_text("打开客厅灯", request_id="q0")
        self.assertTrue(gate_ready.wait(timeout=5))
        for index in range(1, 5):
            service.handle_text("打开书房灯", request_id="q%d" % index)
        # Queue size is 4: one running + three queued takes four; the
        # next one overflows and must be dropped, not block the engine.
        overflow = service.handle_text("打开卧室灯", request_id="q5")
        self.assertEqual(overflow["status"], "dropped")
        release.set()
        service._queue.join()

    def test_no_retry_on_network_error(self):
        boom = HaNetworkError("连不上 Home Assistant：x")
        transport = FakeTransport(boom)
        service = self.make(transport=transport)
        result = service.command("打开茶几灯", request_id="r2")
        # A timeout can still have run the command: unknown, one call.
        self.assertEqual(result["result"]["status"], "unknown")
        self.assertEqual(len(transport.calls), 1)

    def test_auth_error_is_failed(self):
        transport = FakeTransport(HaAuthError("访问令牌无效"))
        service = self.make(transport=transport)
        result = service.command("打开茶几灯", request_id="r3")
        self.assertEqual(result["result"]["status"], "failed")

    def test_command_rejects_non_home_sentence(self):
        transport = FakeTransport()
        service = self.make(transport=transport)
        result = service.command("今天天气怎么样")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(len(transport.calls), 0)

    def test_speak_off_stays_silent(self):
        transport = FakeTransport((200, conversation_body()))
        service = self.make(transport=transport, speak_results=False)
        service.command("打开茶几灯", request_id="r4")
        kinds = [kind for kind, _ in self.engine.events]
        self.assertIn("miniapp_card", kinds)
        self.assertNotIn("speak", kinds)

    def test_speak_on_emits_brief_confirmation(self):
        transport = FakeTransport((200, conversation_body()))
        service = self.make(transport=transport)
        service.command("打开茶几灯", request_id="r5")
        spoken = [p for k, p in self.engine.events if k == "speak"]
        self.assertEqual(len(spoken), 1)
        self.assertIn("茶几灯", spoken[0]["text"])

    def test_speak_is_bound_to_its_result_card(self):
        """若琪's home announcement must go through device speakForCard.

        A bare speak submitted right after a factory TTS cancel wedges the
        engine (tts_error error=2), so the line is never heard even though
        the card is on screen. Binding it to the card makes the device wait
        for the previous TTS request to finish.
        """
        transport = FakeTransport((200, conversation_body()))
        service = self.make(transport=transport)
        service.command("打开茶几灯", request_id="r-card")
        cards = [p for k, p in self.engine.events if k == "miniapp_card"]
        spoken = [p for k, p in self.engine.events if k == "speak"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(len(spoken), 1)
        self.assertEqual(spoken[0]["card_id"], cards[0]["card_id"])
        self.assertEqual(spoken[0]["expires_at_ms"],
                         cards[0]["expires_at_ms"])
        self.assertGreater(spoken[0]["expires_at_ms"], 0)
        kinds = [k for k, _ in self.engine.events]
        self.assertLess(kinds.index("miniapp_card"), kinds.index("speak"))

    def test_feedback_card_ttl_is_short_and_shared_by_both_paths(self):
        """若琪 and 小家 must use the same short display window.

        Both build the card through _show_card(), so one config value is
        enough. Mini-app query cards keep their own longer window.
        """
        import time
        import config as config_module
        from homeassistant.service import CARD_TTL_MS_DEFAULT
        self.assertEqual(CARD_TTL_MS_DEFAULT, 8000)
        self.assertEqual(config_module.DEFAULTS["homeassistant"]["card_ttl_ms"],
                         8000)
        self.assertEqual(config_module.DEFAULTS["miniapps"]["card_ttl_ms"],
                         30000)

        service = self.make(
            transport=FakeTransport((200, conversation_body())))
        before = int(time.time() * 1000)
        service.command("打开茶几灯", request_id="r-ttl-ruoqi")
        payload = json.loads(conversation_body("好的，已关闭茶几灯")
                             .decode("utf-8"))
        service.present_xiaojia(payload, "关闭茶几灯", "r-ttl-xiaojia")
        after = int(time.time() * 1000)
        cards = [p for k, p in self.engine.events if k == "miniapp_card"]
        self.assertEqual(len(cards), 2)
        self.assertEqual({c["ttl_ms"] for c in cards}, {8000})
        for card in cards:
            self.assertGreaterEqual(card["expires_at_ms"], before + 8000)
            self.assertLessEqual(card["expires_at_ms"], after + 8000)
        # The spoken line still binds to its own card, not the other one.
        speaks = [p for k, p in self.engine.events
                  if k == "speak" and p.get("card_id")]
        self.assertEqual(len(speaks), 1)
        self.assertEqual(speaks[0]["card_id"], cards[0]["card_id"])
        self.assertEqual(speaks[0]["expires_at_ms"],
                         cards[0]["expires_at_ms"])

    def test_speak_off_still_shows_the_card(self):
        """Silencing the announcement must not remove the feedback card."""
        transport = FakeTransport((200, conversation_body()))
        service = self.make(transport=transport, speak_results=False)
        service.command("打开茶几灯", request_id="r-silent")
        kinds = [k for k, _ in self.engine.events]
        self.assertIn("miniapp_card", kinds)
        self.assertNotIn("speak", kinds)

    def test_present_xiaojia_builds_a_card_from_the_backend_payload(self):
        """小家's own HA reply yields the same on-screen feedback."""
        transport = FakeTransport()
        service = self.make(transport=transport)
        payload = json.loads(conversation_body("好的，已打开茶几灯")
                             .decode("utf-8"))
        card_id, expires = service.present_xiaojia(
            payload, "打开茶几灯", "r-xj")
        self.assertTrue(card_id)
        self.assertGreater(expires, 0)
        cards = [p for k, p in self.engine.events if k == "miniapp_card"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["card_id"], card_id)
        self.assertEqual(cards[0]["expires_at_ms"], expires)
        self.assertEqual(cards[0]["card"]["layout"], "home_feedback")
        job = service.jobs()[0]
        self.assertEqual(job["status"], "reported_success")
        self.assertEqual(job["verification"], "not_checked")
        # No state polling and no second HA request: 小家 already ran the
        # turn in its backend, so the worker must not block for 1/2/4 s.
        self.assertEqual(len(transport.calls), 0)

    def test_present_xiaojia_labels_a_query_as_query(self):
        service = self.make(transport=FakeTransport())
        payload = json.loads(conversation_body("主卧温度是24度")
                             .decode("utf-8"))
        card_id, _ = service.present_xiaojia(
            payload, "主卧温湿度是多少", "r-query")
        self.assertTrue(card_id)
        self.assertEqual(service.jobs()[0]["kind"], "query")

    def test_gate_rebuild_on_alias_change(self):
        service = self.make(transport=FakeTransport())
        self.assertFalse(service.gate().matches("打开那个啥"))
        service.update_settings(gate_extras=["那个啥"])
        self.assertTrue(service.gate().matches("打开那个啥"))
        self.assertTrue(service.device_rule())

    def test_query_turn_is_labelled_query(self):
        transport = FakeTransport((200, conversation_body("主卧温度28.5度")),
                                  (200, conversation_body("已打开茶几灯")))
        service = self.make(transport=transport)
        result = service.command("主卧多少度", request_id="rq1")
        self.assertEqual(result["result"]["kind"], "query")
        result = service.command("打开茶几灯", request_id="rq2")
        self.assertEqual(result["result"]["kind"], "control")

    def test_query_card_shows_result_on_screen(self):
        transport = FakeTransport((200, conversation_body("主卧温度28.5度，湿度38%")))
        service = self.make(transport=transport)
        service.command("主卧多少度", request_id="rq3")
        cards = [p for k, p in self.engine.events if k == "miniapp_card"]
        self.assertEqual(len(cards), 1)
        card = cards[0]["card"]
        self.assertEqual(card["layout"], "home_feedback")
        self.assertEqual([(p["label"], p["value"], p["unit"]) for p in card["rows"]], [
            ("主卧 · 温度", "28.5", "°C"), ("主卧 · 湿度", "38", "%"),
        ])
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("28.5", text)          # the reading itself is on screen


class ParseTest(unittest.TestCase):
    def parse(self, body):
        data = json.loads(body)
        return HomeAssistantService._parse(data, request_id="rx")

    def test_speech_only_is_reported_success(self):
        result = self.parse(conversation_body("已打开"))
        self.assertEqual(result.status, "reported_success")
        self.assertEqual(result.message, "已打开")

    def test_success_and_failed_is_partial(self):
        result = self.parse(conversation_body(
            "一半好了", success={"light.a": {"name": "A"}},
            failed={"light.b": {}}))
        self.assertEqual(result.status, "partial")

    def test_failed_only_is_failed(self):
        result = self.parse(conversation_body("", failed={"light.b": {}}))
        self.assertEqual(result.status, "failed")

    def test_explicit_conversation_error_cannot_be_a_success(self):
        result = HomeAssistantService._parse({
            "response": {
                "response_type": "error",
                "speech": {"plain": {"speech": "没有找到茶几灯"}},
            },
        })
        self.assertEqual(result.status, "failed")

    def test_empty_response_is_unknown_not_success(self):
        result = self.parse(b'{"response": {"speech": {}, "data": {}}}')
        self.assertEqual(result.status, "unknown")
        self.assertEqual(CARD_STATUS["unknown"], "stale")

    def test_targets_carry_entity_names(self):
        result = self.parse(conversation_body(
            "好", success={"light.unknown_21": {"name": "茶几灯"}}))
        self.assertEqual(result.targets[0]["name"], "茶几灯")


class CardTest(unittest.TestCase):
    def test_light_switch_uses_action_art_only_for_an_affirmative_reply(self):
        examples = (
            ("打开茶几灯", "好的，已打开茶几灯", "ok", "已开启", "lightbulb-on"),
            ("把茶几灯关掉", "茶几灯已关闭", "ok", "已关闭", "lightbulb-off-outline"),
            ("把茶几灯关掉", "茶几灯已经关了", "ok", "已关闭", "lightbulb-off-outline"),
            ("关掉茶几灯", "已将茶几灯关闭", "ok", "已关闭", "lightbulb-off-outline"),
            ("打开茶几灯", "无法打开茶几灯", "stale", "未确认", "lightbulb-outline"),
            ("打开茶几灯", "茶几灯已关闭", "stale", "未确认", "lightbulb-outline"),
            ("打开茶几灯", "已准备打开茶几灯", "stale", "未确认", "lightbulb-outline"),
        )
        for sentence, message, status, headline, icon in examples:
            with self.subTest(sentence=sentence, message=message):
                card = result_card(HaResult(status="reported_success", message=message),
                                   sentence=sentence)
                self.assertEqual(card["layout"], "home_feedback")
                self.assertEqual(card["status"], status)
                self.assertEqual(card["rows"][0]["value"], headline)
                self.assertEqual(card["rows"][0]["icon"], f"ha-{icon}.png")

    def test_climate_preserves_text_if_a_single_reading_is_ambiguous(self):
        for message in ("主卧温度25°C至26°C", "主卧温度约25°C", "暂时没有温湿度读数"):
            with self.subTest(message=message):
                card = result_card(HaResult(kind="query", status="reported_success",
                                            message=message),
                                   sentence="主卧温湿度")
                self.assertEqual(card["rows"][0]["label"], message)
                self.assertNotIn("unit", card["rows"][0])

    def test_climate_handles_a_negative_temperature_and_never_invents_humidity(self):
        card = result_card(HaResult(kind="query", status="reported_success",
                                    message="阳台温度，-5.5摄氏度"),
                           sentence="阳台温湿度")
        self.assertEqual(len(card["rows"]), 1)
        self.assertEqual(card["rows"][0]["value"], "-5.5")
        self.assertEqual(card["rows"][0]["unit"], "°C")

    def test_brightness_keeps_standard_feedback(self):
        card = result_card(HaResult(status="reported_success", message="亮度已调整"),
                           sentence="把茶几灯调亮一点")
        self.assertEqual(card["rows"][0]["icon"], "ha-home-outline.png")

    def test_temperature_setpoint_uses_air_conditioner_art(self):
        card = result_card(HaResult(kind="query", status="reported_success",
                                    message="已将空调设为25°C"),
                           sentence="把空调温度调到25度")
        self.assertEqual(card["rows"][0]["icon"], "ha-air-conditioner.png")
        self.assertIn("设定温度", card["rows"][0]["label"])

    def test_specialized_feedback_for_supported_conversation_replies(self):
        samples = (
            ("关闭加湿器", "已关闭加湿器", "control", "power-plug-off-outline", "已关闭"),
            ("扫地机器人还剩多少电量", "扫地机器人电量72%", "query", "battery-70", "72"),
            ("主卧湿度是多少", "主卧湿度54%", "query", "water-percent", "54"),
            ("台灯插座开着吗", "台灯插座当前关闭", "query", "toggle-switch-off-outline", "当前关闭"),
            ("给入户门锁上锁", "入户门已上锁", "control", "lock", "已上锁"),
            ("解锁入户门", "入户门已解锁", "control", "lock-open-variant-outline", "已解锁"),
            ("开启观影模式", "已启动观影模式", "control", "movie-open-outline", "已启动"),
            ("退出观影模式", "已退出观影模式", "control", "movie-off-outline", "已退出"),
            ("把灯调成黄色", "灯已调成黄色", "control", "palette-outline", "暖色"),
            ("把灯调成蓝色", "灯已调成蓝色", "control", "palette-outline", "冷色"),
            ("把灯的亮度调到65%", "亮度已设为65%", "control", "brightness-6", "65"),
            ("打开Apple TV", "已打开 Apple TV", "control", "apple-tv", "已开机"),
            ("关闭苹果电视", "Apple TV 已进入待机", "control", "apple-tv", "已进入待机"),
            ("开启主卧空调", "已开启主卧空调", "control", "air-conditioner", "已开启"),
            ("关掉主卧空调", "已关闭主卧空调", "control", "power-off", "已关闭"),
            ("拉开客厅窗帘", "已拉开客厅窗帘", "control", "curtains", "已打开"),
            ("拉上客厅窗帘", "客厅窗帘已闭合", "control", "curtains-closed", "已闭合"),
        )
        gate = Gate([])
        for sentence, reply, kind, icon, value in samples:
            with self.subTest(sentence=sentence):
                self.assertTrue(gate.route(sentence)[0])
                card = result_card(HaResult(kind=kind, status="reported_success", message=reply),
                                   sentence=sentence)
                self.assertEqual(card["rows"][0]["icon"], f"ha-{icon}.png")
                self.assertEqual(card["rows"][0]["value"], value)

    def test_combined_lighting_feedback_uses_separate_pages_in_one_card(self):
        card = result_card(HaResult(status="reported_success",
                                    message="灯已调成黄色，亮度已调到70%"),
                           sentence="把灯调成黄色，亮度设成70%")
        self.assertEqual([page["value"] for page in card["rows"]], ["暖色", "70"])

    def test_unknown_card_is_stale_with_notice(self):
        card = result_card(HaResult(status="unknown",
                                    message="网络异常，没有收到执行结果"))
        self.assertEqual(card["status"], "stale")
        self.assertIn("没有收到执行结果", card["notice"])
        self.assertIn("未确认", json.dumps(card, ensure_ascii=False))

    def test_success_card_is_ok(self):
        card = result_card(HaResult(status="reported_success",
                                    message="已打开茶几灯"))
        self.assertEqual(card["status"], "ok")

    def test_speech_text_is_capped(self):
        long = HaResult(status="reported_success", message="好" * 200)
        self.assertLessEqual(len(speech_text(long)), 61)

    def test_failure_fallback_speech(self):
        self.assertIn("没有执行成功",
                      speech_text(HaResult(status="failed", message="")))

    def test_mismatch_never_shows_green(self):
        card = result_card(HaResult(status="reported_success",
                                    message="已打开茶几灯",
                                    verification="state_mismatch"),
                           sentence="打开茶几灯")
        self.assertEqual(card["status"], "stale")

    def test_confirmed_light_switch_may_show_green(self):
        card = result_card(HaResult(status="reported_success",
                                    message="已打开茶几灯",
                                    verification="state_confirmed"),
                           sentence="打开茶几灯")
        self.assertEqual(card["status"], "ok")
        self.assertEqual(card["rows"][0]["value"], "已开启")

    def test_mismatch_speech_says_not_confirmed(self):
        text = speech_text(HaResult(status="reported_success",
                                    message="已打开茶几灯",
                                    verification="state_mismatch"))
        self.assertIn("没有确认到设备变化", text)


class VerifyTest(unittest.TestCase):
    """Batch 3: control turns are checked against real entity state."""

    def setUp(self):
        import homeassistant.service as ha_service
        self._module = ha_service
        self._old_checkpoints = ha_service.VERIFY_CHECKPOINTS
        ha_service.VERIFY_CHECKPOINTS = (0.0,)   # never sleep in tests
        self.engine = FakeEngine()

    def tearDown(self):
        self._module.VERIFY_CHECKPOINTS = self._old_checkpoints

    def make(self, transport):
        return HomeAssistantService(
            config={"enabled": True, "base_url": "http://ha.test:8123",
                    "token": "t", "speak_results": False},
            transport=transport, engine=self.engine)

    def test_state_change_confirms_the_command(self):
        body = conversation_body("已打开茶几灯",
                                 success={"light.unknown_21": {"name": "茶几灯"}})
        state_body = json.dumps({
            "entity_id": "light.unknown_21", "state": "on",
            "last_changed": "2099-01-01T00:00:00+00:00",
        }).encode("utf-8")
        transport = FakeTransport((200, body), (200, state_body))
        service = self.make(transport)
        result = service.command("打开茶几灯", request_id="v1")
        self.assertEqual(result["result"]["verification"], "state_confirmed")

    def test_no_state_change_is_a_mismatch(self):
        body = conversation_body("已打开茶几灯",
                                 success={"light.unknown_21": {"name": "茶几灯"}})
        state_body = json.dumps({
            "entity_id": "light.unknown_21", "state": "off",
            "last_changed": "2000-01-01T00:00:00+00:00",
        }).encode("utf-8")
        transport = FakeTransport((200, body), (200, state_body),
                                  (200, state_body), (200, state_body))
        service = self.make(transport)
        result = service.command("打开茶几灯", request_id="v2")
        self.assertEqual(result["result"]["verification"], "state_mismatch")
        self.assertEqual(result["result"]["card_status"], "stale")

    def test_query_turns_are_not_verified(self):
        body = conversation_body("主卧温度28.5度")
        transport = FakeTransport((200, body))
        service = self.make(transport)
        result = service.command("主卧多少度", request_id="v3")
        self.assertEqual(result["result"]["verification"], "not_checked")
        self.assertEqual(len(transport.calls), 1)   # no state read-back

    def test_control_without_targets_is_not_verified(self):
        body = conversation_body("好的")
        transport = FakeTransport((200, body))
        service = self.make(transport)
        result = service.command("打开茶几灯", request_id="v4")
        self.assertEqual(result["result"]["verification"], "not_checked")
        self.assertEqual(len(transport.calls), 1)

    def test_state_read_failure_stays_not_checked(self):
        body = conversation_body("已打开茶几灯",
                                 success={"light.unknown_21": {"name": "茶几灯"}})
        # GET retries once (client._get), so both attempts must fail.
        transport = FakeTransport((200, body), HaNetworkError("down"),
                                  HaNetworkError("down"))
        service = self.make(transport)
        result = service.command("打开茶几灯", request_id="v5")
        self.assertEqual(result["result"]["verification"], "not_checked")


class ClarifyTest(unittest.TestCase):
    """Batch 4: a failed/asking turn arms a short follow-up window."""

    def setUp(self):
        self.engine = FakeEngine()

    def make(self, transport):
        return HomeAssistantService(
            config={"enabled": True, "base_url": "http://ha.test:8123",
                    "token": "t", "speak_results": False},
            transport=transport, engine=self.engine)

    @staticmethod
    def body(speech, conversation_id="c-1"):
        return conversation_body(speech, conversation_id=conversation_id)

    def test_reply_continues_the_same_conversation(self):
        # HA asks "哪个灯？" -> armed; "茶几灯" continues the session.
        transport = FakeTransport(
            (200, self.body("你想打开哪个灯？")),
            (200, self.body("已打开茶几灯", conversation_id="c-2")))
        service = self.make(transport)
        first = service.command("打开灯", request_id="k1")
        self.assertIn("哪个", first["result"]["message"])
        self.assertTrue(service._pending)
        taken = service.handle_text("茶几灯", request_id="k2")
        self.assertIsNotNone(taken)
        self.assertTrue(taken["continued"])
        service._queue.join()
        # The second call carried the first turn's conversation_id.
        second = transport.calls[1]["body"]
        self.assertIn('"conversation_id": "c-1"', second.decode("utf-8"))

    def test_retry_word_continues_after_a_failure(self):
        transport = FakeTransport(
            (200, self.body("没有找到茶几灯")),
            (200, self.body("已打开茶几灯")))
        service = self.make(transport)
        service.command("打开茶几灯", request_id="k3")
        self.assertTrue(service._pending)
        taken = service.handle_text("再试一次", request_id="k4")
        self.assertIsNotNone(taken)
        service._queue.join()
        self.assertEqual(len(transport.calls), 2)

    def test_pending_expires_and_then_the_gate_stays_shut(self):
        transport = FakeTransport((200, self.body("没有找到茶几灯")))
        service = self.make(transport)
        service.command("打开茶几灯", request_id="k5")
        self.assertTrue(service._pending)
        service._pending["expires_at"] = 0   # force expiry
        self.assertIsNone(service.handle_text("再试一次", request_id="k6"))
        self.assertEqual(len(transport.calls), 1)

    def test_success_does_not_arm_a_pending(self):
        transport = FakeTransport((200, self.body("已打开茶几灯")))
        service = self.make(transport)
        service.command("打开茶几灯", request_id="k7")
        self.assertIsNone(service._pending)
        self.assertIsNone(service.handle_text("再试一次", request_id="k8"))

    def test_unrelated_short_line_is_not_swallowed(self):
        transport = FakeTransport((200, self.body("你想打开哪个灯？")))
        service = self.make(transport)
        service.command("打开灯", request_id="k9")
        self.assertTrue(service._pending)
        # No device/room word and not an answer word -> not ours.
        self.assertIsNone(service.handle_text("今天天气怎么样", request_id="k10"))
        self.assertEqual(len(transport.calls), 1)

    def test_answer_word_without_pending_is_not_swallowed(self):
        transport = FakeTransport()
        service = self.make(transport)
        self.assertIsNone(service.handle_text("好的", request_id="k11"))
        self.assertEqual(len(transport.calls), 0)


if __name__ == "__main__":
    unittest.main()
