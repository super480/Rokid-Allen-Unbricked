"""QQ 音乐卡片后端：账号走 N.E.K.O. 同一套 Cookie 上游（platform=qqmusic）。

和网易云是同一套机制，只是上游 platform 不同；QQ 的 Cookie 没有 MUSIC_U
字段，所以登录时不强制要求某个特定字段。这里只测纯逻辑，不连真实 N.E.K.O.。
"""
import json
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from netease import QQMusicAuth, NeteaseError  # noqa: E402
import music as music_module  # noqa: E402


class QQAuthTest(unittest.TestCase):
    def test_platform_is_qqmusic(self):
        self.assertEqual(QQMusicAuth.PLATFORM, "qqmusic")

    def test_subclass_of_netease(self):
        self.assertTrue(issubclass(QQMusicAuth, __import__(
            "netease", fromlist=["NeteaseAuth"]).NeteaseAuth))

    def test_normalize_accepts_non_music_u_cookie(self):
        # QQ 的 Cookie 没有 MUSIC_U 字段，不能像网易云那样强制要求。
        value = QQMusicAuth.normalize("uin=12345; skey=abc;")
        self.assertIn("uin=12345", value)

    def test_normalize_rejects_empty(self):
        with self.assertRaises(NeteaseError):
            QQMusicAuth.normalize("")

    def test_normalize_rejects_newline(self):
        with self.assertRaises(NeteaseError):
            QQMusicAuth.normalize("a=1\nb=2")

    def test_normalize_rejects_too_long(self):
        with self.assertRaises(NeteaseError):
            QQMusicAuth.normalize("x" * 9000)


class QQProviderTest(unittest.TestCase):
    def _provider(self):
        return music_module.MusicProvider(
            {}, local_root=Path("runtime/music"), runtime_root=Path("runtime"))

    def test_qq_status_delegates_to_auth(self):
        p = self._provider()
        with mock.patch.object(
                p.qqmusic, "status",
                return_value={"online": True, "logged_in": False}) as m:
            self.assertEqual(
                p.qqmusic_status(),
                {"online": True, "logged_in": False})
            m.assert_called_once()

    def test_qq_login_and_logout_delegate(self):
        p = self._provider()
        with mock.patch.object(p.qqmusic, "login",
                               return_value={"ok": True}) as m:
            self.assertEqual(p.qqmusic_login("qq-cookie"), {"ok": True})
            m.assert_called_once_with("qq-cookie")
        with mock.patch.object(p.qqmusic, "logout",
                               return_value={"ok": True}) as m:
            self.assertEqual(p.qqmusic_logout(), {"ok": True})
            m.assert_called_once()

    def test_qq_search_parses_payload(self):
        p = self._provider()
        payload = json.dumps({
            "success": True,
            "data": [{"name": "七里香", "artist": "周杰伦",
                      "url": "/api/music/play/qqmusic/1", "cover": "",
                      "duration": 240}],
        }).encode("utf-8")
        with mock.patch.object(p, "_fetch", return_value=payload):
            items = p.qqmusic_search("七里香")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "七里香")
        self.assertEqual(items[0]["artist"], "周杰伦")
        self.assertEqual(items[0]["url"], "/api/music/play/qqmusic/1")
        self.assertEqual(items[0]["kind"], "online")

    def test_qq_search_empty_raises(self):
        p = self._provider()
        payload = json.dumps({"success": True, "data": []}).encode("utf-8")
        with mock.patch.object(p, "_fetch", return_value=payload):
            with self.assertRaises(music_module.MusicError):
                p.qqmusic_search("不存在的歌")

    def test_qq_search_empty_query_raises(self):
        p = self._provider()
        with self.assertRaises(music_module.MusicError):
            p.qqmusic_search("   ")


if __name__ == "__main__":
    unittest.main()
