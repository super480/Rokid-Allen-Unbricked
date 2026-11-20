"""The device's speech gate, checked against the host's own rule.

``SpeechGate.java`` is plain Java -- it needs nothing from Android -- so it
can be compiled and run on this machine. That makes it possible to compare
the two sides on the same sentences: the host decides who answers a turn
in Python, the device decides whether to silence the factory in Java, and
the two must never disagree, or two replies happen at once.

The rule itself is the one in the configuration; the device gets it with
the status, so this test also proves that the copy on the device is the
same as the one here.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "host"
ANDROID = ROOT / "android" / "src" / "cn" / "rokid" / "bridge"

sys.path.insert(0, str(HOST))

import config as config_module  # noqa: E402
import intent  # noqa: E402

# The sentences both sides are asked about. The first group belongs to the
# factory, the second one does not.
SENTENCES = [
    "现在几点了",
    "今天几号",
    "今天星期几",
    "报时",
    "明天早上八点提醒我",
    "放一首周杰伦",
    "今天天气怎么样",
    "打开蓝牙",
    "重启一下",
    "帮我写一首关于秋天的诗",
    "列一下目录",
    "这段代码有什么问题",
    # Playback controls for the bridge's own MediaPlayer. The factory must
    # not own these, or the host never sees the command.
    "暂停",
    "继续播放",
    "下一首",
    "上一首",
    "停止播放",
    # Asking for a song is ours too: the bridge plays from its own local
    # library, falling back to Netease when the song is not there.
    "我想听晴天",
    "来首歌",
    "播放音乐",
    # Volume drives our own STREAM_MUSIC now, not the factory's player.
    "把音量调大一点",
    "调大音量",
    "静音",
    "音量调到50",
    "取消静音",
]

# The Java side, compiled and run on this machine.
CHECKER = '''package cn.rokid.bridge;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.List;

/**
 * Answers five questions for every sentence: does the factory own it, is
 * it taken in hybrid mode, is it taken in full mode, is it taken in off
 * mode while the bridge's MediaPlayer is running, and is it taken in off
 * mode with no clip playing (spoken song requests).
 */
public class SpeechGateCheck {
    public static void main(String[] args) throws Exception {
        String rule = new String(Files.readAllBytes(Paths.get(args[0])), StandardCharsets.UTF_8);
        List<String> sentences = Files.readAllLines(Paths.get(args[1]), StandardCharsets.UTF_8);
        StringBuilder out = new StringBuilder();
        for (String raw : sentences) {
            if (raw == null || raw.isEmpty()) continue;
            boolean owns = newGate(rule, SpeechGate.DIRECT_OFF, false).ownsFactoryTurn(raw);
            boolean hybrid = taken(rule, SpeechGate.DIRECT_HYBRID, false, raw);
            boolean full = taken(rule, SpeechGate.DIRECT_FULL, false, raw);
            boolean playing = taken(rule, SpeechGate.DIRECT_OFF, true, raw);
            boolean request = taken(rule, SpeechGate.DIRECT_OFF, false, raw);
            boolean suppressed = suppressed(rule, raw);
            out.append(raw).append('\\t').append(owns).append('\\t')
               .append(hybrid).append('\\t').append(full).append('\\t')
               .append(playing).append('\\t').append(request).append('\\t')
               .append(suppressed).append('\\n');
        }
        System.out.print(out);
    }

    private static SpeechGate newGate(String rule, int mode, boolean bridgePlaying) {
        SpeechGate gate = new SpeechGate();
        gate.rule(rule);
        gate.selected(false, 0L);
        gate.direct(mode);
        gate.bridgePlaying(bridgePlaying);
        return gate;
    }

    /** True when the device would silence the factory for this sentence. */
    private static boolean taken(String rule, int mode, boolean bridgePlaying, String raw) {
        SpeechGate gate = newGate(rule, mode, bridgePlaying);
        gate.asr(raw, 1000L);
        return "eligible".equals(gate.reason(1000L));
    }

    /** True when a music turn opens the factory-notice suppression window. */
    private static boolean suppressed(String rule, String raw) {
        SpeechGate gate = newGate(rule, SpeechGate.DIRECT_OFF, false);
        gate.asr(raw, 1000L);
        return gate.suppressFactory(1000L);
    }
}
'''


def find_jdk():
    """The JDK the Android build uses."""
    for candidate in (ROOT / "tools" / "jdk" / "Contents" / "Home",
                      Path(__import__("os").environ.get("JAVA_HOME", "/nonexistent"))):
        if (candidate / "bin" / "javac").exists():
            return candidate
    return None


class SpeechGateTest(unittest.TestCase):
    """The device side of the split, compiled and run here."""

    @classmethod
    def setUpClass(cls):
        cls.jdk = find_jdk()
        if cls.jdk is None:
            raise unittest.SkipTest("没有可用的 JDK，跳过设备端规则验证")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.work = Path(cls.tmp.name)

        # The rule the host uses, written out for the device to read.
        cls.pattern = config_module.DEFAULTS["direct"]["factory_owns"]
        (cls.work / "rule.txt").write_text(cls.pattern, encoding="utf-8")
        (cls.work / "sentences.txt").write_text(
            "\n".join(SENTENCES) + "\n", encoding="utf-8")

        (cls.work / "cn" / "rokid" / "bridge").mkdir(parents=True)
        (cls.work / "cn" / "rokid" / "bridge" / "SpeechGateCheck.java").write_text(
            CHECKER, encoding="utf-8")
        (cls.work / "cn" / "rokid" / "bridge" / "SpeechGate.java").write_text(
            (ANDROID / "SpeechGate.java").read_text(encoding="utf-8"), encoding="utf-8")

        cls.classes = cls.work / "classes"
        cls.classes.mkdir()
        subprocess.run(
            [str(cls.jdk / "bin" / "javac"), "-encoding", "UTF-8",
             "-d", str(cls.classes),
             str(cls.work / "cn" / "rokid" / "bridge" / "SpeechGate.java"),
             str(cls.work / "cn" / "rokid" / "bridge" / "SpeechGateCheck.java")],
            check=True, capture_output=True)
        result = subprocess.run(
            [str(cls.jdk / "bin" / "java"), "-cp", str(cls.classes),
             "cn.rokid.bridge.SpeechGateCheck",
             str(cls.work / "rule.txt"), str(cls.work / "sentences.txt")],
            check=True, capture_output=True, text=True)
        cls.rows = {}
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                cls.rows[parts[0]] = (parts[1] == "true", parts[2] == "true",
                                      parts[3] == "true",
                                      len(parts) > 4 and parts[4] == "true",
                                      len(parts) > 5 and parts[5] == "true",
                                      len(parts) > 6 and parts[6] == "true")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "tmp", None):
            cls.tmp.cleanup()

    def test_the_device_read_the_same_rule(self):
        # The rule the device compiles is the one the host has.
        self.assertEqual(len(self.rows), len(SENTENCES))

    def test_both_sides_agree_about_the_factory_skills(self):
        for sentence in SENTENCES:
            self.assertIn(sentence, self.rows, sentence)
            java_owns = self.rows[sentence][0]
            python_owns = intent.factory_owns(sentence, self.pattern)
            self.assertEqual(java_owns, python_owns, sentence)

    def test_hybrid_leaves_the_factory_skills_alone(self):
        for sentence in SENTENCES:
            java_taken = self.rows[sentence][1]
            python_owns = intent.factory_owns(sentence, self.pattern)
            self.assertEqual(java_taken, not python_owns, sentence)

    def test_full_takes_everything(self):
        for sentence in SENTENCES:
            self.assertTrue(self.rows[sentence][2], sentence)

    def test_controls_reach_the_host_instead_of_the_factory(self):
        controls = ("暂停", "继续播放", "下一首", "上一首", "停止播放")
        for sentence in controls:
            self.assertFalse(intent.factory_owns(sentence, self.pattern),
                             sentence)
            self.assertFalse(self.rows[sentence][0], sentence)
            # Hybrid hands the turn to the host.
            self.assertTrue(self.rows[sentence][1], sentence)

    def test_bridge_playback_takes_controls_in_every_direct_mode(self):
        controls = ("暂停", "继续播放", "下一首", "上一首", "停止播放")
        for sentence in controls:
            self.assertTrue(self.rows[sentence][3], sentence)
        # Ordinary factory skills must not be stolen just because a song
        # happens to be playing.
        for sentence in ("现在几点了", "今天天气怎么样",
                         "这段代码有什么问题"):
            self.assertFalse(self.rows[sentence][3], sentence)

    def test_song_requests_reach_the_host_in_every_direct_mode(self):
        songs = ("我想听晴天", "放一首周杰伦", "来首歌", "播放音乐")
        for sentence in songs:
            self.assertFalse(intent.factory_owns(sentence, self.pattern),
                             sentence)
            self.assertFalse(self.rows[sentence][0], sentence)
            self.assertTrue(self.rows[sentence][1], sentence)   # hybrid
            # Off mode with nothing playing: the factory must still stay
            # quiet, or it answers about its own cloud catalogue.
            self.assertTrue(self.rows[sentence][4], sentence)
        # Ordinary questions are not stolen by the request rule.
        for sentence in ("现在几点了", "帮我写一首关于秋天的诗",
                         "这段代码有什么问题"):
            self.assertFalse(self.rows[sentence][4], sentence)

    def test_music_turns_suppress_the_factory_notice(self):
        # The canned "合作到期，请用蓝牙播放" answer must not be spoken
        # after any music turn, even in off mode where the factory owns
        # unnamed turns.
        for sentence in ("我想听晴天", "来首歌", "暂停", "下一首",
                         "调大音量", "静音", "音量调到50"):
            self.assertTrue(self.rows[sentence][5], sentence)
        for sentence in ("现在几点了", "今天天气怎么样",
                         "这段代码有什么问题"):
            self.assertFalse(self.rows[sentence][5], sentence)

    def test_volume_turns_reach_the_host_in_every_direct_mode(self):
        for sentence in ("把音量调大一点", "调大音量", "静音",
                         "音量调到50", "取消静音"):
            self.assertFalse(intent.factory_owns(sentence, self.pattern),
                             sentence)
            self.assertFalse(self.rows[sentence][0], sentence)
            self.assertTrue(self.rows[sentence][1], sentence)   # hybrid
            self.assertTrue(self.rows[sentence][4], sentence)   # off mode


if __name__ == "__main__":
    unittest.main()
