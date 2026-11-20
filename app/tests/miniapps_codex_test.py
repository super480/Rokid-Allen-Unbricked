"""Codex read-only RPC: protocol order, field normalisation, bad values."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from miniapps.codex_usage import (  # noqa: E402
    CodexError, CodexRpcClient, CodexUsageProvider,
    normalise_account, normalise_rate_limits, normalise_usage,
    resolve_executable,
)

FAKE_CLI = '''#!/usr/bin/env python3
import json, os, sys, time
log = os.environ.get("FAKE_LOG", "")
def note(method):
    if log:
        with open(log, "a") as f:
            f.write(method + "\\n")
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method", "")
    note(method)
    rid = msg.get("id")
    if rid is None:
        continue
    if method == "initialize":
        if os.environ.get("FAKE_NO_INIT"):
            time.sleep(5)
            continue
        print(json.dumps({"method": "progress", "params": {}}), flush=True)
        print(json.dumps({"id": rid, "result": {"ok": True}}), flush=True)
        continue
    if os.environ.get("FAKE_SILENT") and method != "account/read":
        continue
    result = {
        "account/read": {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": False},
        "account/rateLimits/read": {"rateLimitsByLimitId": {"codex": {
            "limitId": "codex",
            "primary": {"usedPercent": 32, "windowDurationMins": 300, "resetsAt": 1794691630},
            "secondary": {"usedPercent": 58, "windowDurationMins": 10080, "resetsAt": 1795248418}}}},
        "account/usage/read": {"summary": {"lifetimeTokens": 123456789},
                               "dailyUsageBuckets": [{"startDate": "2026-11-09", "tokens": 42000}]},
    }.get(method, {"error": {"code": -32601}})
    print(json.dumps({"id": rid, "result": result}), flush=True)
'''


def bucket(primary=None, secondary=None, limit_id="codex"):
    return {"rateLimitsByLimitId": {"codex": {
        "limitId": limit_id,
        "primary": primary or {},
        "secondary": secondary or {},
    }}}


def window(percent, minutes, reset=1794691630):
    return {"usedPercent": percent, "windowDurationMins": minutes,
            "resetsAt": reset}


class RateLimitsTest(unittest.TestCase):
    def test_codex_bucket_is_chosen_by_id_not_by_order(self):
        raw = {"rateLimitsByLimitId": {
            "other": {"limitId": "other", "primary": window(99, 300)},
            "codex": {"limitId": "codex",
                      "primary": window(32, 300),
                      "secondary": window(58, 10080)}}}
        result = normalise_rate_limits(raw)
        self.assertEqual([w["label"] for w in result["windows"]], ["7d", "5h"])
        self.assertEqual([w["used_percent"] for w in result["windows"]], [58, 32])
        self.assertEqual(result["source"], "by_limit_id")

    def test_primary_secondary_order_does_not_decide_the_rings(self):
        result = normalise_rate_limits(bucket(window(58, 10080), window(32, 300)))
        self.assertEqual(result["windows"][0]["label"], "7d")
        self.assertEqual(result["windows"][0]["slot"], "primary")

    def test_missing_map_uses_compatible_single_bucket(self):
        result = normalise_rate_limits({"rateLimits": {
            "limitId": "codex", "primary": window(10, 300)}})
        self.assertEqual(result["source"], "compat")
        self.assertEqual(result["windows"][0]["value"], 10)

    def test_only_other_buckets_is_unsupported_not_a_guess(self):
        result = normalise_rate_limits({"rateLimitsByLimitId": {
            "other": {"limitId": "other", "primary": window(50, 300)}}})
        self.assertEqual(result["windows"], [])
        self.assertEqual(result["error_code"], "unsupported_window")

    def test_non_standard_window_is_not_forced_into_5h_or_7d(self):
        result = normalise_rate_limits(bucket(window(40, 1440)))
        self.assertEqual(result["windows"], [])
        self.assertEqual(len(result["unrecognized"]), 1)
        self.assertEqual(result["error_code"], "unsupported_window")

    def test_zero_hundred_over_hundred_keep_their_value(self):
        for percent in (0, 100, 137.5):
            result = normalise_rate_limits(bucket(window(percent, 300)))
            self.assertEqual(result["windows"][0]["used_percent"], percent)
        self.assertTrue(normalise_rate_limits(
            bucket(window(137.5, 300)))["windows"][0]["over_limit"])

    def test_bad_fields_are_not_turned_into_zero(self):
        for bad in (-1, "32", None, True, float("nan"), float("inf")):
            result = normalise_rate_limits(bucket(window(bad, 300)))
            self.assertEqual(result["windows"], [])
            self.assertTrue(result["field_error"])

    def test_seconds_convert_to_milliseconds(self):
        result = normalise_rate_limits(bucket(window(32, 300, 1794691630)))
        self.assertEqual(result["windows"][0]["reset_at_ms"], 1794691630000)

    def test_missing_duration_or_reset_is_tolerated(self):
        result = normalise_rate_limits(bucket(
            {"usedPercent": 12, "windowDurationMins": 300},
            {"usedPercent": 40, "windowDurationMins": 10080}))
        self.assertEqual(len(result["windows"]), 2)
        self.assertIsNone(result["windows"][0]["reset_at_ms"])


class UsageTest(unittest.TestCase):
    def test_lifetime_is_a_decimal_string_and_large_ints_keep_precision(self):
        result = normalise_usage({"summary": {"lifetimeTokens": 9007199254740993}})
        self.assertEqual(result["lifetime_tokens"], "9007199254740993")

    def test_missing_today_is_not_zero(self):
        result = normalise_usage({
            "summary": {"lifetimeTokens": 10},
            "dailyUsageBuckets": [{"startDate": "2026-11-09", "tokens": 42}]})
        self.assertIsNone(result["today_tokens"])
        self.assertEqual(result["latest_bucket_date"], "2026-11-09")

    def test_today_present_is_reported_without_claiming_timezone(self):
        from datetime import date
        today = date.today().isoformat()
        result = normalise_usage({
            "summary": {"lifetimeTokens": 10},
            "dailyUsageBuckets": [{"startDate": today, "tokens": 7}]})
        self.assertEqual(result["today_tokens"], "7")

    def test_malformed_bucket_is_flagged_not_dropped_silently(self):
        result = normalise_usage({"summary": {"lifetimeTokens": "oops"},
                                  "dailyUsageBuckets": [{"startDate": 5}]})
        self.assertTrue(result["field_error"])
        self.assertIsNone(result["lifetime_tokens"])

    def test_account_keeps_only_the_login_type(self):
        self.assertEqual(normalise_account(
            {"account": {"type": "chatgpt", "email": "secret@example.com"}}),
            {"type": "chatgpt", "requires_auth": False})
        self.assertEqual(normalise_account({"requiresOpenaiAuth": True})["type"], "")


class RpcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cli = Path(self.tmp.name) / "fake-codex"
        self.cli.write_text(FAKE_CLI)
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)
        self.log = Path(self.tmp.name) / "calls.log"
        self.env = dict(os.environ, FAKE_LOG=str(self.log))

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, **kwargs):
        client = CodexRpcClient(str(self.cli), **kwargs)
        client.env = self.env
        return client

    def test_initialize_then_initialized_then_reads(self):
        # Patch Popen so the fake child inherits FAKE_LOG.
        original = __import__("subprocess").Popen

        def patched(*args, **kwargs):
            kwargs["env"] = self.env
            return original(*args, **kwargs)

        import subprocess
        subprocess.Popen = patched
        try:
            results = self.client().call([
                ("account/read", {"refreshToken": False}),
                ("account/rateLimits/read", {}),
            ])
        finally:
            subprocess.Popen = original
        self.assertEqual(self.log.read_text().split(), [
            "initialize", "initialized", "account/read",
            "account/rateLimits/read"])
        self.assertEqual(len(results), 2)
        self.assertIn("rateLimitsByLimitId", results[1])

    def test_notifications_do_not_become_responses(self):
        import subprocess
        original = subprocess.Popen

        def patched(*args, **kwargs):
            kwargs["env"] = self.env
            return original(*args, **kwargs)

        subprocess.Popen = patched
        try:
            results = self.client().call([("account/usage/read", {})])
        finally:
            subprocess.Popen = original
        self.assertEqual(results[0]["summary"]["lifetimeTokens"], 123456789)

    def test_timeout_terminates_only_our_child(self):
        import subprocess
        original = subprocess.Popen
        env = dict(self.env, FAKE_NO_INIT="1")

        def patched(args, **kwargs):
            kwargs["env"] = env
            return original(args, **kwargs)

        subprocess.Popen = patched
        try:
            client = CodexRpcClient(str(self.cli), init_timeout=0.3,
                                    total_timeout=1.0)
            with self.assertRaises(CodexError) as caught:
                client.call([("account/read", {})])
            self.assertEqual(caught.exception.code, "timeout")
        finally:
            subprocess.Popen = original

    def test_missing_executable_is_a_clean_error(self):
        client = CodexRpcClient(str(Path(self.tmp.name) / "nope"))
        # resolve_executable falls back to PATH/default, so force it empty.
        client.executable = ""
        with self.assertRaises(CodexError) as caught:
            client.call([("account/read", {})])
        self.assertEqual(caught.exception.code, "unavailable")

    def test_provider_rejects_non_chatgpt_login(self):
        class Client:
            def call(self, requests):
                return [{"account": {"type": "apiKey"}}]

        with self.assertRaises(CodexError) as caught:
            CodexUsageProvider(Client()).read("quota")
        self.assertEqual(caught.exception.code, "unsupported_auth")

    def test_resolve_executable_prefers_config(self):
        self.assertEqual(resolve_executable(str(self.cli)), str(self.cli))


if __name__ == "__main__":
    unittest.main()
