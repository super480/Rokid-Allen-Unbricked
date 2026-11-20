"""The HTTP layer: what the console can reach.

The console page itself is served without the token: a browser cannot
attach an Authorization header to a page request, so requiring one there
means the console can never be opened. The data APIs stay behind the
token, and the pictures the console shows are public like the page.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
import server as server_module  # noqa: E402
from engine import Engine  # noqa: E402


def _managed_node():
    """The node this machine has, if there is one.

    WorkBuddy manages node itself, and that copy is not on the PATH of a
    plain shell, so the well-known location is checked too. The console's
    script is only checked when a JavaScript engine can be found.
    """
    known = Path.home() / ".workbuddy" / "binaries" / "node" / "versions"
    if not known.is_dir():
        return None
    for version in sorted(known.iterdir(), reverse=True):
        candidate = version / "bin" / "node"
        if candidate.exists():
            return str(candidate)
    return None


class FakeBackend:
    """A backend that answers with a fixed line, without starting a CLI."""

    def __init__(self, settings=None, state_store=None):
        self.settings = settings or {}
        self.state_store = state_store
        self.model = ""

    def run(self, text, request_id, on_progress=None):
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


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config_module.create_config(Path(self.tmp.name) / "config.json")
        self.config["_root"] = self.tmp.name
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.engine = Engine(self.config, state_file=str(Path(self.tmp.name) / "s.json"))
        # A real assistant would start its CLI; the tests only care about
        # the HTTP layer, so the backend is replaced by a stand-in.
        self.engine.backend_for = lambda agent_id: FakeBackend()
        self.token = self.config["token"]
        handler = server_module.make_handler(self.engine, self.token)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        # The default poll interval of serve_forever is half a second, which
        # every test would pay again on shutdown.
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.engine.close()
        self.tmp.cleanup()

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path, token=None):
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(self.url(path), headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), response.headers

    def post(self, path, body, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(
            self.url(path), data=json.dumps(body).encode("utf-8"),
            headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    # -- the console page -----------------------------------------------------

    def test_the_console_script_is_valid_javascript(self):
        # One stray bracket in the page's script makes the whole script
        # fail to parse, and the console then shows an empty shell with no
        # explanation at all. The page is plain JavaScript, so its syntax is
        # checked here rather than only in a browser.
        import re
        status, body, _ = self.get("/console")
        page = body.decode("utf-8")
        blocks = re.findall(r"<script>(.*?)</script>", page, re.S)
        self.assertTrue(blocks, "the console has no script")
        script = blocks[-1]
        self.assertIn("boot", script)

        # The real check: let a JavaScript engine parse it. Node is used when
        # it is there; without it the brackets are counted, which still
        # catches the mistake that actually happened.
        node = shutil.which("node") or _managed_node()
        if node:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                             encoding="utf-8") as handle:
                handle.write(script)
                path = handle.name
            try:
                result = subprocess.run(
                    [node, "--check", path], capture_output=True, text=True,
                    timeout=30)
                self.assertEqual(result.returncode, 0,
                                 "the console script does not parse:\n"
                                 + result.stderr[:600])
            finally:
                os.unlink(path)
            return

        for opener, closer in (("(", ")"), ("[", "]"), ("{", "}")):
            self.assertEqual(script.count(opener), script.count(closer),
                             "unbalanced %s%s in the console script" % (opener, closer))

    def test_the_console_recovers_from_a_stale_token(self):
        # The page has to be able to get a fresh token by itself, otherwise a
        # token left over from an older config leaves the console empty.
        status, body, _ = self.get("/console")
        page = body.decode("utf-8")
        self.assertIn("/console-token", page)
        self.assertIn("refreshToken", page)

    def test_the_console_page_loads_without_a_token(self):
        # This is the fix that makes the console reachable at all: a
        # browser sends no Authorization header with a page request.
        status, body, headers = self.get("/console")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"input", body)

    def test_the_root_serves_the_console_too(self):
        status, _, _ = self.get("/")
        self.assertEqual(status, 200)

    # -- the data APIs stay behind the token -----------------------------------

    def test_the_status_needs_a_token(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/status")
        self.assertEqual(caught.exception.code, 401)

    def test_the_status_works_with_a_token(self):
        status, body, _ = self.get("/api/status", self.token)
        self.assertEqual(status, 200)
        self.assertIn("agents", json.loads(body.decode("utf-8")))

    def test_the_transcript_needs_a_token(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/transcript")
        self.assertEqual(caught.exception.code, 401)

    # -- the pictures ---------------------------------------------------------

    def test_an_avatar_is_served(self):
        status, body, headers = self.get("/avatars/hermes.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_an_avatar_state_is_served(self):
        status, body, _ = self.get("/avatars/hermes-idle.png")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_an_unknown_avatar_is_not_found(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/avatars/nobody.png")
        self.assertEqual(caught.exception.code, 404)

    def test_a_path_cannot_walk_out_of_the_folder(self):
        for path in ("/avatars/../server.py", "/avatars/%2e%2e%2fserver.py",
                     "/avatars/sub/dir.png"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(path)
            self.assertEqual(caught.exception.code, 404)

    def test_an_unknown_state_is_not_found(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/avatars/hermes-dancing.png")
        self.assertEqual(caught.exception.code, 404)

    # -- the new endpoints ----------------------------------------------------

    def test_the_model_list_is_served(self):
        status, body, _ = self.get("/api/model?agent=hermes", self.token)
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["agent"], "hermes")
        self.assertIsInstance(data["models"], list)

    def test_the_direct_status_is_served(self):
        status, body, _ = self.get("/api/direct", self.token)
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn(data["mode"], ("off", "hybrid", "full"))

    def test_a_model_can_be_changed(self):
        status, body = self.post("/api/model",
                                  {"agent": "hermes", "model": "some-model"}, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["model"], "some-model")

    def test_a_model_without_an_assistant_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/model", {"model": "x"}, self.token)
        self.assertEqual(caught.exception.code, 400)

    def test_the_direct_settings_can_be_saved(self):
        status, body = self.post("/api/direct", {
            "mode": "full", "source": "custom",
            "custom": {"base_url": "https://example.com/v1", "model": "m",
                       "api_key": "sk-secret"}}, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "full")
        self.assertEqual(body["direct"]["custom"]["model"], "m")
        # The key itself is never sent back.
        self.assertNotIn("sk-secret", json.dumps(body))

    def test_a_bare_mode_still_works(self):
        status, body = self.post("/api/direct", {"mode": "hybrid"}, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "hybrid")

    # -- a device that just connected ----------------------------------------

    def test_a_fresh_device_gets_the_current_display_state(self):
        # The overlay on the speaker is built from a display command. A
        # device that was restarted asks for the newest command and never
        # sees the one that built the overlay before, so the screen would
        # stay empty until the next turn.
        self.post("/api/agent", {"agent": "hermes", "quiet": True}, self.token)
        _, body, _ = self.get("/api/commands?after=-1", self.token)
        commands = json.loads(body.decode("utf-8"))["commands"]
        displays = [c for c in commands if c.get("type") == "display"]
        self.assertTrue(displays, "a fresh device got no display command")
        self.assertEqual(displays[-1].get("agent"), "hermes")

    def test_the_resync_does_not_repeat_old_speech(self):
        # The queue holds every command of the session. Replaying it would
        # make the speaker read out old answers again, so only the state
        # is sent again.
        self.post("/api/agent", {"agent": "hermes", "quiet": True}, self.token)
        self.engine.emit("speak", text="不该被重复的旧播报")
        _, body, _ = self.get("/api/commands?after=-1", self.token)
        commands = json.loads(body.decode("utf-8"))["commands"]
        spoken = [c for c in commands if c.get("type") == "speak"]
        self.assertEqual(spoken, [])

    def test_the_older_commands_are_still_delivered_once(self):
        # A device that is already connected asks for everything after the
        # last command it saw; that behaviour must not change.
        self.post("/api/agent", {"agent": "hermes", "quiet": True}, self.token)
        seq = self.engine.status()["last_seq"]
        self.engine.emit("state", state="thinking")
        _, body, _ = self.get("/api/commands?after=%d" % seq, self.token)
        commands = json.loads(body.decode("utf-8"))["commands"]
        self.assertEqual([c.get("type") for c in commands], ["state"])

    def test_a_typed_line_can_be_sent(self):
        status, body = self.post("/api/chat",
                                  {"text": "你好", "agent": "hermes"}, self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body["accepted"])

    def test_the_transcript_cursor_returns_only_what_is_new(self):
        self.engine.log_transcript("user", "hermes", "一")
        _, body, _ = self.get("/api/transcript?after=0", self.token)
        first = json.loads(body.decode("utf-8"))
        self.assertEqual(len(first["items"]), 1)
        self.engine.log_transcript("agent", "hermes", "二")
        _, body, _ = self.get("/api/transcript?after=%d" % first["last_seq"], self.token)
        second = json.loads(body.decode("utf-8"))
        self.assertEqual(len(second["items"]), 1)
        self.assertEqual(second["items"][0]["message"], "二")

    def test_the_threads_are_served_per_assistant(self):
        self.engine.log_transcript("user", "hermes", "问小赫")
        self.engine.log_transcript("user", "dsh", "问大肥鱼")
        status, body, _ = self.get("/api/threads", self.token)
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("threads", data)
        self.assertEqual([e["message"] for e in data["threads"]["hermes"]], ["问小赫"])
        self.assertEqual([e["message"] for e in data["threads"]["dsh"]], ["问大肥鱼"])

    def test_the_threads_need_a_token(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/threads")
        self.assertEqual(caught.exception.code, 401)

    def test_the_status_carries_the_avatar_folder(self):
        _, body, _ = self.get("/api/status", self.token)
        agents = json.loads(body.decode("utf-8"))["agents"]
        # The DeepSeek assistant is called "dsh" but its pictures live in
        # the "deepseek" folder.
        self.assertEqual(agents["dsh"]["design"], "deepseek")
        self.assertEqual(agents["hermes"]["design"], "hermes")

    def test_the_status_says_which_characters_have_a_picture(self):
        _, body, _ = self.get("/api/status", self.token)
        art = json.loads(body.decode("utf-8"))["art"]
        # The designed characters are in the repository; GPT has no
        # artwork yet, so the console falls back to a letter for it.
        self.assertIn("hermes", art)
        self.assertIn("deepseek", art)
        self.assertNotIn("gpt", art)

    # -- the token is remembered, so the short address keeps working ------

    def test_the_page_remembers_the_token_beyond_the_tab(self):
        # localStorage, not sessionStorage: the address has to keep working
        # after the tab is closed and the browser restarted, otherwise the
        # token has to be pasted every single time.
        status, body, _ = self.get("/console")
        self.assertEqual(status, 200)
        page = body.decode("utf-8")
        self.assertIn('localStorage.setItem("rokid_token"', page)
        self.assertNotIn('sessionStorage.setItem("rokid_token"', page)

    def test_a_stale_token_is_dropped_and_explained(self):
        # A token that no longer works must not leave the page stuck on
        # 401. It is forgotten, and the page says what to do about it.
        status, body, _ = self.get("/console")
        page = body.decode("utf-8")
        self.assertIn("forgetToken", page)
        self.assertIn("重新打开控制台", page)

    # -- the WorkBuddy authorisation endpoint -------------------------------

    def test_the_agent_auth_status_is_served(self):
        status, body, _ = self.get("/api/agent-auth?agent=workbuddy", self.token)
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["agent"], "workbuddy")
        self.assertFalse(data["configured"])

    def test_the_agent_auth_can_be_saved(self):
        status, body = self.post("/api/agent-auth", {
            "agent": "workbuddy", "base_url": "https://x.test/v1",
            "api_key": "tok", "model": "m1"}, self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body["configured"])
        # The token is never sent back.
        self.assertNotIn("tok", json.dumps(body))
        # The next read reflects the change.
        _, after, _ = self.get("/api/agent-auth?agent=workbuddy", self.token)
        self.assertTrue(json.loads(after.decode("utf-8"))["configured"])

    def test_the_agent_auth_rejects_an_unknown_agent(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/agent-auth", {"agent": "nope", "api_key": "x"}, self.token)
        self.assertEqual(caught.exception.code, 400)

    # -- the browser sign-in endpoints --------------------------------------

    def test_starting_a_sign_in_answers_the_login_url(self):
        def fake_start(agent_id, region=None, base=None):
            return {"login_id": "wbo_1_abc", "auth_url": "https://x/login?state=s",
                    "region": region or "cn", "expires_in": 600}

        self.engine.agent_login_start = fake_start
        status, body, _ = self.get(
            "/api/agent-auth/start?agent=workbuddy", self.token)
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["login_id"], "wbo_1_abc")
        self.assertEqual(data["auth_url"], "https://x/login?state=s")

    def test_starting_a_sign_in_needs_a_known_agent(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/agent-auth/start?agent=nope", self.token)
        self.assertEqual(caught.exception.code, 400)

    def test_polling_reports_that_the_login_is_not_finished(self):
        def fake_poll(agent_id, login_id):
            return {"done": False}

        self.engine.agent_login_poll = fake_poll
        status, body = self.post("/api/agent-auth/poll",
                                  {"agent": "workbuddy", "login_id": "x"},
                                  self.token)
        self.assertEqual(status, 200)
        self.assertFalse(body["done"])

    def test_polling_stores_the_credential_when_the_login_finishes(self):
        def fake_poll(agent_id, login_id):
            return {"done": True, "account": {"nickname": "test-user",
                                             "region": "cn"}}

        self.engine.agent_login_poll = fake_poll
        status, body = self.post("/api/agent-auth/poll",
                                  {"agent": "workbuddy", "login_id": "x"},
                                  self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body["done"])
        self.assertEqual(body["account"]["nickname"], "test-user")

    # -- the console's token ------------------------------------------------

    def test_this_computer_can_ask_for_the_token(self):
        # The plain address carries no token, so the page has to be able to
        # learn it. Only a request from this machine is answered.
        status, body, _ = self.get("/console-token", None)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["token"], self.token)

    def test_the_console_asks_for_the_token_when_it_has_none(self):
        status, body, _ = self.get("/console", None)
        page = body.decode("utf-8")
        self.assertIn("/console-token", page)
        self.assertIn("fetchToken", page)


if __name__ == "__main__":
    unittest.main()
