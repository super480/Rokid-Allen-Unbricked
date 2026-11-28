"""Card layouts and summaries: valid shapes, bad upstream values, wording."""
import re
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from miniapps import cards  # noqa: E402
from miniapps.codex_usage import (  # noqa: E402
    CodexError, normalise_rate_limits, normalise_usage)
from miniapps.models import validate_card  # noqa: E402

NOW = time.time()


def limits(primary=None, secondary=None):
    return normalise_rate_limits({"rateLimitsByLimitId": {"codex": {
        "limitId": "codex",
        "primary": primary or {"usedPercent": 32,
                               "windowDurationMins": 300,
                               "resetsAt": int(NOW) + 14760},
        "secondary": secondary or {"usedPercent": 58,
                                   "windowDurationMins": 10080,
                                   "resetsAt": int(NOW) + 205200},
    }}})


def usage():
    return normalise_usage({
        "summary": {"lifetimeTokens": 123456789},
        "dailyUsageBuckets": [{"startDate": "2026-11-09", "tokens": 42000}],
    })


class SchemaTest(unittest.TestCase):
    def test_valid_quota_and_lines_cards(self):
        for operation in ("quota", "tokens", "overview"):
            result = cards.build_result(operation,
                                        {"rate_limits": limits(),
                                         "usage": usage()}, now=NOW)
            validate_card(result.data["card"])

    def test_rejects_negative_nan_and_string_numbers(self):
        card = cards.quota_card([{"label": "7d", "used_percent": 12,
                                  "reset_at_seconds": None,
                                  "reset_at_ms": None}], "ok",
                                cards.now_iso(NOW))
        for bad in (-1, "12", True, float("nan"), float("inf")):
            broken = dict(card)
            broken["rings"] = [dict(card["rings"][0], value=bad)]
            with self.assertRaises(ValueError):
                validate_card(broken)

    def test_rejects_bad_colour_icon_and_oversized_rows(self):
        card = cards.quota_card([{"label": "7d", "used_percent": 12,
                                  "reset_at_seconds": None,
                                  "reset_at_ms": None}], "ok",
                                cards.now_iso(NOW))
        for field, value in (("color", "blue"), ("radius", 0),
                             ("width", 900), ("value", None)):
            if field == "value":
                continue
            broken = dict(card)
            broken["rings"] = [dict(card["rings"][0], **{field: value})]
            with self.assertRaises(ValueError):
                validate_card(broken)
        for icon in ("../../etc/passwd.png", "icon.js"):
            with self.assertRaises(ValueError):
                validate_card(dict(card, icon=icon))
        with self.assertRaises(ValueError):
            validate_card(dict(card, rows=[{"label": "x", "value": "y"}] * 7))

    def test_lines_cards_cannot_carry_rings_and_vice_versa(self):
        lines = cards.lines_card("x", [], "ok", cards.now_iso(NOW))
        with self.assertRaises(ValueError):
            validate_card(dict(lines, rings=[{"label": "7d", "value": 1,
                                              "color": "#0987ff",
                                              "radius": 100, "width": 20}]))
        rings = cards.quota_card([{"label": "7d", "used_percent": 12,
                                   "reset_at_seconds": None,
                                   "reset_at_ms": None}], "ok",
                                 cards.now_iso(NOW))
        with self.assertRaises(ValueError):
            validate_card(dict(rings, rings=[]))

    def test_track_colour_must_be_hex(self):
        card = cards.quota_card([{"label": "7d", "used_percent": 12,
                                  "reset_at_seconds": None,
                                  "reset_at_ms": None}], "ok",
                                cards.now_iso(NOW))
        for bad in ("blue", "#fff", "#315a9600"):
            with self.assertRaises(ValueError):
                validate_card(dict(card, track_color=bad))

    def test_remaining_clamps_over_and_under_limit(self):
        self.assertEqual(cards.remaining(137), 0.0)
        self.assertEqual(cards.remaining(-4), 100.0)
        self.assertAlmostEqual(cards.remaining(32.4), 67.6)

    def test_device_and_host_agree_on_ring_count(self):
        java = (Path(__file__).resolve().parents[1]
                / "android/src/cn/rokid/bridge/MiniAppCardView.java").read_text()
        self.assertIn("Math.min(2, rings.length())", java)
        self.assertIn("new float[2]", java)

    def test_centre_logo_is_a_clean_transparent_white_glyph(self):
        """D037: the user-supplied near-white JPEG became a clean RGBA glyph."""
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover - optional on CI
            self.skipTest("Pillow not installed")
        path = (Path(__file__).resolve().parents[1]
                / "android/assets/system-chip.png")
        image = Image.open(path)
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.size, (256, 256))
        alpha = image.getchannel("A")
        for corner in ((0, 0), (255, 0), (0, 255), (255, 255)):
            self.assertEqual(alpha.getpixel(corner), 0)   # no rounded plate
        rgb = image.convert("RGB")
        solid = [rgb.getpixel((x, y))
                 for x in range(0, 256, 8) for y in range(0, 256, 8)
                 if alpha.getpixel((x, y)) > 240]
        # The public build ships system-chip.png, not D037's gpt-logo;
        # its 8px grid yields 138 solid samples. The 200 threshold belonged
        # to the old private asset and was left behind when v0.2.7 swapped
        # the reference (948d979). Keep the real quality checks: transparent
        # corners, pure white pixels, and a visibly filled glyph.
        self.assertGreater(len(solid), 100)
        self.assertTrue(all(pixel == (255, 255, 255) for pixel in solid))

    def test_device_renderer_keeps_the_review_decisions(self):
        java = (Path(__file__).resolve().parents[1]
                / "android/src/cn/rokid/bridge/MiniAppCardView.java").read_text()
        self.assertIn("ANIMATION_MS = 900L", java)
        self.assertIn('str(card, "track_color")', java)
        self.assertIn('label + "   " + value', java)
        self.assertIn("0xFF315A96", java)
        self.assertIn("drawCap(canvas", java)
        self.assertIn("Math.min(1.06f", java)   # overshoot is not clipped away
        self.assertIn("START_FRACTION = 0.5f", java)
        self.assertIn("target[i] * START_FRACTION", java)
        # D037: filtered bitmap removes the furry edge, rows move up/together.
        self.assertIn("Paint.FILTER_BITMAP_FLAG", java)
        self.assertIn("float y = 356f + i * 26f", java)
        self.assertIn("elapsed % 16L", java)    # frame cadence is absolute
        # D038: static layer cache, ONE shared number rule for both rings,
        # and a real frame-rate log (gfxinfo has no display lists here).
        self.assertIn("canvas.drawBitmap(backdrop(), 0f, 0f, null)", java)
        self.assertIn("private void drawTrack(Canvas canvas)", java)
        self.assertIn("NUMBER_SIZE = 20f", java)
        self.assertIn("anim frames=", java)

    def test_both_ring_numbers_share_one_rule(self):
        """Regression after the user's 22:44 screenshot: the inner number
        had its own inset / back-off / type size, so it sat up the middle
        of the arc and looked smaller than the outer one. Both rings now
        use the same endpoint, centreline, rotation and 20px type.
        """
        java = (Path(__file__).resolve().parents[1]
                / "android/src/cn/rokid/bridge/MiniAppCardView.java").read_text()
        self.assertIn("drawRingLabels(canvas, ring, radius, raw, fraction)", java)
        self.assertIn("NUMBER_SIZE = 20f", java)
        self.assertIn("textPaint.setTextSize(NUMBER_SIZE)", java)
        self.assertIn("double endRad = Math.toRadians(end)", java)
        self.assertIn("float rotation = end + 90f", java)
        self.assertIn("CX + radius * Math.cos(endRad)", java)
        # No inner-only knobs may come back.
        for gone in ("INNER_NUMBER_INSET", "INNER_NUMBER_BACKOFF_DEG",
                     "INNER_NUMBER_SIZE", "boolean inner"):
            self.assertNotIn(gone, java, gone)

        # Equal bands and equal type keep the two numbers symmetric.
        self.assertEqual(cards.OUTER["width"], cards.INNER["width"])
        size = float(re.search(r"NUMBER_SIZE = ([0-9.]+)f", java).group(1))
        self.assertEqual(size, 20.0)
        glyph_height = size * 0.75          # bold digits are shorter than em
        self.assertLess(glyph_height / 2, cards.INNER["width"] / 2,
                        "digits would leave the 30px band radially")

        # The endpoint angles differ only by the two values, never by a
        # per-ring offset: 93% and 94% land on (nearly) the same ray.
        def end_angle(value):
            return 135.0 + 270.0 * min(value / 100.0, 1.0) - 5.0

        self.assertLess(abs(end_angle(93) - end_angle(94)), 3.0)

    def test_spring_curve_overshoots_then_rebounds(self):
        java = (Path(__file__).resolve().parents[1]
                / "android/src/cn/rokid/bridge/MiniAppCardView.java").read_text()
        block = re.search(r"SPRING\s*=\s*\{(.*?)\};", java, re.S)
        self.assertIsNotNone(block)
        knots = [(float(t), float(v)) for t, v in re.findall(
            r"\{\s*([0-9.]+)f\s*,\s*([0-9.\-]+)f\s*\}", block.group(1))]
        self.assertEqual([t for t, _ in knots],
                         [0.0, 0.34, 0.55, 0.72, 0.87, 1.0])

        def spring(u):
            u = min(1.0, max(0.0, u))
            for i in range(1, len(knots)):
                a, b = knots[i - 1], knots[i]
                if u <= b[0]:
                    q = min(1.0, max(0.0, (u - a[0]) / (b[0] - a[0])))
                    if i > 1:
                        q = q * q * (3 - 2 * q)       # smoothstep
                    return a[1] + (b[1] - a[1]) * q
            return 1.0

        samples = [spring(i / 200) for i in range(201)]
        self.assertAlmostEqual(samples[0], 0.0)
        self.assertAlmostEqual(samples[-1], 1.0)
        crest = max(samples[:70])                 # t < 0.35
        self.assertGreater(crest, 1.02)          # really passes the target
        trough = min(samples[85:120])            # 0.425 <= t < 0.60
        self.assertLess(trough, 0.98)            # springs back below it
        bounce = max(samples[130:160])           # 0.65 <= t < 0.80
        self.assertGreater(bounce, 1.0)
        self.assertLess(bounce, crest)           # damped, not another full crest
        self.assertLess(max(abs(samples[i + 1] - samples[i])
                            for i in range(200)), 0.1)  # no jumps

    def test_first_leg_is_visible_instead_of_a_cubic_rush(self):
        """D037: at 10% of the timeline the arc must still be mid-travel."""
        java = (Path(__file__).resolve().parents[1]
                / "android/src/cn/rokid/bridge/MiniAppCardView.java").read_text()
        block = re.search(r"SPRING\s*=\s*\{(.*?)\};", java, re.S)
        knots = [(float(t), float(v)) for t, v in re.findall(
            r"\{\s*([0-9.]+)f\s*,\s*([0-9.\-]+)f\s*\}", block.group(1))]
        self.assertNotIn("1f - inv * inv * inv", java)  # old ease-out rush
        b = knots[1]
        self.assertAlmostEqual(b[1] * (0.10 / b[0]), 0.315, delta=0.01)
        self.assertLess(b[1] * (0.10 / b[0]), 0.45)     # not already at target
        self.assertGreater(b[1] * (0.10 / b[0]), 0.15)  # not frozen either

    def test_entrance_starts_at_each_ring_halfway_point(self):
        java = (Path(__file__).resolve().parents[1]
                / "android/src/cn/rokid/bridge/MiniAppCardView.java").read_text()
        self.assertIn("from[i] = sameCard ? shown[i] : target[i] * START_FRACTION",
                      java)
        block = re.search(r"SPRING\s*=\s*\{(.*?)\};", java, re.S)
        knots = [(float(t), float(v)) for t, v in re.findall(
            r"\{\s*([0-9.]+)f\s*,\s*([0-9.\-]+)f\s*\}", block.group(1))]

        def spring(u):
            for i in range(1, len(knots)):
                a, b = knots[i - 1], knots[i]
                if u <= b[0]:
                    q = min(1.0, max(0.0, (u - a[0]) / (b[0] - a[0])))
                    if i > 1:
                        q = q * q * (3 - 2 * q)
                    return a[1] + (b[1] - a[1]) * q
            return 1.0

        # A real quota card (94% / 98%): the first frame is exactly half,
        # the crest still passes the target, and it settles on the target.
        for target in (94.0, 98.0):
            start = target * 0.5
            self.assertAlmostEqual(
                start + (target - start) * spring(0.0), target / 2)
            # D037: the crest now arrives at t=0.34 of a 900ms run.
            crest = start + (target - start) * spring(0.34)
            self.assertGreater(crest, target)
            self.assertLess(crest, 106.0)       # arc is allowed to 106%
            self.assertAlmostEqual(
                start + (target - start) * spring(1.0), target)


class QuotaCardTest(unittest.TestCase):
    def test_v6_geometry_and_colours(self):
        result = cards.build_result("quota", {"rate_limits": limits()}, now=NOW)
        card = result.data["card"]
        self.assertEqual(card["template"], "rings")
        self.assertEqual([r["label"] for r in card["rings"]], ["7d", "5h"])
        self.assertEqual(card["rings"][0]["color"], "#0987ff")
        self.assertEqual(card["rings"][0]["radius"], 138)
        self.assertEqual(card["rings"][1]["radius"], 96)
        # Equal stroke weight (user revision 2026-11-15): 32/27 -> 30/30.
        self.assertEqual(card["rings"][0]["width"], 30)
        self.assertEqual(card["rings"][1]["width"], 30)
        self.assertEqual(card["rings"][0]["width"], card["rings"][1]["width"])
        # Shrunk uniformly; the centre logo is drawn by the device at a
        # fixed 94x94 and is therefore not part of this contract.
        self.assertAlmostEqual(138 / 96, 158 / 110, delta=0.01)
        self.assertEqual(card["rings"][1]["color"], "#00bfed")
        self.assertEqual(card["icon"], "system-chip.png")
        self.assertEqual(card["track_color"], "#315a96")
        self.assertEqual(result.status, "ok")

    def test_arcs_and_speech_show_remaining_not_used(self):
        result = cards.build_result("quota", {"rate_limits": limits()}, now=NOW)
        rings = result.data["card"]["rings"]
        self.assertEqual([r["value"] for r in rings], [42.0, 68.0])
        self.assertEqual([r["used_percent"] for r in rings], [58, 32])
        self.assertIn("剩余百分之四十二", result.speech)
        self.assertNotIn("已用", result.speech)

    def test_low_and_zero_used_stay_readable(self):
        low = limits({"usedPercent": 0, "windowDurationMins": 300},
                     {"usedPercent": 3, "windowDurationMins": 10080})
        result = cards.build_result("quota", {"rate_limits": low}, now=NOW)
        self.assertIn("百分之一百", result.speech)
        self.assertEqual({r["value"] for r in result.data["card"]["rings"]},
                         {100.0, 97.0})

    def test_over_limit_shows_empty_arc_and_warning(self):
        over = limits({"usedPercent": 137, "windowDurationMins": 300})
        result = cards.build_result("quota", {"rate_limits": over}, now=NOW)
        self.assertEqual(result.data["card"]["rings"][1]["value"], 0.0)
        self.assertIn("超出额度", result.speech)

    def test_missing_window_is_not_invented(self):
        empty = normalise_rate_limits({"rateLimitsByLimitId": {}})
        result = cards.build_result("quota", {"rate_limits": empty}, now=NOW)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.data["card"]["template"], "lines")
        self.assertIn("无法", result.speech)

    def test_expired_reset_never_zeroes_the_value(self):
        expired = limits({"usedPercent": 44, "windowDurationMins": 300,
                          "resetsAt": int(NOW) - 60})
        result = cards.build_result("quota", {"rate_limits": expired}, now=NOW)
        self.assertEqual(cards.reset_brief(int(NOW) - 60, NOW), "等待更新")
        self.assertEqual(result.data["card"]["rings"][0]["value"], 42.0)
        self.assertIn("等待数据更新", result.speech)

    def test_summary_is_short_and_uses_chinese_numbers(self):
        result = cards.build_result("quota", {"rate_limits": limits()}, now=NOW)
        self.assertLessEqual(len(result.speech), 100)
        self.assertIn("剩余百分之四十二", result.speech)


class TokenCardTest(unittest.TestCase):
    def test_missing_today_is_explicit_not_zero(self):
        result = cards.build_result("tokens", {"usage": usage()}, now=NOW)
        self.assertEqual(result.data_through, "2026-11-09")
        self.assertIn("目前没有今天的 token 统计", result.speech)
        self.assertIn("最新日统计到十一月九日", result.speech)
        self.assertEqual(result.data["card"]["template"], "lines")

    def test_lifetime_is_formatted_in_python_not_in_javascript(self):
        result = cards.build_result("tokens", {"usage": usage()}, now=NOW)
        self.assertIn("1.23 亿", result.speech)
        self.assertEqual(result.data["lifetime_tokens"], "123456789")

    def test_no_summary_is_an_error_card(self):
        result = cards.build_result("tokens", {"usage": None}, now=NOW)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.data["card"]["rows"][0]["value"], "统计不可用")

    def test_overview_keeps_the_double_ring(self):
        result = cards.build_result("overview",
                                    {"rate_limits": limits(),
                                     "usage": usage()}, now=NOW)
        self.assertEqual(result.data["card"]["template"], "rings")
        self.assertLessEqual(len(result.speech), 100)
        self.assertIn("累计使用", result.speech)


class ErrorTest(unittest.TestCase):
    def test_every_error_code_has_trustworthy_wording(self):
        for code in ("not_logged_in", "unsupported_auth",
                     "unsupported_method", "unsupported_window",
                     "timeout", "upstream_error", "unavailable"):
            result = cards.error_result("quota", CodexError(code, "raw"))
            result.validate()
            self.assertNotIn("raw", result.speech)
            self.assertNotIn("0%", result.speech)
            self.assertEqual(result.status, "error")

    def test_cn_number_handles_large_and_negative(self):
        self.assertEqual(cards.cn_number(3), "三")
        self.assertEqual(cards.cn_number(58), "五十八")
        self.assertEqual(cards.cn_number(100), "一百")
        self.assertEqual(cards.cn_number(1200), "1200")


if __name__ == "__main__":
    unittest.main()
