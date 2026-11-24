"""The chat console: models, direct settings and typed input.

These cover the parts the console depends on: the model list of an
assistant, changing that model, saving the direct configuration, and a
typed line reaching the right backend.
"""
import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
import asr_watcher  # noqa: E402
import intent  # noqa: E402
from backends.direct import FULL, HYBRID, OFF  # noqa: E402
from engine import Engine  # noqa: E402


class FakeBackend:
    """A backend that answers with a fixed line."""

    def __init__(self, settings=None, state_store=None):
        self.answers = []
        self.settings = settings or {}
        self.state_store = state_store
        self.model = str(self.settings.get("model") or "")

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

    def models(self):
        return ["alpha", "beta"]

    def current_model(self):
        return self.model

    def switch_model(self, name):
        self.model = str(name or "").strip()
        return self.model


class ConsoleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.config_path = Path(self.tmp.name) / "config.json"
        self.config = config_module.create_config(self.config_path)
        self.config["_root"] = self.tmp.name
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.config["_path"] = str(self.config_path)
        self.engine = Engine(self.config, state_file=str(self.state))
        self.backend = FakeBackend()
        self.engine.backend_for = lambda agent_id: self.backend

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    # -- models ---------------------------------------------------------------

    def test_model_list_comes_from_the_backend(self):
        current, names = self.engine.agent_models("hermes")
        self.assertEqual(names, ["alpha", "beta"])
        self.assertEqual(current, "")

    def test_switching_a_model_reaches_the_backend(self):
        self.engine.set_agent_model("hermes", "beta")
        self.assertEqual(self.backend.model, "beta")

    def test_switching_a_model_is_written_to_the_config(self):
        self.engine.set_agent_model("hermes", "beta")
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["agents"]["hermes"]["model"], "beta")

    def test_switching_a_model_survives_a_restart(self):
        self.engine.set_agent_model("hermes", "beta")
        again = config_module.load_config(self.config_path)
        self.assertEqual(again["agents"]["hermes"]["model"], "beta")

    def test_an_empty_model_falls_back_to_the_default(self):
        self.engine.set_agent_model("hermes", "beta")
        self.engine.set_agent_model("hermes", "")
        self.assertEqual(self.backend.model, "")

    def test_an_unknown_assistant_is_refused(self):
        with self.assertRaises(ValueError):
            self.engine.set_agent_model("nobody", "beta")

    def test_direct_mode_reuses_the_assistants_model(self):
        # Direct mode answers with the reused assistant's model, so the
        # console shows the model that is really in use.
        current, names = self.engine.agent_models("direct")
        self.assertEqual(names, ["alpha", "beta"])

    def test_direct_mode_with_a_custom_endpoint_shows_its_model(self):
        self.engine.set_direct_settings({
            "source": "custom", "custom": {"model": "custom-model"}})
        current, names = self.engine.agent_models("direct")
        self.assertEqual(current, "custom-model")
        self.assertEqual(names, [])

    def test_changing_the_direct_model_changes_the_assistants_model(self):
        self.engine.set_agent_model("direct", "beta")
        self.assertEqual(self.backend.model, "beta")
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["agents"]["hermes"]["model"], "beta")

    # -- direct settings ------------------------------------------------------

    def test_the_mode_is_saved(self):
        self.engine.set_direct_settings({"mode": FULL})
        self.assertEqual(self.engine.direct_mode(), FULL)

    def test_the_source_is_saved(self):
        self.engine.set_direct_settings({"mode": HYBRID, "source": "custom"})
        self.assertEqual(self.engine.direct_status()["source"], "custom")

    def test_the_custom_endpoint_is_saved(self):
        self.engine.set_direct_settings({
            "mode": HYBRID, "source": "custom",
            "custom": {"base_url": "https://example.com/v1",
                       "model": "some-model", "api_key": "sk-secret"}})
        status = self.engine.direct_status()
        self.assertEqual(status["custom"]["base_url"], "https://example.com/v1")
        self.assertEqual(status["custom"]["model"], "some-model")
        self.assertTrue(status["custom"]["api_key_set"])

    def test_the_api_key_never_leaves_the_host(self):
        self.engine.set_direct_settings({
            "custom": {"api_key": "sk-secret"}})
        status = self.engine.direct_status()
        self.assertNotIn("sk-secret", json.dumps(status))

    def test_an_empty_api_key_keeps_the_stored_one(self):
        self.engine.set_direct_settings({"custom": {"api_key": "sk-secret"}})
        self.engine.set_direct_settings({"custom": {"api_key": "", "model": "other"}})
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["direct"]["custom"]["api_key"], "sk-secret")
        self.assertEqual(saved["direct"]["custom"]["model"], "other")

    def test_the_settings_survive_a_restart(self):
        self.engine.set_direct_settings({
            "mode": FULL, "source": "custom",
            "custom": {"base_url": "https://example.com/v1", "model": "m"}})
        again = config_module.load_config(self.config_path)
        self.assertEqual(again["direct"]["mode"], FULL)
        self.assertEqual(again["direct"]["source"], "custom")
        self.assertEqual(again["direct"]["custom"]["model"], "m")

    def test_an_unknown_source_is_ignored(self):
        self.engine.set_direct_settings({"source": "carrier-pigeon"})
        self.assertEqual(self.engine.direct_status()["source"], "assistant")

    # -- typed input ----------------------------------------------------------

    def test_a_typed_line_reaches_the_assistant(self):
        result = self.engine.chat("你好", "hermes")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["agent"], "hermes")
        # The worker runs in the background; give it a moment.
        for _ in range(50):
            if self.backend.answers:
                break
            import time
            time.sleep(0.02)
        self.assertEqual(self.backend.answers, ["你好"])

    def test_a_typed_line_is_never_read_as_a_voice_command(self):
        # "退出助手" is a command when spoken. Typed, it is a message.
        result = self.engine.chat("退出助手", "hermes")
        self.assertTrue(result["accepted"])
        self.assertEqual(self.engine.selected, None)

    def test_a_typed_line_to_an_unknown_assistant_is_refused(self):
        with self.assertRaises(ValueError):
            self.engine.chat("你好", "nobody")

    def test_an_empty_typed_line_is_refused(self):
        with self.assertRaises(ValueError):
            self.engine.chat("   ", "hermes")

    def test_a_typed_line_to_an_assistant_that_is_off_is_refused(self):
        # The rule is what matters, not which assistant happens to be off
        # today, so the test switches one off itself.
        with mock.patch.dict(config_module.REGISTRY["hermes"], {"enabled": False}):
            with self.assertRaises(ValueError):
                self.engine.chat("你好", "hermes")

    def test_a_typed_line_does_not_change_the_selected_assistant(self):
        self.engine.select("dsh")
        self.engine.chat("你好", "hermes")
        self.assertEqual(self.engine.selected, "dsh")

    # -- transcript -----------------------------------------------------------

    def test_the_transcript_grows_by_number(self):
        self.engine.log_transcript("user", "hermes", "一")
        self.engine.log_transcript("agent", "hermes", "二")
        entries = self.engine.transcript_since(0)
        self.assertEqual(len(entries["items"]), 2)
        self.assertEqual(entries["items"][0]["seq"], 1)
        self.assertEqual(entries["last_seq"], 2)

    # -- one conversation per assistant ----------------------------------------

    def test_the_conversations_are_split_up_per_assistant(self):
        self.engine.log_transcript("user", "hermes", "问小赫")
        self.engine.log_transcript("agent", "hermes", "小赫答")
        self.engine.log_transcript("user", "dsh", "问大肥鱼")
        self.engine.log_transcript("agent", "dsh", "大肥鱼答")
        threads = self.engine.threads()["threads"]
        self.assertEqual([e["message"] for e in threads["hermes"]],
                         ["问小赫", "小赫答"])
        self.assertEqual([e["message"] for e in threads["dsh"]],
                         ["问大肥鱼", "大肥鱼答"])

    def test_the_speaker_has_a_conversation_of_its_own(self):
        # The direct path is the speaker itself, so it keeps its own thread.
        self.engine.log_transcript("user", "direct", "你好")
        self.engine.log_transcript("agent", "direct", "你好呀")
        threads = self.engine.threads()["threads"]
        self.assertEqual([e["message"] for e in threads["direct"]],
                         ["你好", "你好呀"])

    def test_the_device_notices_go_to_the_speaker(self):
        # A message about the device itself belongs to the speaker, not to
        # any one assistant.
        self.engine.log_transcript("system", "none", "语音引擎已重启")
        self.engine.log_transcript("system", "", "已退出助手")
        threads = self.engine.threads()["threads"]
        self.assertEqual([e["message"] for e in threads["direct"]],
                         ["语音引擎已重启", "已退出助手"])

    def test_an_assistants_own_conversation_is_not_mixed_up(self):
        # The point of the split: what one assistant said must not show up
        # in another assistant's conversation.
        self.engine.log_transcript("agent", "hermes", "小赫答")
        self.engine.log_transcript("agent", "dsh", "大肥鱼答")
        threads = self.engine.threads()["threads"]
        self.assertNotIn("大肥鱼答", [e["message"] for e in threads["hermes"]])
        self.assertNotIn("小赫答", [e["message"] for e in threads["dsh"]])

    def test_the_threads_keep_the_order_they_came_in(self):
        self.engine.log_transcript("user", "dsh", "一")
        self.engine.log_transcript("agent", "dsh", "二")
        self.engine.log_transcript("user", "dsh", "三")
        threads = self.engine.threads()["threads"]
        self.assertEqual([e["message"] for e in threads["dsh"]], ["一", "二", "三"])

    def test_a_long_conversation_does_not_swallow_the_others(self):
        # Ten lines for one assistant must not push the other one out.
        for i in range(10):
            self.engine.log_transcript("user", "hermes", "小赫%d" % i)
        self.engine.log_transcript("user", "dsh", "大肥鱼的一句")
        threads = self.engine.threads()["threads"]
        self.assertEqual([e["message"] for e in threads["dsh"]], ["大肥鱼的一句"])

    def test_only_the_new_entries_are_sent(self):
        self.engine.log_transcript("user", "hermes", "一")
        first = self.engine.transcript_since(0)
        self.engine.log_transcript("agent", "hermes", "二")
        second = self.engine.transcript_since(first["last_seq"])
        self.assertEqual(len(second["items"]), 1)
        self.assertEqual(second["items"][0]["message"], "二")

    # -- the console page keeps one conversation per assistant -----------------

    def test_the_console_page_is_valid_javascript(self):
        """A typo in the page breaks everything, and nothing says so.

        A stray bracket makes the whole script fail to parse: the page
        then shows nothing at all, with no error anywhere. The browser
        is the only one that notices, and it does not say a word. So the
        script is handed to a JavaScript engine here, which does say.
        """
        import shutil
        import subprocess
        import tempfile
        node = shutil.which("node")
        if not node:
            known = Path.home() / ".workbuddy" / "binaries" / "node" / "versions"
            if known.is_dir():
                for version in sorted(known.iterdir(), reverse=True):
                    candidate = version / "bin" / "node"
                    if candidate.exists():
                        node = str(candidate)
                        break
        if not node:
            self.skipTest("no JavaScript engine on this machine")

        page = (Path(__file__).resolve().parents[1] / "host" / "web" /
                "console.html").read_text(encoding="utf-8")
        # The page has two script blocks: the theme one in the head and
        # the main one. The main one is the last one.
        start = page.rindex("<script>")
        end = page.rindex("</script>")
        script = page[start + len("<script>"):end]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "console.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([node, "--check", str(path)],
                                    capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         result.stderr.decode("utf-8", "replace")[:2000])

    def test_the_console_page_keeps_a_pane_per_assistant(self):
        # The page has to split the conversation up, not show one mixed
        # stream: every assistant gets its own pane, and only the selected
        # one is visible.
        page = (Path(__file__).resolve().parents[1] / "host" / "web" /
                "console.html").read_text(encoding="utf-8")
        self.assertIn("function threadKey", page)
        self.assertIn("function syncThreadVisibility", page)
        self.assertIn("function threadOf", page)
        self.assertIn("threads[", page)
        self.assertIn('.pane.active', page)

    def test_the_console_page_does_not_mix_the_conversations(self):
        page = (Path(__file__).resolve().parents[1] / "host" / "web" /
                "console.html").read_text(encoding="utf-8")
        # The old single-thread rendering is gone.
        self.assertNotIn('$("thread").appendChild(d);', page)
        # Every entry goes through the per-assistant bucket instead.
        self.assertIn("items.forEach(item => addEntry(item, false))", page)
        # Only a newly arrived agent reply raises the notification badge;
        # first-paint history, switch notices and the user's lines do not.
        self.assertIn('isNew && item.role === "agent"', page)
        self.assertIn("renderEntries(data.items, true)", page)

    def test_the_console_page_reads_the_split_conversation(self):
        page = (Path(__file__).resolve().parents[1] / "host" / "web" /
                "console.html").read_text(encoding="utf-8")
        self.assertIn("/api/threads", page)
        self.assertIn("async function loadThreads", page)

    def test_the_console_page_does_not_show_the_same_line_twice(self):
        # The full load and the incremental poll can hand over the same
        # entry, so the page drops what it has already drawn.
        page = (Path(__file__).resolve().parents[1] / "host" / "web" /
                "console.html").read_text(encoding="utf-8")
        self.assertIn("const seen = new Set()", page)
        self.assertIn("seen.has(seq)", page)

    def test_the_roon_card_can_point_at_another_core(self):
        # The Core address used to be baked into the sidecar defaults; the
        # console has to let the user retarget it and survive a Core move.
        page = (Path(__file__).resolve().parents[1] / "host" / "web" /
                "console.html").read_text(encoding="utf-8")
        self.assertIn('id="roonhost"', page)
        self.assertIn('id="roonport"', page)
        self.assertIn('id="roonhostsave"', page)
        # The save goes through the Roon settings route and carries the host.
        self.assertIn('apiPost("/api/roon/settings", payload)', page)
        self.assertIn("payload = { host }", page)
        # A blank host must not wipe the saved address.
        self.assertIn('if (!host) {', page)

    def test_the_users_words_are_a_blue_bubble_on_the_right(self):
        page = (Path(__file__).resolve().parents[1] / "host" / "web" /
                "console.html").read_text(encoding="utf-8")
        self.assertIn("align-items: flex-end", page)
        self.assertIn("justify-content: flex-end", page)
        self.assertIn("background: var(--accent); border-color: var(--accent); "
                      "color: var(--on-accent);", page)
        # It used to be plain text with no bubble at all.
        self.assertNotIn("background: transparent; border-color: transparent; "
                         "padding: 0;", page)

    def test_repeated_switch_notices_are_folded_into_one_line(self):
        page = (Path(__file__).resolve().parents[1] / "host" / "web" /
                "console.html").read_text(encoding="utf-8")
        self.assertIn("function placeSystem(", page)
        self.assertIn('item.message + " ×" + n', page)
        self.assertIn('if (item.role === "system") placeSystem(t, item, false);', page)
        self.assertIn('if (item.role === "system") placeSystem(t, item, true);', page)

    def test_music_panel_and_audio_channel_contract(self):
        """S3: the console exposes the channel and the device plays a file.

        The device must never stream a network URL directly (no auth, poor
        Android 4.4 support) and must never open a capture stream: doing so
        deadlocks the whole audio HAL on this speaker, factory TTS included.
        """
        root = Path(__file__).resolve().parents[1]
        page = (root / "host" / "web" / "console.html").read_text(
            encoding="utf-8")
        bridge = (root / "android" / "src" / "cn" / "rokid" / "bridge" /
                  "BridgeService.java").read_text(encoding="utf-8")
        self.assertIn('data-tab="music"', page)
        self.assertIn('id="tab-music"', page)
        self.assertIn('"/api/music/play"', page)
        self.assertIn("function watchAudio()", page)
        # The console manages folders only; it never renders the whole
        # library (tens of thousands of rows would freeze the page).
        self.assertIn('"/api/music/folders"', page)
        self.assertIn('id="musicfolders"', page)
        self.assertIn('id="musicshuffle"', page)
        self.assertNotIn('id="musiclibrary"', page)
        self.assertIn("MediaPlayer", bridge)
        self.assertIn("setDataSource(file.getAbsolutePath())", bridge)
        self.assertIn("AudioManager.STREAM_MUSIC", bridge)
        self.assertIn('event.put("source", "audio")', bridge)
        # The rule is stated in a comment too, so look for the *call*.
        self.assertNotIn("new AudioRecord(", bridge)
        self.assertNotIn("new AudioTrack(", bridge)
        # Cover and timed lyrics are drawn on the speaker, not the console.
        screen = (root / "android" / "src" / "cn" / "rokid" / "bridge" /
                  "NowPlayingView.java")
        self.assertTrue(screen.is_file())
        view = screen.read_text(encoding="utf-8")
        self.assertIn("getCurrentPosition()", view)
        self.assertIn("drawBitmap", view)
        self.assertIn("lyric", view.lower())
        self.assertIn("NowPlayingView", bridge)

    def test_the_console_page_notices_a_restarted_host(self):
        """A restarted host numbers the conversation from scratch.

        The page keeps the number of the last line it drew. After a
        restart the host starts counting again, so the old number points
        past the end: every poll would ask for messages that do not
        exist. The page has to recognise the new host and redraw.
        """
        page = (Path(__file__).resolve().parents[1] / "host" / "web" /
                "console.html").read_text(encoding="utf-8")
        self.assertIn("instance_id", page)
        self.assertIn("hostInstance", page)
        self.assertIn("function resetThreads", page)

    def test_the_console_page_keeps_polling_after_a_failed_start(self):
        """One failed request must not stop the page for good.

        Status, conversation and the polling used to sit in one try
        block: a single failure left the page blank and silent until it
        was reloaded by hand. So the polling now starts outside of it.
        """
        page = (Path(__file__).resolve().parents[1] / "host" / "web" /
                "console.html").read_text(encoding="utf-8")
        self.assertIn("setInterval(pollTranscript, 1200)", page)
        boot = page[page.index("async function boot()"):]
        # The intervals come after the last try/catch of the start-up:
        # nothing that can fail is allowed to swallow them.
        last_catch = boot.rindex("catch")
        intervals = boot.index("setInterval(pollTranscript, 1200)")
        self.assertGreater(intervals, last_catch)

    # -- the conversation on disk ---------------------------------------------

    # -- the conversation on disk is never truncated --------------------------

    def test_more_than_the_memory_window_survives_a_restart(self):
        """The 300-line memory window must not cap the record on disk.

        The first version of the history wrote ``list(self.transcript)``
        to a file -- and that is a deque with maxlen=300. So the file
        quietly threw away the oldest lines every time it was saved.
        Writing 350 lines and restarting proved the first line was gone.
        """
        for index in range(350):
            self.engine.log_transcript("user", "hermes", "第%d句" % index)
        self.engine.close()

        again = Engine(self.config, state_file=str(self.state))
        try:
            # Only 300 lines fit in memory, as the user asked.
            self.assertEqual(len(again.transcript), 300)
            # But the file has all of them, and they can be read back.
            self.assertEqual(again.history.counts().get("hermes"), 350)
            every = [e["message"] for e in again.history.recent("hermes", 1000)]
            self.assertEqual(len(every), 350)
            self.assertEqual(every[0], "第0句")
            self.assertEqual(every[-1], "第349句")
        finally:
            again.close()

    def test_a_busy_assistant_cannot_push_another_ones_history_out(self):
        """Each assistant has its own file, so their histories never mix."""
        self.engine.log_transcript("user", "dsh", "大肥鱼最早的一句")
        for index in range(400):
            self.engine.log_transcript("user", "hermes", "小赫%d" % index)
        self.engine.close()

        again = Engine(self.config, state_file=str(self.state))
        try:
            counts = again.history.counts()
            self.assertEqual(counts.get("dsh"), 1)
            self.assertEqual(counts.get("hermes"), 400)
            self.assertIn("大肥鱼最早的一句",
                          [e["message"] for e in again.threads()["threads"]["dsh"]])
        finally:
            again.close()

    def test_the_first_screen_reports_how_much_history_there_is(self):
        for index in range(120):
            self.engine.log_transcript("user", "codex", "第%d句" % index)
        data = self.engine.threads()
        # The console only draws 50; it needs to know there is more.
        self.assertEqual(len(data["threads"]["codex"]), 50)
        self.assertEqual(data["counts"]["codex"], 120)
        self.assertTrue(data["has_more"]["codex"])

    def test_earlier_pages_come_in_order_without_repeating(self):
        for index in range(120):
            self.engine.log_transcript("user", "codex", "第%d句" % index)
        first = self.engine.threads()["threads"]["codex"]
        anchor = first[0]["seq"]
        page = self.engine.history_before("codex", anchor, 50)
        numbers = [int(e["seq"]) for e in page["items"]]
        self.assertEqual(numbers, sorted(numbers))
        self.assertTrue(all(n < anchor for n in numbers))
        self.assertFalse(set(numbers) & {int(e["seq"]) for e in first})
        self.assertTrue(page["has_more"])

    def test_the_console_can_read_lines_that_fell_out_of_memory(self):
        """A tab that slept through more than 300 lines still gets them."""
        for index in range(320):
            self.engine.log_transcript("user", "hermes", "第%d句" % index)
        rows = self.engine.transcript_since(0)["items"]
        messages = [e["message"] for e in rows]
        self.assertIn("第0句", messages)
        self.assertEqual(len(messages), 320)

    def test_the_old_single_file_is_imported_once(self):
        """A host upgraded in place must not lose what it had already kept."""
        from pathlib import Path
        import json
        legacy = Path(self.state).with_name("bridge-transcript.json")
        legacy.write_text(json.dumps({
            "seq": 3,
            "entries": [
                {"seq": 1, "at": 1, "role": "user", "agent": "hermes",
                 "message": "旧的第一句", "request_id": ""},
                {"seq": 2, "at": 2, "role": "agent", "agent": "hermes",
                 "message": "旧的回答", "request_id": ""},
                {"seq": 3, "at": 3, "role": "user", "agent": "dsh",
                 "message": "旧的大肥鱼", "request_id": ""},
            ]}, ensure_ascii=False), encoding="utf-8")
        self.engine.close()

        upgraded = Engine(self.config, state_file=str(self.state))
        try:
            threads = upgraded.threads()["threads"]
            self.assertEqual([e["message"] for e in threads["hermes"]],
                             ["旧的第一句", "旧的回答"])
            self.assertEqual([e["message"] for e in threads["dsh"]],
                             ["旧的大肥鱼"])
            self.assertEqual(upgraded.transcript_seq, 3)
            self.assertTrue(legacy.with_name(legacy.name + ".migrated").exists())
            # Starting again must not import the same lines twice.
            again = Engine(self.config, state_file=str(self.state))
            try:
                self.assertEqual(again.history.counts().get("hermes"), 2)
            finally:
                again.close()
        finally:
            upgraded.close()

    def test_the_conversation_survives_a_restart(self):
        """The history is the only record of what was said.

        It used to live in memory only, so a restart of the host left
        the console with an empty page -- the same as losing the
        answers. So it is written to disk and read back.
        """
        self.engine.log_transcript("user", "direct", "第一句")
        self.engine.log_transcript("agent", "direct", "第二句")
        self.engine.close()

        again = Engine(self.config, state_file=str(self.state))
        try:
            messages = [item["message"] for item in again.transcript]
            self.assertIn("第一句", messages)
            self.assertIn("第二句", messages)
        finally:
            again.close()

    def test_the_numbers_keep_growing_after_a_restart(self):
        """The console asks for "everything after number N".

        After a restart the new lines must carry numbers above the ones
        the console already has, or the console would never ask for
        them.
        """
        self.engine.log_transcript("user", "direct", "第一句")
        self.engine.close()

        again = Engine(self.config, state_file=str(self.state))
        try:
            before = again.transcript_seq
            again.log_transcript("user", "direct", "第二句")
            self.assertGreater(again.transcript_seq, before)
        finally:
            again.close()

    def test_a_restarted_host_has_a_new_identity(self):
        """The console recognises a new host by its identity."""
        first = self.engine.instance_id
        self.engine.close()
        again = Engine(self.config, state_file=str(self.state))
        try:
            self.assertNotEqual(again.instance_id, first)
            self.assertIn("instance_id", again.threads())
            self.assertIn("instance_id", again.transcript_since(0))
        finally:
            again.close()

    def test_every_line_gets_its_own_number(self):
        """Two lines with the same number would lose one of them.

        The console drops what it has already drawn, and it recognises
        what it has drawn by the number. So two lines sharing a number
        means one of them never reaches the screen.
        """
        numbers = []
        for index in range(50):
            numbers.append(self.engine.log_transcript(
                "user", "direct", "第%d句" % index)["seq"])
        self.assertEqual(len(set(numbers)), len(numbers))

    def test_lines_written_at_the_same_time_get_different_numbers(self):
        """Several threads write to the conversation at once."""
        import threading
        numbers = []
        lock = threading.Lock()

        def write(index):
            entry = self.engine.log_transcript("user", "direct", "第%d句" % index)
            with lock:
                numbers.append(entry["seq"])

        threads = [threading.Thread(target=write, args=(index,))
                   for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(numbers)), len(numbers))

    # -- what survives a restart ----------------------------------------------

    def test_the_selected_assistant_comes_back_after_a_restart(self):
        self.engine.select("hermes")
        again = Engine(self.config, state_file=str(self.state))
        try:
            self.assertEqual(again.selected, "hermes")
        finally:
            again.close()

    def test_an_assistant_that_is_off_is_not_restored(self):
        self.engine.store["selected_agent"] = "codex"
        again = Engine(self.config, state_file=str(self.state))
        try:
            self.assertIsNone(again.selected)
        finally:
            again.close()

    def test_switching_to_the_same_assistant_twice_says_it_once(self):
        self.engine.select("hermes")
        before = len(self.engine.transcript)
        self.engine.select("hermes")
        self.assertEqual(len(self.engine.transcript), before)


class WakeSettingsTest(unittest.TestCase):
    """The local wake-word settings behind the console's 唤醒词 page."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.config_path = Path(self.tmp.name) / "config.json"
        self.config = config_module.create_config(self.config_path)
        self.config["_root"] = self.tmp.name
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.config["_path"] = str(self.config_path)
        self.engine = Engine(self.config, state_file=str(self.state))

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def test_the_default_words_are_the_ones_agreed_on(self):
        words = self.engine.wake_settings()
        self.assertFalse(words["enabled"])
        self.assertEqual(words["agents"]["dsh"]["pinyin"], "da4fei2yu2")
        self.assertEqual(words["agents"]["workbuddy"]["pinyin"], "ba1di4")
        self.assertEqual(words["agents"]["codex"]["pinyin"], "ji4pi4ti4")
        self.assertEqual(words["agents"]["hermes"]["pinyin"], "xiao3he4")

    def test_the_master_switch_changes_and_is_saved(self):
        result = self.engine.set_wake(enabled=True)
        self.assertTrue(result["enabled"])
        self.assertTrue(self.engine.wake_settings()["enabled"])
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["wake"]["enabled"])

    def test_badi_vt_word_covers_the_baba_misrecognition(self):
        # A missing alias makes the cloud judge the wake fake and cut the
        # microphone after 0.7s, so every known spelling must be sent down.
        vt = self.engine.wake_settings()["agents"]["workbuddy"]["vt_word"]
        self.assertIn("八八", vt)
        self.assertIn("八弟", vt)

    def test_a_typed_word_is_saved_and_reaches_the_device_poll(self):
        self.engine.set_wake(agent="dsh", pinyin="xiao3yu2")
        self.assertEqual(self.engine.wake_settings()["agents"]["dsh"]["pinyin"],
                         "xiao3yu2")
        # The device reads the settings with every command poll.
        self.assertEqual(self.engine.status()["wake"]["agents"]["dsh"]["pinyin"],
                         "xiao3yu2")

    def test_a_word_that_is_not_tone_marked_pinyin_is_refused(self):
        for bad in ["", "大肥鱼", "dafeiyu", "da0", "da4 fei2",
                    "da4fei2yu2yu2yu2"]:
            with self.assertRaises(ValueError, msg=bad):
                self.engine.set_wake(agent="dsh", pinyin=bad)
        self.assertEqual(self.engine.wake_settings()["agents"]["dsh"]["pinyin"],
                         "da4fei2yu2")

    def test_spaces_and_capitals_are_tidied_up(self):
        # A person types "DA4FEI2YU2 " -- it means the same pinyin.
        self.engine.set_wake(agent="dsh", pinyin="  DA4FEI2YU2  ")
        self.assertEqual(self.engine.wake_settings()["agents"]["dsh"]["pinyin"],
                         "da4fei2yu2")

    def test_an_unknown_assistant_is_refused(self):
        with self.assertRaises(ValueError):
            self.engine.set_wake(agent="nobody", pinyin="xiao3yu2")

    def test_one_row_switch_does_not_touch_the_others(self):
        self.engine.set_wake(agent="dsh", agent_enabled=False)
        words = self.engine.wake_settings()["agents"]
        self.assertFalse(words["dsh"]["enabled"])
        self.assertTrue(words["hermes"]["enabled"])
        self.assertEqual(words["dsh"]["pinyin"], "da4fei2yu2")

    def test_the_words_survive_a_restart(self):
        self.engine.set_wake(enabled=True)
        self.engine.set_wake(agent="codex", pinyin="zhi1pi4ti4")
        again = config_module.load_config(self.config_path)
        self.assertTrue(again["wake"]["enabled"])
        self.assertEqual(again["wake"]["agents"]["codex"]["pinyin"],
                         "zhi1pi4ti4")

    def test_the_console_has_a_wake_word_page(self):
        page = (Path(__file__).resolve().parents[1] / "host" / "web" /
                "console.html").read_text(encoding="utf-8")
        self.assertIn('data-tab="wake"', page)
        self.assertIn('id="tab-wake"', page)
        self.assertIn('id="wakeenable"', page)
        self.assertIn('id="wakerows"', page)
        self.assertIn("da4fei2yu2", page)
        self.assertIn("/api/wake", page)
        self.assertIn("function renderWake", page)

    def test_the_composer_ignores_ime_confirmation_enter(self):
        page = (Path(__file__).resolve().parents[1] / "host" / "web" /
                "console.html").read_text(encoding="utf-8")
        self.assertIn("compositionstart", page)
        self.assertIn("compositionend", page)
        self.assertIn("e.isComposing", page)
        self.assertIn("e.keyCode === 229", page)
        # Enter still sends, and Cmd/Ctrl+Enter is an alternative.
        self.assertIn("e.metaKey || e.ctrlKey", page)
        self.assertIn("e.shiftKey && !accel", page)
        self.assertIn('id="composerhint"', page)

    def test_the_appearance_tab_is_now_other_with_a_scoped_notice_switch(self):
        page = (Path(__file__).resolve().parents[1] / "host" / "web" /
                "console.html").read_text(encoding="utf-8")
        self.assertIn('data-tab="appearance">其它<', page)
        self.assertNotIn('data-tab="appearance">外观<', page)
        self.assertIn('appearance: ["其它"', page)
        self.assertIn('id="factorynoticemute"', page)
        self.assertIn("/api/factory-notice", page)
        self.assertIn("function loadFactoryNotice", page)
        # The label must not imply all factory speech is silenced.
        self.assertIn("只遮蔽到期提示", page)
        self.assertIn("不会屏蔽闹钟", page)


class BadiMisrecognitionTest(unittest.TestCase):
    """2026-11-15: the cloud wrote 巴蒂 as 八八 ("八八控制电脑吗")."""

    def setUp(self):
        aliases = config_module.DEFAULTS["wake"]["agents"]["workbuddy"]["aliases"]
        self.spellings = {"workbuddy": ["巴蒂"] + list(aliases)}
        self.pinyins = {"workbuddy": "ba1di4"}

    def test_the_new_spellings_are_registered_and_in_the_phonetic_fallback(self):
        for word in ("八八", "八弟"):
            self.assertIn(word, self.spellings["workbuddy"])
        self.assertIn("八八", intent.WAKE_PHONETIC["ba1di4"])

    def test_a_baba_sentence_is_routed_and_the_wake_word_is_stripped(self):
        agent, rest = intent.wake_prefix("八八控制电脑吗", self.spellings)
        self.assertEqual(agent, "workbuddy")
        self.assertEqual(rest, "控制电脑吗")

    def test_baba_and_badi_labels_route_to_workbuddy(self):
        for word in ("八八", "八弟"):
            self.assertEqual(
                intent.wake_word_agent(word, self.spellings, self.pinyins),
                "workbuddy")

    def test_the_watcher_logs_the_recognised_text(self):
        # Without it, the next new misrecognition is invisible in asr.log.
        src = (Path(__file__).resolve().parents[1] / "host" /
               "asr_watcher.py").read_text(encoding="utf-8")
        self.assertIn('sent.get("text")', src)


class WakeRoutingTest(unittest.TestCase):
    """A spoken custom wake word picks its assistant on the host."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.config_path = Path(self.tmp.name) / "config.json"
        self.config = config_module.create_config(self.config_path)
        self.config["_root"] = self.tmp.name
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.config["_path"] = str(self.config_path)
        self.engine = Engine(self.config, state_file=str(self.state))
        self.backend = FakeBackend()
        self.engine.backend_for = lambda agent_id: self.backend

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def spoken(self, text):
        return self.engine.handle_event({
            "type": "asr_final", "text": text,
            "request_id": "r" + uuid.uuid4().hex, "source": "bus"})

    def test_nothing_is_routed_while_the_feature_is_off(self):
        self.assertFalse(self.engine.wake_spellings())
        result = self.spoken("大肥鱼今天天气怎么样")
        self.assertNotEqual(result.get("intent"), "select")
        self.assertIsNone(self.engine.selected)

    def test_a_wake_word_selects_its_assistant_and_strips_the_prefix(self):
        self.engine.set_wake(enabled=True)
        result = self.spoken("大肥鱼今天天气怎么样")
        self.assertEqual(result.get("intent"), "command")
        self.assertEqual(self.engine.selected, "dsh")
        self.engine.jobs.join()
        self.assertEqual(self.backend.answers, ["今天天气怎么样"])

    def test_each_word_selects_its_own_assistant(self):
        self.engine.set_wake(enabled=True)
        for word, agent in [("小赫", "hermes"), ("巴蒂", "workbuddy"),
                            ("大肥鱼", "dsh"), ("gpt", "codex")]:
            self.engine.exit(announce=False)
            self.spoken(word + "帮我看看")
            self.assertEqual(self.engine.selected, agent, word)

    def test_a_homophone_of_the_word_still_routes(self):
        self.engine.set_wake(enabled=True)
        # "小鹤" is how the recogniser often writes 小赫.
        self.spoken("小鹤你好")
        self.assertEqual(self.engine.selected, "hermes")

    def test_just_the_word_picks_the_assistant_without_a_question(self):
        self.engine.set_wake(enabled=True)
        result = self.spoken("巴蒂")
        self.assertEqual(result.get("intent"), "select")
        self.assertEqual(self.engine.selected, "workbuddy")
        self.assertEqual(self.backend.answers, [])

    def test_one_row_off_means_that_word_does_not_route(self):
        self.engine.set_wake(enabled=True)
        self.engine.set_wake(agent="dsh", agent_enabled=False)
        self.spoken("大肥鱼你好")
        self.assertIsNone(self.engine.selected)

    def test_the_chinese_name_reaches_the_device_poll(self):
        names = self.engine.status()["wake"]["agents"]
        self.assertEqual(names["dsh"]["name"], "大肥鱼")
        self.assertEqual(names["hermes"]["name"], "小赫")

    def test_console_input_is_not_routed_by_a_wake_word(self):
        self.engine.set_wake(enabled=True)
        # A typed line is a deliberate act: it must not be hijacked into
        # selecting an assistant, whatever the direct mode decides.
        result = self.engine.handle_event({
            "type": "text", "text": "大肥鱼你好",
            "request_id": "r" + uuid.uuid4().hex, "source": "console"})
        self.assertNotEqual(result.get("intent"), "select")
        self.assertIsNone(self.engine.selected)
        self.engine.jobs.join()


class WakeDeviceProtocolTest(unittest.TestCase):
    """The device-side wire format is fixed by the factory APK.

    These constants were read out of openvoice_process.apk and
    rokid_services.jar (BearKid$Stub$Proxy). A wrong transaction number
    would not fail loudly on the speaker -- the words would simply never
    wake -- so the numbers are pinned here.
    """

    def test_the_bearkid_protocol_constants_are_pinned(self):
        source = (Path(__file__).resolve().parents[1] / "android" / "src" /
                  "cn" / "rokid" / "bridge" / "WakeWords.java").read_text(
                      encoding="utf-8")
        self.assertIn('DESCRIPTOR = "com.rokid.voicerec.BearKid"', source)
        self.assertIn("TRANSACTION_ADD = 6", source)
        self.assertIn("TRANSACTION_REMOVE = 7", source)
        self.assertIn("TRANSACTION_LIST = 8", source)
        self.assertIn("VT_TYPE_AWAKE = 1", source)
        self.assertIn('SERVICE_ACTION = "com.rokid.openvoice.VoiceService"', source)
        # The factory's own words must never be removed.
        self.assertIn("managed.contains(word)", source)
        self.assertIn("writeInt(VT_TYPE_AWAKE)", source)
        self.assertIn("data.writeString(pinyin)", source)

    def test_the_bridge_service_syncs_the_words_on_every_poll(self):
        source = (Path(__file__).resolve().parents[1] / "android" / "src" /
                  "cn" / "rokid" / "bridge" / "BridgeService.java").read_text(
                      encoding="utf-8")
        self.assertIn("wakeWords.connect()", source)
        self.assertIn('wakeWords.sync(response.optJSONObject("wake"))', source)
        self.assertIn("wakeWords.close()", source)


class WakeEchoGuardTest(unittest.TestCase):
    """The speaker must not re-select an assistant while it is talking.

    BlackSiren matches wake words in our own TTS. Those hits arrive as
    wake_word events, which used to bypass the echo filter applied to ASR
    text; every reply mentioning a name then switched the assistant.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.config_path = Path(self.tmp.name) / "config.json"
        self.config = config_module.create_config(self.config_path)
        self.config["_root"] = self.tmp.name
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.config["_path"] = str(self.config_path)
        self.engine = Engine(self.config, state_file=str(self.state))
        self.engine.set_wake(enabled=True)

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def wake(self, word):
        self.engine.last_wake_route = ("", 0.0)
        return self.engine.handle_event({
            "type": "wake_word", "word": word,
            "request_id": "w" + uuid.uuid4().hex, "source": "wake-log"})

    def test_no_broadcast_means_a_wake_is_real(self):
        self.assertFalse(self.engine.wake_during_speech())
        self.assertEqual(self.wake("GPT").get("selected_agent"), "codex")

    def test_a_wake_during_playback_is_an_echo(self):
        self.engine.emit("speak", text="GPT在，请说你的任务。")
        self.assertTrue(self.engine.wake_during_speech())
        result = self.wake("GPT")
        self.assertEqual(result.get("intent"), "wake_echo")
        self.assertIsNone(self.engine.selected)

    def test_the_tail_after_playback_is_still_an_echo(self):
        self.engine.emit("speak", text="GPT在，请说你的任务。")
        self.engine.mark_spoken_done()
        self.assertTrue(self.engine.wake_during_speech())

    def test_a_real_wake_after_the_tail_is_routed(self):
        self.engine.emit("speak", text="GPT在，请说你的任务。")
        self.engine.mark_spoken_done()
        older = time.time() - 10
        for entry in self.engine.recent_speeches:
            entry["at"] = older
            entry["ended"] = older
        result = self.wake("巴蒂")
        self.assertEqual(result.get("selected_agent"), "workbuddy")
        self.assertEqual(self.engine.selected, "workbuddy")

    def test_a_burst_of_different_words_is_one_switch(self):
        # The self-wake burst alternates labels to slip past a same-agent
        # window. Without a global gap the screen flips every second.
        first = self.engine.handle_event({
            "type": "wake_word", "word": "GPT",
            "request_id": "burst-1", "source": "wake-log"})
        second = self.engine.handle_event({
            "type": "wake_word", "word": "巴蒂",
            "request_id": "burst-2", "source": "wake-log"})
        self.assertNotIn("duplicate", first)
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(self.engine.selected, "codex")


class WakePlaybackStatusTest(unittest.TestCase):
    """VoiceOptions status lines are not new wake hits."""

    def test_playing_status_line_is_ignored(self):
        line = ("11-14 14:45:14.653 D/speech.speech( 2431): VoiceOptions: "
                'voice_trigger(GPT|计屁替), skill_options({"application":'
                '{"x":{"voice":{"state":"PLAYING","itemId":"D0F2"}}}})')
        self.assertTrue(asr_watcher.is_playback_status(line))
        # Whitespace inside the JSON must not defeat the check.
        self.assertTrue(asr_watcher.is_playback_status(
            line.replace('"state":"PLAYING"', '"state" : "PLAYING"')))

    def test_a_real_black_siren_hit_is_not_a_status_line(self):
        line = ("11-14 14:45:14.643 I/BlackSiren( 2560): "
                "vt word with GPT|计屁替 [3200, 13440]645.597900")
        self.assertFalse(asr_watcher.is_playback_status(line))
        self.assertEqual(asr_watcher.parse_wake_word(line), "GPT|计屁替")

    def test_an_idle_voice_options_line_is_not_a_status_line(self):
        line = ("11-14 14:47:15.643 D/speech.speech( 2431): VoiceOptions: "
                'voice_trigger(), skill_options({"application":{}})')
        self.assertFalse(asr_watcher.is_playback_status(line))


class CodexAliasTableTest(unittest.TestCase):
    """2026-11-15: the cloud writes a spoken "GPT" as gpt / g p t / c p t.

    Trimming this table to a single alias did not reduce false wakes --
    acoustic hits are decided by vt_pinyin -- but it made every real
    wake fail the cloud re-check and lose the mic after 0.7s.
    """

    def setUp(self):
        self.aliases = config_module.DEFAULTS["wake"]["agents"]["codex"]["aliases"]
        self.spellings = {"codex": ["GPT"] + list(self.aliases)}

    def test_every_observed_spelling_is_in_the_default_table(self):
        for spelling in ("gpt", "cpt", "g p t", "c p t", "计屁替"):
            self.assertIn(spelling, self.aliases, spelling)

    def test_the_registered_table_would_accept_those_asr_lines(self):
        table = ["GPT"] + list(self.aliases)
        for heard in ("g p t", "gpt", "c p t"):
            self.assertTrue(any(word in heard for word in table), heard)

    def test_the_whole_vt_word_label_routes_to_codex(self):
        label = "GPT|" + "|".join(self.aliases)
        self.assertEqual(
            intent.wake_word_agent(label, self.spellings,
                                   {"codex": "ji4pi4ti4"}),
            "codex")

    def test_the_misheard_prefix_is_stripped_from_the_question(self):
        for heard in ("gpt帮我写代码", "g p t帮我写代码", "c p t帮我写代码"):
            agent, rest = intent.wake_prefix(heard, self.spellings)
            self.assertEqual(agent, "codex", heard)
            self.assertEqual(rest, "帮我写代码", heard)


class WatcherDeviceSerialTest(unittest.TestCase):
    """The watcher must not append :5555 to a serial that already has it.

    A full serial passed to --device used to become host:port:port, which
    adb rejects; the watcher then looped forever and no wake word reached
    the host, so no assistant was ever selected.
    """

    def test_bare_ip(self):
        self.assertEqual(asr_watcher.device_serial("192.0.2.31"),
                         "192.0.2.31:5555")

    def test_full_serial(self):
        self.assertEqual(asr_watcher.device_serial("192.0.2.31:5555"),
                         "192.0.2.31:5555")

    def test_empty_and_whitespace(self):
        with self.assertRaises(ValueError):
            asr_watcher.device_serial("")
        with self.assertRaises(ValueError):
            asr_watcher.device_serial(None)
        self.assertEqual(asr_watcher.device_serial(" 192.0.2.31:5555 "),
                         "192.0.2.31:5555")


class WakeLogParseTest(unittest.TestCase):
    """The two log lines that tell us which word actually woke the device."""

    def test_black_siren_hit_line(self):
        line = ("11-05 01:50:12.345 I/BlackSiren( 2602): "
                "vt word with 若琪|洛奇 [3200, 9280]11905.688477")
        self.assertEqual(asr_watcher.parse_wake_word(line), "若琪|洛奇")

    def test_a_custom_word_is_kept_whole(self):
        line = ("11-05 01:50:12.345 I/BlackSiren( 2602): "
                "vt word with 大肥鱼 [3200, 9280]11905.688477")
        self.assertEqual(asr_watcher.parse_wake_word(line), "大肥鱼")

    def test_the_voice_options_line(self):
        line = ("11-05 01:50:12.400 D/speech.speech( 2443): VoiceOptions: "
                "stack(abc), voice_trigger(若琪|洛奇), trigger_start(3200), "
                "trigger_length(6080)")
        self.assertEqual(asr_watcher.parse_wake_word(line), "若琪|洛奇")

    def test_unrelated_lines_are_not_wake_words(self):
        for line in ("11-05 01:50:12.345 I/BlackSiren( 2602): vad data with len 145",
                     "11-05 01:50:12.345 I/SirenJava( 2370): "
                     "toStringResult: + type 4 action null nlp null "
                     "location 0.0 extype 0 activation 0 asr 若琪休息吧 energy 0.0"):
            self.assertIsNone(asr_watcher.parse_wake_word(line), line)

    def test_the_final_asr_line_still_parses_as_speech(self):
        line = ("11-05 01:50:12.345 I/SirenJava( 2370): toStringResult: "
                "+ type 4 action null nlp null location 0.0 extype 0 "
                "activation 0 asr 休息吧 energy 0.0")
        self.assertEqual(asr_watcher.parse_final(line), "休息吧")
        self.assertIsNone(asr_watcher.parse_wake_word(line))


class WakeWordEventTest(unittest.TestCase):
    """A word hit reported by the wake engine switches the assistant.

    This is the path that actually runs on the speaker: the recognised
    sentence has its wake word stripped by the NLP layer, so the switch
    has to happen when the word is matched, not when ASR comes back.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.config_path = Path(self.tmp.name) / "config.json"
        self.config = config_module.create_config(self.config_path)
        self.config["_root"] = self.tmp.name
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.config["_path"] = str(self.config_path)
        self.engine = Engine(self.config, state_file=str(self.state))
        self.backend = FakeBackend()
        self.engine.backend_for = lambda agent_id: self.backend

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def wake(self, word):
        self.engine.last_wake_route = ("", 0.0)
        return self.engine.handle_event({
            "type": "wake_word", "word": word,
            "request_id": "w" + uuid.uuid4().hex, "source": "wake-log"})

    def test_every_registered_word_switches_to_its_assistant(self):
        self.engine.set_wake(enabled=True)
        for word, agent in [("大肥鱼", "dsh"), ("巴蒂", "workbuddy"),
                            ("GPT", "codex"), ("Hermes", "hermes"),
                            ("小赫", "hermes")]:
            self.engine.exit(announce=False)
            result = self.wake(word)
            self.assertEqual(result["selected_agent"], agent, word)
            self.assertEqual(self.engine.selected, agent, word)
        # A wake is a switch, not a question: no backend is called.
        self.assertEqual(self.backend.answers, [])

    def test_the_factory_word_leaves_the_assistant_alone(self):
        self.engine.set_wake(enabled=True)
        self.engine.select("dsh", announce=False)
        result = self.wake("若琪|洛奇")
        self.assertEqual(result["selected_agent"], "dsh")
        self.assertEqual(self.engine.selected, "dsh")

    def test_nothing_routes_while_the_master_switch_is_off(self):
        self.wake("大肥鱼")
        self.assertIsNone(self.engine.selected)

    def test_a_disabled_row_does_not_route(self):
        self.engine.set_wake(enabled=True)
        self.engine.set_wake(agent="dsh", agent_enabled=False)
        self.wake("大肥鱼")
        self.assertIsNone(self.engine.selected)

    def test_the_question_after_a_wake_goes_to_that_assistant(self):
        self.engine.set_wake(enabled=True)
        self.wake("巴蒂")
        self.engine.handle_event({
            "type": "asr_final", "text": "帮我写个函数",
            "request_id": "r" + uuid.uuid4().hex, "source": "bus"})
        self.engine.jobs.join()
        self.assertEqual(self.backend.answers, ["帮我写个函数"])
        self.assertEqual(self.engine.selected, "workbuddy")

    def test_the_same_wake_twice_in_a_row_is_one_switch(self):
        self.engine.set_wake(enabled=True)
        self.engine.last_wake_route = ("", 0.0)
        first = self.engine.handle_event({
            "type": "wake_word", "word": "大肥鱼",
            "request_id": "w1", "source": "wake-log"})
        second = self.engine.handle_event({
            "type": "wake_word", "word": "大肥鱼",
            "request_id": "w2", "source": "wake-log"})
        self.assertNotIn("duplicate", first)
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(self.engine.selected, "dsh")


class WakeWatchdogTest(unittest.TestCase):
    """The self-heal must ask the device to start the bridge on reconnect."""

    def setUp(self):
        self.base = ["adb", "-s", "192.0.2.31:5555", "shell"]

    def test_health_check_interval_is_five_minutes(self):
        self.assertEqual(asr_watcher.BRIDGE_HEALTH_INTERVAL, 300.0)

    def test_ensure_bridge_issues_startservice(self):
        with mock.patch.object(asr_watcher.subprocess, "run") as run:
            run.return_value = mock.Mock(stderr="", stdout="")
            asr_watcher.ensure_bridge(self.base)
            cmd = run.call_args[0][0]
            self.assertEqual(cmd[0], "adb")
            self.assertTrue(cmd[-1].startswith("am startservice -n cn.rokid.bridge/.BridgeService"))

    def test_ensure_bridge_skips_start_when_service_alive(self):
        with mock.patch.object(asr_watcher.subprocess, "run") as run:
            run.return_value = mock.Mock(
                stderr="", stdout="USER PID PPID NAME\nu0_a81 4784 1918 cn.rokid.bridge\n")
            asr_watcher.ensure_bridge(self.base)
            self.assertEqual(run.call_count, 1)
            self.assertTrue(run.call_args[0][0][-1].startswith("ps cn.rokid.bridge"))

    def test_ensure_bridge_skips_when_watcher_stopping(self):
        with mock.patch.object(asr_watcher.subprocess, "run") as run:
            asr_watcher.ensure_bridge(self.base, lambda: False)
            self.assertFalse(run.called)

    def test_ensure_bridge_logs_adb_error_without_raising(self):
        with mock.patch.object(
                asr_watcher.subprocess, "run",
                side_effect=OSError("device not found")) as run:
            asr_watcher.ensure_bridge(self.base)  # must not raise
            self.assertTrue(run.called)

    def test_ensure_bridge_reports_start_error(self):
        with mock.patch.object(asr_watcher.subprocess, "run") as run:
            run.return_value = mock.Mock(
                stderr="Error: app is not installed", stdout="")
            asr_watcher.ensure_bridge(self.base)  # must not raise on error
            self.assertTrue(run.called)


class SpeechCountsTest(unittest.TestCase):
    """The per-assistant counter counts only the assistant's own replies.

    Switch notices and the user's own lines stay in the conversation and
    can be paged back, but they must not inflate the "N 句" counter.
    """
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.path = Path(self.tmp.name) / "config.json"
        self.config = config_module.create_config(self.path)
        self.config["_root"] = self.tmp.name
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.config["_path"] = str(self.path)
        self.engine = Engine(self.config, state_file=str(self.state))
        self.engine._proactive_stop.set()

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def test_only_agent_replies_are_counted(self):
        self.engine.log_transcript("system", "dsh", "已切换到 大肥鱼")
        self.engine.log_transcript("user", "dsh", "我说的话")
        self.engine.log_transcript("agent", "dsh", "它回的一句")
        self.engine.log_transcript("agent", "dsh", "它回的第二句")
        data = self.engine.threads()
        self.assertEqual(data["counts"]["dsh"], 4)          # paging total
        self.assertEqual(data["speech_counts"]["dsh"], 2)    # shown counter

    def test_speech_counts_survive_a_restart(self):
        self.engine.log_transcript("system", "neko", "已切换到 猫娘")
        self.engine.log_transcript("user", "neko", "我说的话")
        self.engine.log_transcript("agent", "neko", "喵")
        # A fresh engine reading the same files must rebuild the same count.
        again = Engine(self.config, state_file=str(self.state))
        again._proactive_stop.set()
        try:
            self.assertEqual(again.threads()["speech_counts"]["neko"], 1)
        finally:
            again.close()


if __name__ == "__main__":
    unittest.main()
