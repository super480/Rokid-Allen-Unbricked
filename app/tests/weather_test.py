"""The rebuilt weather skill (D047): phrases, contract, voice entry.

The native weather app is kept and fed by a local API replacement, so
the tests pin the pieces the app depends on: which turns open the card,
the exact ResponseBean shape it parses (field names and types come from
its bean classes), and the wiring -- the engine takes a weather turn
before any assistant, while the factory rule no longer claims it.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


class _Resp:
    """A minimal context-manager response for patched urlopen."""

    def __init__(self, blob: bytes):
        self.blob = blob

    def read(self):
        return self.blob

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def json_resp(payload: dict) -> "_Resp":
    import json as _json
    return _Resp(_json.dumps(payload).encode())

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))
sys.path.insert(0, str(ROOT.parent))  # scripts.weather_device_setup

from weather import (OpenMeteo, WttrIn, WeatherApiServer, WeatherService,  # noqa: E402
                     app_error, app_result, description_to_app,
                     is_weather_query, API_HOST, BUILTIN_KEYWORDS,
                     DEFAULT_API_URL, DEFAULT_WTTR_URL,
                     FACTORY_WEATHER_WORDS, NLP_DOMAIN)
import config as config_module  # noqa: E402
import engine  # noqa: E402
import intent  # noqa: E402

HOST_SOURCE = (ROOT / "host/engine.py").read_text(encoding="utf-8")


class PhraseTest(unittest.TestCase):
    def test_weather_queries_match(self):
        for text in ("今天天气怎么样", "若琪查一下天气", "外面气温多少度",
                     "明天会下雨吗", "下雪了吗", "热不热啊", "降温了没"):
            self.assertTrue(is_weather_query(text), text)

    def test_other_sentences_do_not_match(self):
        for text in ("放一首晴天", "报时", "提醒我开会", "音量大一点",
                     "今天几号", "小赫讲个故事"):
            self.assertFalse(is_weather_query(text), text)

    def test_disabled_service_passes_the_turn_on(self):
        service = WeatherService(StubEngine(), config={"enabled": False})
        self.assertIsNone(service.handle_text("今天天气怎么样", "r1"))


class ContractTest(unittest.TestCase):
    def test_result_shape_matches_the_app_beans(self):
        result = app_result("上海", icon=26, condition="阴",
                             temp=13.4, low=8.6, high=16.49)
        self.assertEqual(result["code"], "OK")
        body = result["result"]
        self.assertEqual(body["intent"], "query_weather")
        self.assertEqual(body["locCity"], "上海")
        self.assertTrue(body["tts"].endswith("。"))
        self.assertEqual(body["uiFlag"], "")  # iconOneValueWidget
        info = body["weatherUIInfos"][0]
        self.assertEqual(info["time"], "今天")
        self.assertEqual(info["icon"], 26)  # the Yahoo code, as-is
        self.assertEqual(info["valueMain"], 13)  # int, as getNumIcons wants
        self.assertEqual(info["valueleftUp"], 16)
        self.assertEqual(info["valueRightDown"], 9)
        self.assertEqual(info["dataTemp"], 1)  # plain temperature
        self.assertEqual(info["errorInfo"], 0)
        self.assertFalse(info["isOpposite"])
        # The app speaks whatever the tts field carries, as it always did.
        self.assertIn("上海今天阴，13度", body["tts"])

    def test_rainy_day_recommends_an_umbrella(self):
        result = app_result("上海", icon=12, condition="有雨",
                            temp=12.0, low=9.0, high=14.0)
        self.assertIn("带伞", result["result"]["tts"])

    def test_negative_temperature_comes_through(self):
        result = app_result("哈尔滨", icon=14, condition="有小雪",
                            temp=-5.4, low=-11.0, high=-1.0)
        self.assertIn("-5度", result["result"]["tts"])

    def test_error_shape_makes_the_app_speak_it(self):
        error = app_error("天气数据暂时拿不到")
        self.assertEqual(error["code"], "ERROR")
        self.assertTrue(error["msg"])
        self.assertNotEqual(error["msgtype"], "0")  # not the sys type

    def test_every_app_icon_code_is_one_the_app_draws(self):
        # The codes WeatherIconUtils.getWeatherIcon understands (it has
        # no default branch that renders a real condition).
        known = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
                 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30,
                 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44,
                 45, 46, 47, 1010}
        from weather import WMO_TO_APP, WMO_UNKNOWN_APP_ICON, DESC_RULES
        for wmo, (icon, _) in WMO_TO_APP.items():
            self.assertIn(icon, known, "icon for wmo %d" % wmo)
        for icon, _, _ in DESC_RULES:
            self.assertIn(icon, known)
        self.assertIn(WMO_UNKNOWN_APP_ICON, known)


class WttrDescriptionTest(unittest.TestCase):
    def test_common_english_conditions_map(self):
        cases = {
            "Sunny": (32, "晴"),
            "Partly cloudy": (30, "多云转晴"),
            "Overcast": (26, "阴"),
            "Cloudy": (26, "多云"),
            "Fog, mist": (20, "有雾"),
            "Light rain": (11, "有小雨"),
            "Moderate rain": (12, "有雨"),
            "Heavy rain": (40, "有大雨"),
            "Patchy light drizzle": (9, "有毛毛雨"),
            "Thundery showers": (37, "有雷阵雨"),
            "Light snow": (14, "有小雪"),
            "Blizzard": (42, "有暴雪"),
        }
        for description, expected in cases.items():
            self.assertEqual(description_to_app(description), expected,
                             description)

    def test_worst_condition_wins_in_a_composite_description(self):
        # "Patchy freezing drizzle nearby" must read as freezing, not as
        # the drizzle it also contains -- the rules run worst-first.
        icon, condition = description_to_app("Patchy freezing drizzle nearby")
        self.assertEqual(icon, 8)
        self.assertIn("冻", condition)

    def test_unknown_description_is_the_placeholder(self):
        self.assertEqual(description_to_app("something odd"), (44, ""))

    def test_wttr_reader_parses_the_j1_shape(self):
        payload = {"current_condition": [{"temp_C": "17",
                                          "weatherDesc": [{"value": "Overcast"}]}],
                   "weather": [{"mintempC": "14", "maxtempC": "18"}]}
        with patch("weather.urlopen", return_value=json_resp(payload)):
            reader = WttrIn()
            icon, condition, temp, low, high = reader.read("31.23,121.47")
        self.assertEqual((icon, condition), (26, "阴"))
        self.assertEqual((temp, low, high), (17.0, 14.0, 18.0))


class AddressTest(unittest.TestCase):
    def test_device_location_json_is_parsed(self):
        address = json.dumps({"city": "深圳", "latitude": "22.54",
                              "longitude": "114.06"})
        from weather import _coordinates_from_address
        self.assertEqual(_coordinates_from_address(address),
                         ("深圳", "22.54", "114.06"))

    def test_broken_address_falls_back_to_blank(self):
        from weather import _coordinates_from_address
        self.assertEqual(_coordinates_from_address("not json"), ("", "", ""))
        self.assertEqual(_coordinates_from_address(""), ("", "", ""))


class ServerLogicTest(unittest.TestCase):
    def test_the_console_city_outranks_the_device_location(self):
        # D049: a city saved from the console wins -- the user asked for
        # that place; the device only reports where its last fix was.
        server = WeatherApiServer(StubEngine(), {"city": "上海",
                                                 "latitude": "31.2",
                                                 "longitude": "121.4"})
        address = json.dumps({"city": "北京", "latitude": "39.9",
                              "longitude": "116.4"})
        with patch.object(OpenMeteo, "read",
                          return_value=(32, "晴", 25.0, 18.0, 30.0)) as read:
            result = server.result_for(
                json.dumps({"domain": NLP_DOMAIN,
                            "intent": "query_weather"}), address)
        self.assertEqual(result["code"], "OK")
        self.assertEqual(result["result"]["locCity"], "上海")
        # the configured coordinates were used, not the device's
        self.assertEqual(read.call_args[0][:2], ("31.2", "121.4"))

    def test_without_a_console_city_the_device_location_wins(self):
        server = WeatherApiServer(StubEngine(), {"latitude": "31.2",
                                                 "longitude": "121.4"})
        address = json.dumps({"city": "北京", "latitude": "39.9",
                              "longitude": "116.4"})
        with patch.object(OpenMeteo, "read",
                          return_value=(32, "晴", 8.0, 2.0, 12.0)) as read:
            result = server.result_for("{}", address)
        self.assertEqual(result["result"]["locCity"], "北京")
        self.assertEqual(read.call_args[0][:2], ("39.9", "116.4"))

    def test_config_city_when_device_location_is_empty(self):
        server = WeatherApiServer(StubEngine(), {"city": "上海",
                                                 "latitude": "31.2",
                                                 "longitude": "121.4"})
        with patch.object(OpenMeteo, "read",
                          return_value=(30, "多云转晴", 20.0, 12.0, 24.0)):
            result = server.result_for("{}", "")
        self.assertEqual(result["result"]["locCity"], "上海")

    def test_fallback_source_answers_when_the_primary_fails(self):
        server = WeatherApiServer(StubEngine(), {"city": "上海",
                                                 "latitude": "31.2",
                                                 "longitude": "121.4"})
        with patch.object(OpenMeteo, "read", side_effect=OSError("down")), \
             patch.object(WttrIn, "read",
                          return_value=(26, "阴", 17.0, 14.0, 18.0)) as wttr:
            result = server.result_for("{}", "")
        self.assertEqual(result["code"], "OK")
        self.assertIn("阴", result["result"]["tts"])
        self.assertTrue(wttr.called)

    def test_city_without_coordinates_geocodes_then_falls_back(self):
        server = WeatherApiServer(StubEngine(), {"city": "杭州"})
        with patch.object(OpenMeteo, "geocode", side_effect=OSError("down")), \
             patch.object(WttrIn, "read",
                          return_value=(32, "晴", 21.0, 15.0, 26.0)) as wttr:
            result = server.result_for("{}", "")
        self.assertEqual(result["code"], "OK")
        self.assertEqual(result["result"]["locCity"], "杭州")
        # the fallback is queried by city name
        self.assertEqual(wttr.call_args[0][0], "杭州")

    def test_no_city_and_no_coordinates_is_a_spoken_error(self):
        server = WeatherApiServer(StubEngine(), {"city": ""})
        result = server.result_for("{}", "")
        self.assertEqual(result["code"], "ERROR")

    def test_all_sources_down_is_a_spoken_error_not_a_crash(self):
        server = WeatherApiServer(StubEngine(), {"city": "上海",
                                                 "latitude": "31.2",
                                                 "longitude": "121.4"})
        with patch.object(OpenMeteo, "read", side_effect=OSError("down")), \
             patch.object(WttrIn, "read", side_effect=OSError("down")):
            result = server.result_for("{}", "")
        self.assertEqual(result["code"], "ERROR")
        self.assertIn("拿不到", result["msg"])

    def test_the_apps_form_key_nlp_is_parsed_for_the_voice(self):
        # WeatherModelImpl puts the NLP payload under a constant whose
        # value is "nlp" (seen in the device's own request log).
        source = (ROOT / "host/weather.py").read_text(encoding="utf-8")
        self.assertIn('form.get("nlp") or form.get("message")', source)

    def test_the_factory_centroid_coordinates_are_ignored(self):
        # 35.86166 / 104.195397 is the "centre of China" fallback the
        # factory reports when the speaker has no real position; the
        # city has to carry the query instead.
        server = WeatherApiServer(StubEngine(), {"city": "沈阳市"})
        address = json.dumps(
            {"city": "沈阳市", "latitude": "35.86166",
             "longitude": "104.195397", "locationFrom": "MANUAL"})
        with patch.object(OpenMeteo, "geocode",
                          return_value=("41.8", "123.4")) as geo, \
             patch.object(OpenMeteo, "read",
                          return_value=(26, "阴", 2.0, -3.0, 6.0)):
            result = server.result_for("{}", address)
        self.assertEqual(result["code"], "OK")
        self.assertEqual(result["result"]["locCity"], "沈阳市")
        self.assertTrue(geo.called)  # the bogus point was not used

    def test_real_device_coordinates_are_ignored_for_a_console_city(self):
        # D049 flipped the precedence: with 北京市 saved in the console,
        # the device's 大连 fix (and its coordinates) must not leak into
        # the answer any more.
        server = WeatherApiServer(StubEngine(), {"city": "北京市"})
        address = json.dumps({"city": "大连市", "latitude": "38.91",
                              "longitude": "121.60"})
        with patch.object(OpenMeteo, "geocode",
                          return_value=("39.9", "116.4")) as geo, \
             patch.object(OpenMeteo, "read",
                          return_value=(32, "晴", 8.0, 2.0, 12.0)) as read:
            result = server.result_for("{}", address)
        self.assertEqual(result["result"]["locCity"], "北京市")
        # geocoded from the console city, not the device's point
        self.assertEqual(geo.call_args[0][0], "北京市")
        self.assertEqual(read.call_args[0][:2], ("39.9", "116.4"))

    def test_a_saved_source_url_rebuilds_the_readers_live(self):
        # D049: the console saves a source URL at any time; the API
        # server reads it live and rebuilds its readers with the very
        # next query -- no restart, no rebuild of the host.
        settings = {"city": "杭州"}
        server = WeatherApiServer(StubEngine(), settings)
        self.assertEqual(server.meteo.url, DEFAULT_API_URL)
        self.assertEqual(server.wttr.url, DEFAULT_WTTR_URL)
        settings["api_url"] = "https://my-mirror.example/v1/forecast"
        settings["wttr_url"] = "https://wttr.example"
        server._sync_readers()
        self.assertEqual(server.meteo.url, "https://my-mirror.example/v1/forecast")
        self.assertEqual(server.wttr.url, "https://wttr.example")
        # empty falls back to the built-in sources again
        settings["api_url"] = ""
        settings["wttr_url"] = ""
        server._sync_readers()
        self.assertEqual(server.meteo.url, DEFAULT_API_URL)
        self.assertEqual(server.wttr.url, DEFAULT_WTTR_URL)

    def test_cached_answer_is_reused_within_ttl(self):
        server = WeatherApiServer(StubEngine(), {"city": "上海",
                                                 "latitude": "31.2",
                                                 "longitude": "121.4",
                                                 "ttl_seconds": 300})
        with patch.object(OpenMeteo, "read",
                          return_value=(32, "晴", 20.0, 10.0, 25.0)) as read:
            server.result_for("{}", "")
            server.result_for("{}", "")
        self.assertEqual(read.call_count, 1)


class VoiceEntryTest(unittest.TestCase):
    def make_service(self):
        service = WeatherService(StubEngine(), config={"enabled": True})
        return service

    def test_weather_turn_launches_the_native_app(self):
        service = self.make_service()
        with patch.object(WeatherService, "_launch") as launch:
            result = service.handle_text("今天天气怎么样", "r1")
        self.assertEqual(result["intent"], "weather")
        self.assertTrue(result["launched"])
        nlp = json.loads(launch.call_args[0][0])
        self.assertEqual(nlp["domain"], NLP_DOMAIN)
        self.assertEqual(nlp["intent"], "query_weather")
        self.assertEqual(nlp["voice"], "今天天气怎么样")

    def test_the_launch_command_targets_the_exported_activity(self):
        source = (ROOT / "host/weather.py").read_text(encoding="utf-8")
        self.assertIn("com.rokid.weather1/.ui.view.MainActivity", source)
        # am start ... --es nlp <json> -- the extra the app checks.
        self.assertIn('"am start -n %s --es %s %s"', source)

    def test_repeat_within_window_is_not_launched_twice(self):
        service = self.make_service()
        with patch.object(WeatherService, "_launch") as launch:
            first = service.handle_text("查一下天气", "r1")
            second = service.handle_text("查一下天气", "r2")
        self.assertTrue(first["launched"])
        self.assertFalse(second["launched"])
        self.assertTrue(second.get("duplicate"))
        # one spawned launch thread, not two
        self.assertEqual(launch.call_count, 1)

    def test_non_weather_turn_is_untouched(self):
        service = self.make_service()
        self.assertIsNone(service.handle_text("放一首歌", "r1"))


class WiringTest(unittest.TestCase):
    """The turn must reach the service before any assistant answers."""

    def test_engine_intercepts_weather_before_routing(self):
        self.assertIn("self.weather.handle_text(text, request_id)",
                      HOST_SOURCE)
        # and it sits after the mini-apps, before parse_intent. The 小家
        # assistant shortcut calls parse_intent earlier in the file, so
        # compare against the main-flow call (the last one).
        mini = HOST_SOURCE.index("self.miniapps.handle_text")
        weather = HOST_SOURCE.index("self.weather.handle_text")
        parse = HOST_SOURCE.rfind("parse_intent(text)")
        self.assertLess(mini, weather)
        self.assertLess(weather, parse)

    def test_engine_construction_wires_the_service(self):
        self.assertIn("WeatherService(self, config=config.get(\"weather\")",
                      HOST_SOURCE)

    def test_factory_rule_no_longer_claims_weather(self):
        for word in ("天气", "气温", "下雨"):
            self.assertNotIn(word, config_module.DEFAULTS["direct"]["factory_owns"],
                             word)

    def test_weather_defaults_are_off_by_default(self):
        defaults = config_module.DEFAULTS["weather"]
        self.assertFalse(defaults["enabled"])
        self.assertEqual(defaults["port"], 8799)
        self.assertIn("ttl_seconds", defaults)

    def test_server_starts_the_api_when_enabled(self):
        source = (ROOT / "host/server.py").read_text(encoding="utf-8")
        self.assertIn("WeatherApiServer", source)


class StubEngine:
    """log_transcript / config / persistence only -- no real engine."""

    def __init__(self):
        self.config = {"device": "test:5555"}
        self.transcript = []
        self.saves = 0
        self.weather_api = None

    def log_transcript(self, *args):
        self.transcript.append(args)

    def _save_config(self):
        self.saves += 1


class FakeApi:
    """Records start/stop so the override switch test needs no port."""

    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1
        return True

    def stop(self):
        self.stopped += 1


class ConsoleCardTest(unittest.TestCase):
    """The 天气查询 card in 设置 · 小应用 talks to four endpoints."""

    def make_service(self, enabled=False):
        return WeatherService(StubEngine(), config={"enabled": enabled})

    def test_status_reports_switch_city_and_phrases(self):
        service = self.make_service(enabled=True)
        service.settings["city"] = "杭州"
        info = service.status()
        self.assertTrue(info["enabled"])
        self.assertEqual(info["city"], "杭州")
        self.assertEqual(info["voice_phrases"], [])
        self.assertEqual(info["builtin_phrases"], BUILTIN_KEYWORDS)
        self.assertEqual(info["builtin_mode"], "keywords")

    def test_override_flip_persists_and_toggles_the_api(self):
        service = self.make_service(enabled=False)
        api = FakeApi()
        service.engine.weather_api = api
        result = service.set_override(enabled=True)
        self.assertTrue(result["enabled"])
        self.assertEqual(api.started, 1)
        self.assertEqual(api.stopped, 0)
        self.assertEqual(service.engine.saves, 1)
        result = service.set_override(enabled=False)
        self.assertFalse(result["enabled"])
        self.assertEqual(api.stopped, 1)
        self.assertEqual(service.engine.saves, 2)

    def test_query_now_requires_the_switch(self):
        service = self.make_service(enabled=False)
        result = service.query_now()
        self.assertFalse(result["ok"])
        self.assertFalse(result["launched"])
        self.assertIn("error", result)

    def test_query_now_opens_the_card_once(self):
        service = self.make_service(enabled=True)
        with patch.object(WeatherService, "_launch") as launch:
            first = service.query_now()
            second = service.query_now()
        self.assertTrue(first["launched"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(launch.call_count, 1)
        nlp = json.loads(launch.call_args[0][0])
        self.assertEqual(nlp["domain"], NLP_DOMAIN)

    def test_custom_phrase_matches_despite_wake_word_and_punctuation(self):
        # The D044 normalisation: the phrase is pasted with 「若琪，」 and
        # punctuation, the recogniser delivers it clean -- one key. (The
        # console splits on commas, so the wake word prefix drops off the
        # saved phrase; the key is what matters.)
        service = self.make_service(enabled=True)
        service.set_voice_phrases("若琪，外面啥情况！")
        self.assertEqual(len(service.voice_phrases), 1)
        with patch.object(WeatherService, "_launch") as launch:
            # No builtin keyword in the sentence: only the custom key hits.
            self.assertFalse(is_weather_query("外面啥情况"))
            result = service.handle_text("外面啥情况", "r1")
        self.assertTrue(result["launched"])
        self.assertEqual(launch.call_count, 1)

    def test_custom_phrase_limits_match_the_miniapps(self):
        service = self.make_service(enabled=True)
        service.set_voice_phrases(["、".join(["问天"] * 30)])  # >40 chars
        self.assertEqual(service.voice_phrases, [])
        service.set_voice_phrases(["问天%d" % i for i in range(20)])
        self.assertEqual(len(service.voice_phrases), 12)
        result = service.set_voice_phrases([])
        self.assertEqual(result["phrases"], [])
        self.assertEqual(service.voice_phrases, [])

    def test_set_settings_city_and_source_urls_persist(self):
        service = self.make_service(enabled=True)
        result = service.set_settings(city="沈阳市",
                                      api_url="https://mirror.example/v1",
                                      wttr_url="https://wttr.example")
        self.assertEqual(result["city"], "沈阳市")
        self.assertEqual(result["api_url"], "https://mirror.example/v1")
        self.assertEqual(result["wttr_url"], "https://wttr.example")
        self.assertEqual(service.settings["city"], "沈阳市")
        self.assertEqual(service.engine.saves, 1)
        # empty clears each field (city follows the device, URLs default)
        result = service.set_settings(city="", api_url="", wttr_url="")
        self.assertEqual(result["city"], "")
        self.assertEqual(result["api_url"], "")
        self.assertEqual(result["wttr_url"], "")

    def test_set_settings_rejects_a_bare_url(self):
        service = self.make_service(enabled=True)
        with self.assertRaises(ValueError):
            service.set_settings(api_url="mirror.example/v1")

    def test_set_settings_rejects_an_overlong_city(self):
        service = self.make_service(enabled=True)
        with self.assertRaises(ValueError):
            service.set_settings(city="很" * 41)

    def test_partial_set_settings_leaves_other_fields_alone(self):
        service = self.make_service(enabled=True)
        service.set_settings(api_url="https://mirror.example/v1")
        result = service.set_settings(city="北京市")
        self.assertEqual(result["api_url"], "https://mirror.example/v1")
        self.assertEqual(result["city"], "北京市")

    def test_public_edition_does_not_change_system_network(self):
        self.assertFalse(self.make_service(enabled=True).fix_wiring()["ok"])



class OverrideSwitchTest(unittest.TestCase):
    """The 覆盖原厂天气查询 switch and the factory rule it drives."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config_module.create_config(
            Path(self.tmp.name) / "config.json")
        self.config["_root"] = self.tmp.name
        self.config["_path"] = str(Path(self.tmp.name) / "config.json")
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.engine = engine.Engine(
            self.config, state_file=str(Path(self.tmp.name) / "s.json"))

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def test_off_regains_the_factory_weather_words(self):
        # Default is off: the stored rule stays clean, but the runtime
        # pattern answers weather turns again -- the pre-D047 behaviour.
        self.assertFalse(self.engine.weather.enabled)
        self.assertNotIn("天气",
                        config_module.DEFAULTS["direct"]["factory_owns"])
        pattern = self.engine.factory_pattern()
        self.assertIn(FACTORY_WEATHER_WORDS, pattern)
        for said in ("今天天气怎么样", "外面气温多少度", "明天会下雨吗"):
            self.assertTrue(intent.factory_owns(said, pattern), said)
        self.assertTrue(intent.factory_owns("现在几点了", pattern))

    def test_on_returns_the_stored_rule_as_is(self):
        self.engine.weather.set_override(enabled=True)
        pattern = self.engine.factory_pattern()
        self.assertEqual(pattern,
                         config_module.DEFAULTS["direct"]["factory_owns"])
        self.assertFalse(intent.factory_owns("今天天气怎么样", pattern))

    def test_the_device_rule_follows_the_switch(self):
        status = self.engine.status()
        self.assertEqual(status["direct"]["factory_owns"],
                         self.engine.factory_pattern())
        self.engine.weather.set_override(enabled=True)
        status = self.engine.status()
        self.assertNotIn(FACTORY_WEATHER_WORDS,
                        status["direct"]["factory_owns"])

    def test_the_flip_reaches_the_config_file(self):
        self.engine.weather.set_override(enabled=True)
        saved = json.loads(
            (Path(self.tmp.name) / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["weather"]["enabled"])


if __name__ == "__main__":
    unittest.main()
