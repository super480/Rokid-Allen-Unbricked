"""WorkBuddy backend: browser sign-in plus the streaming gateway."""
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
from backends.base import BackendError  # noqa: E402
from backends.workbuddy import WorkBuddyBackend  # noqa: E402
from backends.workbuddy_oauth import WorkBuddyOAuth  # noqa: E402
from engine import Engine  # noqa: E402

import backends.workbuddy as workbuddy_module  # noqa: E402


class FakeResponse:
    """A urlopen answer that can also be iterated line by line."""

    def __init__(self, payload, url=""):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.payload = payload
        self.url = url

    def read(self):
        return self.payload

    def __iter__(self):
        for line in self.payload.split(b"\n"):
            yield line + b"\n"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def sse(*pieces):
    """Build a server-sent-events body out of answer fragments."""
    lines = []
    for piece in pieces:
        lines.append("data: " + json.dumps(
            {"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False))
    lines.append("data: [DONE]")
    return "\n".join(lines)


class WorkBuddyBackendTest(unittest.TestCase):
    def make(self, auth=None, state_store=None, **settings):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        if auth is not None:
            with open(os.path.join(root, "workbuddy-auth.json"),
                      "w", encoding="utf-8") as handle:
                json.dump(auth, handle, ensure_ascii=False)
        settings.setdefault("_root", root)
        return WorkBuddyBackend(settings, state_store=state_store if state_store is not None else {})

    def tearDown(self):
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    # -- authorisation file ------------------------------------------------

    def test_the_auth_file_overrides_the_config_section(self):
        backend = self.make(
            auth={"base_url": "https://auth.test", "access_token": "tok",
                  "model": "m1"},
            base_url="https://config.test", model="m0")
        self.assertEqual(backend.base_url, "https://auth.test")
        self.assertEqual(backend.api_key, "tok")
        self.assertEqual(backend.model, "m1")

    def test_the_config_section_is_used_when_no_auth_file(self):
        backend = self.make(base_url="https://config.test", model="m0")
        self.assertEqual(backend.base_url, "https://config.test")
        # Without a signed-in account there is no token to talk with.
        self.assertEqual(backend.api_key, "")

    # -- health ------------------------------------------------------------

    def test_health_reports_what_is_missing(self):
        ok, detail = self.make(auth={"model": "m"}).health()
        self.assertFalse(ok)
        self.assertIn("登录", detail)

        # A signed-in account is ready; the model comes from the catalogue.
        ok, detail = self.make(auth={"access_token": "k"}).health()
        self.assertTrue(ok)
        self.assertIn("hy3", detail)

    def test_the_endpoint_is_built_from_the_base_url(self):
        self.assertEqual(
            self.make(auth={"base_url": "https://x.test", "access_token": "k",
                           "model": "m"}).endpoint(),
            "https://x.test/v2/chat/completions")

    def test_the_default_gateway_is_used_when_no_base_is_stored(self):
        backend = self.make(auth={"access_token": "k", "model": "m"})
        self.assertEqual(backend.endpoint(),
                         "https://copilot.tencent.com/v2/chat/completions")

    # -- running -----------------------------------------------------------

    def test_the_request_carries_the_token_and_the_model(self):
        backend = self.make(auth={"base_url": "https://x.test",
                                  "access_token": "tok", "model": "m1"})
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            return FakeResponse(sse("回答 "))

        original = workbuddy_module.urllib.request.urlopen
        workbuddy_module.urllib.request.urlopen = fake_urlopen
        try:
            answer = backend.run("你好", "r1")
        finally:
            workbuddy_module.urllib.request.urlopen = original
        self.assertEqual(answer, "回答")
        self.assertEqual(captured["request"].get_header("Authorization"), "Bearer tok")
        body = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(body["model"], "m1")
        self.assertEqual(body["messages"][-1]["content"], "你好")
        # The gateway refuses a non-stream request, so the stream flag is set.
        self.assertTrue(body["stream"])

    def test_the_reasoning_of_the_model_is_not_spoken(self):
        backend = self.make(auth={"base_url": "https://x.test",
                                  "access_token": "tok", "model": "m1"})
        events = "\n".join([
            "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "想一下"}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"content": "答案"}}]}),
            "data: [DONE]",
        ])

        original = workbuddy_module.urllib.request.urlopen
        workbuddy_module.urllib.request.urlopen = lambda request, timeout=None: FakeResponse(events)
        try:
            answer = backend.run("你好", "r1")
        finally:
            workbuddy_module.urllib.request.urlopen = original
        self.assertEqual(answer, "答案")

    def test_the_conversation_is_kept_in_workbuddys_own_bucket(self):
        store = {}
        backend = self.make(auth={"base_url": "https://x.test",
                                  "access_token": "tok", "model": "m1"},
                            state_store=store)
        original = workbuddy_module.urllib.request.urlopen
        answers = ["第一句", "第二句"]

        def fake_urlopen(request, timeout=None):
            return FakeResponse(sse(answers.pop(0)))

        workbuddy_module.urllib.request.urlopen = fake_urlopen
        try:
            backend.run("第一个问题", "r1")
            backend.run("第二个问题", "r2")
        finally:
            workbuddy_module.urllib.request.urlopen = original
        messages = store["workbuddy_messages"]
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[1]["role"], "assistant")

    def test_a_backend_without_a_login_refuses_to_run(self):
        backend = self.make()  # no auth file, empty config section
        with self.assertRaises(BackendError):
            backend.run("你好", "r1")

    def test_an_expired_token_is_refreshed_before_the_request(self):
        backend = self.make(auth={"base_url": "https://x.test",
                                  "access_token": "old", "refresh_token": "r",
                                  "model": "m1", "expires_at": 1})
        seen = []

        def fake_refresh(self):
            seen.append("refresh")
            self.save_credential({"access_token": "fresh"})
            return True

        def fake_urlopen(request, timeout=None):
            seen.append(request.get_header("Authorization"))
            return FakeResponse(sse("好"))

        original = workbuddy_module.urllib.request.urlopen
        workbuddy_module.urllib.request.urlopen = fake_urlopen
        workbuddy_module.WorkBuddyBackend._refresh = fake_refresh
        try:
            answer = backend.run("你好", "r1")
        finally:
            workbuddy_module.urllib.request.urlopen = original
            del workbuddy_module.WorkBuddyBackend._refresh
        self.assertEqual(answer, "好")
        # The expired token was renewed before the request was made.
        self.assertEqual(seen[0], "refresh")
        self.assertIn("Bearer fresh", seen)

    def test_a_refused_token_is_refreshed_and_the_turn_is_tried_again(self):
        backend = self.make(auth={"base_url": "https://x.test",
                                  "access_token": "old", "refresh_token": "r",
                                  "model": "m1"})
        seen = []

        def fake_refresh(self):
            seen.append("refresh")
            self.save_credential({"access_token": "fresh"})
            return True

        def fake_urlopen(request, timeout=None):
            seen.append(request.get_header("Authorization"))
            if request.get_header("Authorization") == "Bearer old":
                raise urllib.error.HTTPError(request.full_url, 401, "no", {},
                                             None)
            return FakeResponse(sse("好"))

        original = workbuddy_module.urllib.request.urlopen
        workbuddy_module.urllib.request.urlopen = fake_urlopen
        workbuddy_module.WorkBuddyBackend._refresh = fake_refresh
        try:
            answer = backend.run("你好", "r1")
        finally:
            workbuddy_module.urllib.request.urlopen = original
            del workbuddy_module.WorkBuddyBackend._refresh
        self.assertEqual(answer, "好")
        self.assertIn("refresh", seen)
        self.assertIn("Bearer fresh", seen)

    # -- models ------------------------------------------------------------

    def test_models_offers_the_chosen_one_first(self):
        backend = self.make(auth={"base_url": "https://x.test",
                                  "access_token": "tok", "model": "hy3"})
        names = backend.models()
        self.assertEqual(names[0], "hy3")
        self.assertIn("hy4-preview", names)

    def test_switching_the_model_keeps_it_for_the_next_turn(self):
        backend = self.make(auth={"base_url": "https://x.test",
                                  "access_token": "tok", "model": "hy3"})
        self.assertEqual(backend.switch_model("glm-5.3"), "glm-5.3")
        self.assertEqual(backend.current_model(), "glm-5.3")


class WorkBuddyOAuthTest(unittest.TestCase):
    """The three-step browser sign-in, the way the old app did it."""

    def make(self, answers):
        self.calls = []
        answers = list(answers)

        def fetch(url, body=None, headers=None):
            self.calls.append((url, body))
            value = answers.pop(0) if answers else None
            return value

        return WorkBuddyOAuth(fetch=fetch)

    def test_starting_a_login_asks_the_gateway_for_a_state(self):
        oauth = self.make([{"code": 0, "data": {
            "state": "st-1",
            "authUrl": "https://www.codebuddy.cn/login?platform=workbuddy&state=st-1"}}])
        result = oauth.start()
        self.assertNotIn("error", result)
        self.assertEqual(result["auth_url"],
                         "https://www.codebuddy.cn/login?platform=workbuddy&state=st-1")
        url, body = self.calls[0]
        self.assertTrue(url.endswith("/v2/plugin/auth/state?platform=workbuddy"))
        self.assertEqual(body, {})

    def test_a_refusal_is_reported_rather_than_thrown(self):
        oauth = self.make([{"code": 500, "msg": "坏了"}])
        result = oauth.start()
        self.assertIn("error", result)

    def test_polling_finishes_when_the_token_arrives(self):
        oauth = self.make([
            {"code": 0, "data": {"state": "st-1", "authUrl": "https://x/login"}},
            {"code": 0, "data": {"accessToken": "tok", "refreshToken": "ref",
                                 "domain": "www.workbuddy.cn"}},
            {"code": 0, "data": {"uid": "u1", "nickname": "test-user", "uin": "1"}},
        ])
        started = oauth.start()
        result = oauth.poll(started["login_id"])
        self.assertTrue(result["done"])
        self.assertEqual(result["account"]["access_token"], "tok")
        self.assertEqual(result["account"]["refresh_token"], "ref")
        self.assertEqual(result["account"]["nickname"], "test-user")
        self.assertEqual(result["account"]["region"], "cn")

    def test_polling_says_not_done_while_the_user_is_still_logging_in(self):
        oauth = self.make([
            {"code": 0, "data": {"state": "st-1", "authUrl": "https://x/login"}},
            {"code": 0, "data": {}},
        ])
        started = oauth.start()
        result = oauth.poll(started["login_id"])
        self.assertFalse(result["done"])
        self.assertNotIn("error", result)

    def test_polling_an_unknown_login_fails_cleanly(self):
        oauth = self.make([])
        result = oauth.poll("nope")
        self.assertTrue(result["done"])
        self.assertIn("error", result)

    def test_the_domestic_and_international_gates_are_separate(self):
        self.assertEqual(WorkBuddyOAuth().base, "https://www.codebuddy.cn")
        self.assertEqual(WorkBuddyOAuth(region="global").base,
                         "https://www.workbuddy.ai")
        # A pinned base wins, because a token issued at one brand domain is
        # refused by the other.
        self.assertEqual(
            WorkBuddyOAuth(region="global", base="https://www.codebuddy.ai/").base,
            "https://www.codebuddy.ai")


class WorkBuddyEngineTest(unittest.TestCase):
    """The authorisation lives in a dedicated file, not the shared config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config_module.create_config(
            Path(self.tmp.name) / "config.json")
        self.config["_root"] = self.tmp.name
        self.engine = Engine(self.config, state_file=str(Path(self.tmp.name) / "s.json"))

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def test_the_status_reports_a_connection_agent_and_its_auth_state(self):
        status = self.engine.status()
        agent = status["agents"]["workbuddy"]
        self.assertTrue(agent["connection"])
        self.assertFalse(agent["auth"]["configured"])  # not signed in yet

    def test_a_signed_in_account_is_reported_without_its_token(self):
        self.engine.set_agent_auth("workbuddy", {
            "access_token": "tok", "nickname": "test-user"})
        status = self.engine.agent_auth_status("workbuddy")
        self.assertTrue(status["configured"])
        self.assertEqual(status["nickname"], "test-user")
        self.assertNotIn("tok", json.dumps(status))

    def test_the_browser_sign_in_stores_the_credential(self):
        answers = [
            {"code": 0, "data": {"state": "st-1", "authUrl": "https://x/login"}},
            {"code": 0, "data": {"accessToken": "tok", "refreshToken": "ref"}},
            {"code": 0, "data": {"uid": "u1", "nickname": "test-user"}},
        ]
        oauth = self.engine._oauth_for("workbuddy")
        oauth._fetch = lambda url, body=None, headers=None: answers.pop(0)

        started = self.engine.agent_login_start("workbuddy")
        self.assertIn("auth_url", started)
        result = self.engine.agent_login_poll("workbuddy", started["login_id"])
        self.assertTrue(result["done"])
        self.assertEqual(result["account"]["nickname"], "test-user")

        # The token is written to its own file, not echoed back.
        path = self.engine.auth_file_path("workbuddy")
        with open(path, "r", encoding="utf-8") as handle:
            data = json.loads(handle.read())
        self.assertEqual(data["access_token"], "tok")
        self.assertEqual(data["refresh_token"], "ref")
        self.assertEqual(self.engine.agent_auth_status("workbuddy")["configured"], True)

    def test_set_agent_model_for_a_connection_agent_goes_to_the_auth_file(self):
        self.engine.set_agent_auth("workbuddy", {"access_token": "tok"})
        self.engine.set_agent_model("workbuddy", "hy3")
        data = self.engine.agent_auth("workbuddy")
        self.assertEqual(data["model"], "hy3")

    def test_unknown_agents_are_rejected_for_auth(self):
        with self.assertRaises(ValueError):
            self.engine.set_agent_auth("hermes", {"access_token": "x"})


if __name__ == "__main__":
    unittest.main()
