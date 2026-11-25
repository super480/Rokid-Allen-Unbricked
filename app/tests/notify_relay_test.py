"""P2-12 Mac 通知播报 Host 侧纯逻辑单测（静音，无设备/无 AX/无网络依赖）。"""
from __future__ import annotations

import json
import os
import plistlib
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = ROOT / "host"
for entry in (str(ROOT), str(HOST)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from notify_relay import (  # noqa: E402
    DEFAULT_SETTINGS,
    NotifyRelay,
    NotifyService,
    compose,
    normalize_event,
    resolve_source,
)


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class NotifyRelayTest(unittest.TestCase):
    def _make_relay(self, **kwargs):
        clock = kwargs.pop("clock", FakeClock())
        locked = kwargs.pop("locked", False)
        settings = {
            "enabled": True,
            "sources": {
                "com.apple.reminders": "title",
                "com.apple.iCal": "title_body",
                "com.anthropic.claudefordesktop": "source",
                "com.openai.codex": "card",
            },
        }
        settings.update(kwargs.pop("settings", {}))
        relay = NotifyRelay(
            settings=settings,
            clock=clock,
            is_locked=lambda: locked,
            **kwargs,
        )
        return relay, clock

    def test_defaults_all_off(self):
        relay = NotifyRelay(is_locked=lambda: False)
        self.assertFalse(DEFAULT_SETTINGS["enabled"])
        d = relay.submit({"source_id": "com.apple.reminders", "title": "取快递"})
        self.assertEqual(d.action, "dropped")
        self.assertEqual(d.reason, "disabled")

    def test_source_resolution_and_ambiguity_silent(self):
        self.assertEqual(resolve_source(source_name="提醒事项"), "com.apple.reminders")
        self.assertEqual(resolve_source(source_name="Calendar"), "com.apple.iCal")
        self.assertEqual(resolve_source(source_id="com.example.foo", source_name="Foo"), "com.example.foo")
        self.assertIsNone(resolve_source(source_name="未知应用"))

        relay, _ = self._make_relay()
        d = relay.submit({"source_name": "未知应用", "title": "测试"})
        self.assertEqual(d.action, "dropped")
        self.assertEqual(d.reason, "invalid_or_ambiguous")

    def test_modes_and_compose(self):
        relay, _ = self._make_relay()
        # title 模式（验收样例：提醒事项，取快递）
        d = relay.submit({"source_name": "提醒事项", "title": "取快递", "body": "菜鸟驿站"})
        self.assertEqual(d.action, "queued")
        out = relay.pop_ready()
        self.assertIsNotNone(out)
        self.assertEqual(out.speech, "提醒事项，取快递")
        self.assertEqual(out.card, {"source": "提醒事项", "title": "取快递"})

        # title_body 模式
        relay.submit({"source_id": "com.apple.iCal", "title": "周会", "body": "3号会议室"})
        out = relay.pop_ready()
        self.assertEqual(out.speech, "日历，周会。3号会议室")
        self.assertEqual(out.card["body"], "3号会议室")

        # source 模式
        relay.submit({"source_name": "Claude", "title": "已回复"})
        out = relay.pop_ready()
        self.assertEqual(out.speech, "Claude")

        # card 模式（仅卡片，不播报）
        relay.submit({"source_name": "ChatGPT", "title": "完成"})
        out = relay.pop_ready()
        self.assertIsNone(out.speech)
        self.assertEqual(out.card, {"source": "ChatGPT", "title": "完成"})

    def test_dedupe_by_event_id_and_banner_removed_allows_second(self):
        relay, clock = self._make_relay()
        raw = {"source_id": "com.apple.reminders", "event_id": "uuid-1", "title": "取快递"}
        self.assertEqual(relay.submit(raw).action, "queued")
        # 重复观察同一横幅只播一次
        d2 = relay.submit(raw)
        self.assertEqual(d2.action, "dropped")
        self.assertEqual(d2.reason, "duplicate")

        # 横幅消失且超过短时重绘防抖窗后再出现，视为真实的第二条提醒
        relay.banner_removed(raw)
        clock.advance(6)
        self.assertEqual(relay.submit(raw).action, "queued")

    def test_dedupe_blocks_short_uuid_swap_redraw(self):
        relay, clock = self._make_relay()
        raw1 = {"source_id": "com.apple.iCal", "event_id": "uuid-old", "title": "叠放验证二", "body": "今天 23:36"}
        raw2 = {"source_id": "com.apple.iCal", "event_id": "uuid-new", "title": "叠放验证二", "body": "今天 23:36"}
        self.assertEqual(relay.submit(raw1).action, "queued")
        relay.banner_removed(raw1)
        clock.advance(1.0)
        # 1 秒后系统换新 UUID 重绘同内容 → 判定为 duplicate 不重复播报
        d2 = relay.submit(raw2)
        self.assertEqual(d2.action, "dropped")
        self.assertEqual(d2.reason, "duplicate")
        # 超过 5 秒重绘窗后再次出现 → 允许入队
        clock.advance(5.0)
        self.assertEqual(relay.submit(raw2).action, "queued")

    def test_dedupe_without_event_id_uses_window(self):
        relay, clock = self._make_relay(settings={"dedupe_window": 20.0})
        raw = {"source_id": "com.apple.reminders", "title": "喝水"}
        self.assertEqual(relay.submit(raw).action, "queued")
        clock.advance(10)
        self.assertEqual(relay.submit(raw).reason, "duplicate")
        clock.advance(15)
        self.assertEqual(relay.submit(raw).action, "queued")

    def test_locked_or_unknown_silent_and_rechecked_on_pop(self):
        clock = FakeClock()
        locked_state = [None]
        relay = NotifyRelay(
            settings={"enabled": True, "sources": {"com.apple.reminders": "title"}},
            clock=clock,
            is_locked=lambda: locked_state[0],
        )
        # 未知锁屏状态默认静默
        self.assertEqual(relay.submit({"source_name": "提醒事项", "title": "A"}).reason, "locked_or_unknown")

        # 解锁时入队，出队前锁屏 → 下发前重查丢弃
        locked_state[0] = False
        self.assertEqual(relay.submit({"source_name": "提醒事项", "title": "B"}).action, "queued")
        locked_state[0] = True
        self.assertIsNone(relay.pop_ready())
        self.assertEqual(relay.counters.get("dropped:locked_or_unknown"), 2)

    def test_busy_defer_and_ttl_expiry(self):
        relay, clock = self._make_relay(settings={"ttl_seconds": 30.0})
        relay.submit({"source_id": "com.apple.reminders", "title": "取快递"})
        # 忙时延后，不出队
        self.assertIsNone(relay.pop_ready(busy=True))
        self.assertEqual(len(relay.queue), 1)
        self.assertEqual(relay.counters.get("deferred"), 1)
        # 超过 TTL 后丢弃
        clock.advance(31)
        self.assertIsNone(relay.pop_ready(busy=False))
        self.assertEqual(relay.counters.get("dropped:expired"), 1)

    def test_pause_one_hour_and_unselected_source_silent(self):
        relay, clock = self._make_relay()
        # 未勾选来源保持静默
        d = relay.submit({"source_id": "com.apple.ScriptEditor2", "title": "hello"})
        self.assertEqual(d.reason, "source_off")

        # 暂停一小时
        relay.pause(3600)
        d2 = relay.submit({"source_id": "com.apple.reminders", "title": "取快递"})
        self.assertEqual(d2.reason, "paused")
        clock.advance(3601)
        self.assertEqual(relay.submit({"source_id": "com.apple.reminders", "title": "取快递"}).action, "queued")

    def test_status_contains_no_notification_text(self):
        relay, _ = self._make_relay()
        relay.submit({"source_id": "com.apple.reminders", "title": "机密标题", "body": "机密正文"})
        st = str(relay.status())
        self.assertNotIn("机密标题", st)
        self.assertNotIn("机密正文", st)


class _StubEngine:
    def __init__(self):
        self.config = {}
        self.emitted = []
        self.instance_id = "inst-test"
        self.saved = 0

    def emit(self, kind, **payload):
        self.emitted.append((kind, payload))

    def _save_config(self):
        self.saved += 1


class NotifyServiceAndSocketTest(unittest.TestCase):
    def test_unix_socket_ingest_and_permissions(self):
        clock = FakeClock()
        eng = _StubEngine()
        svc = NotifyService(eng, config={"enabled": True, "sources": {"com.apple.reminders": "title"}},
                            save=eng._save_config, clock=clock)
        with tempfile.TemporaryDirectory() as td:
            sock_path = Path(td) / "priv" / "notify.sock"
            svc.start_socket(sock_path)
            try:
                mode_dir = stat.S_IMODE(os.stat(sock_path.parent).st_mode)
                mode_sock = stat.S_IMODE(os.stat(sock_path).st_mode)
                self.assertEqual(mode_dir, 0o700)
                self.assertEqual(mode_sock, 0o600)

                def send(obj):
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                        c.connect(str(sock_path))
                        c.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

                send({"type": "heartbeat", "trusted": True, "locked": False, "macOS": "27.0.1"})
                send({"type": "banner_added", "source_name": "提醒事项", "event_id": "u-1", "title": "取快递", "locked": False, "trusted": True})
                deadline = time.time() + 2.0
                while time.time() < deadline and len(eng.emitted) < 2:
                    time.sleep(0.02)
                st = svc.status()
                self.assertTrue(st["reader"]["connected"])
                self.assertTrue(st["reader"]["trusted"])
                self.assertFalse(st["reader"]["locked"])
                self.assertEqual([k for k, _ in eng.emitted], ["miniapp_card", "speak"])
                self.assertEqual(eng.emitted[1][1]["text"], "提醒事项，取快递")
                self.assertEqual(st["counters"].get("dispatched_speech"), 1)
            finally:
                svc.close()
            self.assertFalse(sock_path.exists())

    def test_preview_only_shows_locally_and_expires(self):
        clock = FakeClock()
        eng = _StubEngine()
        svc = NotifyService(eng, config={"enabled": True, "sources": {"com.apple.reminders": "title"}},
                            save=eng._save_config, clock=clock)
        svc.ingest_message({"type": "heartbeat", "trusted": True, "locked": False})
        svc.arm_preview(source_id="com.apple.reminders", ttl=60)
        res = svc.ingest_message({
            "type": "banner_added",
            "source_name": "提醒事项",
            "title": "ROKIDTEST 预览",
            "body": "仅本机展示",
        })
        self.assertEqual(res["action"], "preview_captured")
        self.assertEqual(svc.status()["queued"], 0)
        self.assertEqual(svc.status()["preview"]["item"]["title"], "ROKIDTEST 预览")
        self.assertEqual(eng.emitted, [])

        clock.advance(61)
        self.assertIsNone(svc.status()["preview"]["item"])

    def test_silent_test_card_emits_card_only_no_speech(self):
        eng = _StubEngine()
        svc = NotifyService(eng, save=eng._save_config)
        res = svc.send_test_card("com.apple.reminders", speak=False)
        self.assertTrue(res["ok"])
        self.assertTrue(res["emitted"])
        self.assertFalse(res["spoken"])
        self.assertEqual([k for k, _ in eng.emitted], ["miniapp_card"])

        # 默认播报测试卡：发卡片 + 播报指定会议提醒文案
        eng.emitted.clear()
        res2 = svc.send_test_card()
        self.assertTrue(res2["spoken"])
        self.assertEqual([k for k, _ in eng.emitted], ["miniapp_card", "speak"])
        self.assertEqual(eng.emitted[1][1]["text"], "一小时后，您在大会议室有个重要会议")
        self.assertEqual(eng.emitted[1][1]["agent"], "notify")

    def test_http_endpoints_and_console_html(self):
        import config as cfg_mod
        from engine import Engine
        from server import make_handler

        with tempfile.TemporaryDirectory() as td:
            apps_dir = Path(td) / "Applications"
            # 模拟两个本机已安装应用（含 InfoPlist.loctable 中文名）
            wx_res = apps_dir / "WeChat.app" / "Contents" / "Resources"
            wx_res.mkdir(parents=True)
            (apps_dir / "WeChat.app" / "Contents" / "Info.plist").write_bytes(
                plistlib.dumps({"CFBundleIdentifier": "com.tencent.xinWeChat", "CFBundleName": "WeChat"})
            )
            (wx_res / "InfoPlist.loctable").write_bytes(
                plistlib.dumps({"zh_CN": {"CFBundleDisplayName": "微信"}})
            )
            cl_c = apps_dir / "Claude.app" / "Contents"
            cl_c.mkdir(parents=True)
            (cl_c / "Info.plist").write_bytes(
                plistlib.dumps({"CFBundleIdentifier": "com.anthropic.claudefordesktop", "CFBundleDisplayName": "Claude"})
            )

            cfg = cfg_mod.load_config(Path(td) / "config.json")
            cfg["token"] = "tok-notify"
            eng = Engine(cfg, state_file=str(Path(td) / "state.json"))
            eng._proactive_stop.set()
            eng.notify._app_dirs = [apps_dir]
            srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(eng, cfg["token"]))
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                def req(method, path, body=None):
                    data = json.dumps(body).encode("utf-8") if body is not None else None
                    r = urllib.request.Request(
                        f"http://127.0.0.1:{port}{path}",
                        data=data,
                        method=method,
                        headers={"Authorization": "Bearer tok-notify", "Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(r, timeout=5) as resp:
                        return json.loads(resp.read().decode("utf-8"))

                st = req("GET", "/api/notify")
                self.assertFalse(st["enabled"])
                self.assertEqual([x["id"] for x in st["catalog"]], ["com.apple.reminders", "com.apple.iCal"])
                self.assertTrue(all(x["builtin"] for x in st["catalog"]))

                # 读取本机应用程序目录列表
                inst = req("GET", "/api/notify/installed-apps")
                self.assertEqual([a["id"] for a in inst["apps"]], ["com.anthropic.claudefordesktop", "com.tencent.xinWeChat"])

                # 伪造/不存在的 source_id 必须拒绝（400）
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    req("POST", "/api/notify/sources/add", {"source_id": "com.fake.typo", "mode": "title"})
                self.assertEqual(ctx.exception.code, 400)

                # 从已安装列表选取添加「微信」和「Claude」
                st_add1 = req("POST", "/api/notify/sources/add", {"source_id": "com.tencent.xinWeChat", "mode": "title"})
                self.assertTrue(st_add1["ok"])
                self.assertEqual([x["name"] for x in st_add1["catalog"]], ["提醒事项", "日历", "微信"])
                self.assertEqual(st_add1["sources"]["com.tencent.xinWeChat"], "title")

                # 重复添加必须返回失败反馈
                with self.assertRaises(urllib.error.HTTPError) as ctx2:
                    req("POST", "/api/notify/sources/add", {"source_id": "com.tencent.xinWeChat", "mode": "title"})
                self.assertEqual(ctx2.exception.code, 400)

                st_add2 = req("POST", "/api/notify/sources/add", {"source_id": "com.anthropic.claudefordesktop", "mode": "source"})
                self.assertEqual(st_add2["sources"]["com.anthropic.claudefordesktop"], "source")

                # 删除自定义 App 后恢复出现在候选列表
                st_rm = req("POST", "/api/notify/sources/remove", {"source_id": "com.tencent.xinWeChat"})
                self.assertTrue(st_rm["ok"])
                self.assertEqual([x["name"] for x in st_rm["catalog"]], ["提醒事项", "日历", "Claude"])
                inst2 = req("GET", "/api/notify/installed-apps")
                self.assertEqual([a["id"] for a in inst2["apps"]], ["com.tencent.xinWeChat"])

                # 内置应用不可删除
                self.assertFalse(eng.notify.remove_custom_source("com.apple.reminders")["ok"])

                st2 = req("POST", "/api/notify/settings", {
                    "enabled": True,
                    "sources": {"com.apple.reminders": "title"},
                })
                self.assertTrue(st2["enabled"])
                self.assertEqual(st2["sources"]["com.apple.reminders"], "title")

                st3 = req("POST", "/api/notify/pause", {"seconds": 3600})
                self.assertTrue(st3["paused"])

                st4 = req("POST", "/api/notify/preview", {"ttl": 60})
                self.assertTrue(st4["preview"]["armed"])

                tc = req("POST", "/api/notify/test-card", {"source_id": "com.apple.reminders"})
                self.assertTrue(tc["ok"])
                self.assertTrue(tc["emitted"])
            finally:
                srv.shutdown()
                srv.server_close()
                eng.close()

        html = (HOST / "web" / "console.html").read_text(encoding="utf-8")
        for token in (
            'data-tab="notify"',
            'id="tab-notify"',
            'id="notifyenabled"',
            'id="notifyreader"',
            'id="notifypause"',
            'id="notifypreview"',
            'id="notifytestcard"',
            'id="notifycustomapp"',
            'id="notifycustommode"',
            'id="notifycustomadd"',
            'id="notifyaddfeedback"',
            'id="notifysave"',
            "/api/notify/installed-apps",
            "/api/notify/settings",
            "/api/notify/sources/add",
            "/api/notify/sources/remove",
            "/api/notify/test-card",
            "原厂云 TTS",
        ):
            self.assertIn(token, html)

    def test_custom_app_name_matches_banner_and_dispatches(self):
        with tempfile.TemporaryDirectory() as td:
            apps_dir = Path(td) / "Applications"
            wx_res = apps_dir / "WeChat.app" / "Contents" / "Resources"
            wx_res.mkdir(parents=True)
            (apps_dir / "WeChat.app" / "Contents" / "Info.plist").write_bytes(
                plistlib.dumps({"CFBundleIdentifier": "com.tencent.xinWeChat", "CFBundleName": "WeChat"})
            )
            (wx_res / "InfoPlist.loctable").write_bytes(
                plistlib.dumps({"zh_CN": {"CFBundleDisplayName": "微信"}})
            )
            eng = _StubEngine()
            svc = NotifyService(eng, config={"enabled": True}, save=eng._save_config, app_dirs=[apps_dir])
            res = svc.add_custom_source("com.tencent.xinWeChat", mode="title")
            self.assertTrue(res["ok"])
            svc.ingest_message({"type": "heartbeat", "trusted": True, "locked": False})
            # 中文横幅与英文横幅（aliases）均可精准匹配
            svc.ingest_message({
                "type": "banner_added",
                "source_name": "微信",
                "event_id": "wx-1",
                "title": "小明发来一条消息",
                "locked": False,
                "trusted": True,
            })
            svc.on_device_event("playback_done", {})
            svc.ingest_message({
                "type": "banner_added",
                "source_name": "WeChat",
                "event_id": "wx-2",
                "title": "小红发来一条消息",
                "locked": False,
                "trusted": True,
            })
            speaks = [p["text"] for k, p in eng.emitted if k == "speak"]
            self.assertEqual(speaks, ["微信，小明发来一条消息", "微信，小红发来一条消息"])
            # 验证默认卡片使用 notify_carousel，前两个槽位永远是日历和提醒事项，微信落在中间选框(index=2)
            card0 = eng.emitted[0][1]["card"]
            self.assertEqual(card0["layout"], "notify_carousel")
            self.assertEqual([it["name"] for it in card0["items"]], ["日历", "提醒事项", "微信"])
            self.assertEqual(card0["target_index"], 2)
            svc.close()


class NotifyBatch4DeliveryTest(unittest.TestCase):
    def test_dispatch_isolated_from_neko_ear_mode_and_history(self):
        import config as cfg_mod
        from engine import Engine

        with tempfile.TemporaryDirectory() as td:
            cfg = cfg_mod.load_config(Path(td) / "config.json")
            cfg["neko_ear_only"] = True
            eng = Engine(cfg, state_file=str(Path(td) / "state.json"))
            eng._proactive_stop.set()
            try:
                eng.current = "neko"
                mac_calls = []
                eng._speak_on_mac = lambda *a, **kw: mac_calls.append((a, kw))
                emitted = []
                orig_emit = eng.emit
                eng.emit = lambda kind, **kw: (emitted.append((kind, kw)), orig_emit(kind, **kw))[1]

                eng.notify.update_settings({
                    "enabled": True,
                    "sources": {"com.apple.reminders": "title"},
                })
                eng.notify.ingest_message({"type": "heartbeat", "trusted": True, "locked": False})
                eng.notify.ingest_message({
                    "type": "banner_added",
                    "source_name": "提醒事项",
                    "event_id": "rem-1",
                    "title": "取快递",
                    "locked": False,
                    "trusted": True,
                })

                kinds = [k for k, _ in emitted]
                self.assertEqual(kinds, ["miniapp_card", "speak"])
                card_payload = emitted[0][1]
                speak_payload = emitted[1][1]
                self.assertEqual(speak_payload["agent"], "notify")
                self.assertEqual(speak_payload["text"], "提醒事项，取快递")
                self.assertEqual(speak_payload["card_id"], card_payload["card_id"])
                # 隔离断言：不切助手、不走猫娘 Mac 发声、不写对话历史
                self.assertEqual(eng.current, "neko")
                self.assertEqual(mac_calls, [])
                self.assertEqual(sum(eng.threads()["counts"].values()), 0)

                # playback_done 回执单独计数
                eng.handle_event({"type": "playback_done", "request_id": speak_payload["request_id"]})
                self.assertEqual(eng.notify.status()["counters"].get("playback_done"), 1)
            finally:
                eng.close()

    def test_card_only_mode_emits_no_speak(self):
        eng = _StubEngine()
        svc = NotifyService(eng, config={"enabled": True, "sources": {"com.openai.codex": "card"}})
        svc.ingest_message({"type": "heartbeat", "trusted": True, "locked": False})
        svc.ingest_message({"type": "banner_added", "source_name": "ChatGPT", "event_id": "c-1", "title": "已回复"})
        self.assertEqual([k for k, _ in eng.emitted], ["miniapp_card"])
        self.assertEqual(svc.status()["counters"].get("dispatched_card"), 1)
        self.assertIsNone(svc.status()["counters"].get("dispatched_speech"))

    def test_playback_done_advances_queue_and_tts_error_never_replays(self):
        clock = FakeClock()
        eng = _StubEngine()
        svc = NotifyService(
            eng,
            config={"enabled": True, "sources": {"com.apple.reminders": "title"}},
            clock=clock,
        )
        svc.ingest_message({"type": "heartbeat", "trusted": True, "locked": False})
        svc.ingest_message({"type": "banner_added", "source_name": "提醒事项", "event_id": "e-1", "title": "第一条"})
        svc.ingest_message({"type": "banner_added", "source_name": "提醒事项", "event_id": "e-2", "title": "第二条"})
        # 第一条已下发，第二条因第一条在途而排队
        self.assertEqual([k for k, _ in eng.emitted], ["miniapp_card", "speak"])
        req1 = eng.emitted[1][1]["request_id"]
        self.assertEqual(svc.status()["queued"], 1)

        # 第一条回报 tts_error：绝不重播第一条，而是推进下一条
        svc.on_device_event("tts_error", {"request_id": req1})
        self.assertEqual(svc.status()["counters"].get("playback_error"), 1)
        speaks = [p["text"] for k, p in eng.emitted if k == "speak"]
        self.assertEqual(speaks, ["提醒事项，第一条", "提醒事项，第二条"])
        req2 = [p["request_id"] for k, p in eng.emitted if k == "speak"][1]

        # 第二条回报 playback_done（兼容设备端 RokidTts 上报的数字 request_id，如 "219"）
        svc.on_device_event("playback_done", {"request_id": "219"})
        self.assertEqual(svc.status()["counters"].get("playback_done"), 1)
        self.assertEqual(svc.status()["queued"], 0)
        svc.close()

    def test_busy_conversation_or_music_defers_until_idle(self):
        clock = FakeClock()
        eng = _StubEngine()
        busy_flag = [True]
        eng.is_busy_for_notify = lambda: busy_flag[0]
        svc = NotifyService(
            eng,
            config={"enabled": True, "sources": {"com.apple.iCal": "title"}},
            clock=clock,
        )
        try:
            svc.ingest_message({"type": "heartbeat", "trusted": True, "locked": False})
            svc.ingest_message({"type": "banner_added", "source_name": "日历", "event_id": "cal-1", "title": "周会"})
            self.assertEqual(eng.emitted, [])
            self.assertEqual(svc.status()["queued"], 1)
            self.assertEqual(svc.status()["counters"].get("deferred"), 1)

            # 对话/音乐结束，playback_done 到来后自动下发
            busy_flag[0] = False
            svc.on_device_event("playback_done", {})
            self.assertEqual([k for k, _ in eng.emitted], ["miniapp_card", "speak"])
            self.assertEqual(eng.emitted[1][1]["text"], "日历，周会")
        finally:
            svc.close()


if __name__ == "__main__":
    unittest.main()
