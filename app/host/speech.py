"""Turn an assistant answer into text the firmware synthesiser can read.

The on-device TTS reads characters literally, so a raw markdown answer would
be spoken as "**粗体**", "```" and "https://example.com". This module keeps the
prose and drops the markup. It never invents content: fenced code is announced
as skipped rather than silently removed, and every other line survives.
"""
from __future__ import annotations

import re

FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
HORIZONTAL = re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}$")
TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s*")
QUOTE = re.compile(r"^\s{0,3}>\s?")
BULLET = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+")

IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
REF_LINK = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
AUTOLINK = re.compile(r"<((?:https?|mailto|ftp):[^> \s]+)>")
BARE_URL = re.compile(r"(?<![\w<])((?:https?|ftp)://[^\s<>\"'）)，。]+)")
FOOTNOTE = re.compile(r"\[\^[^\]]*\]")
INLINE_CODE = re.compile(r"`([^`]*)`")
BOLD = re.compile(r"(\*\*|__)(.+?)\1", re.S)
ITALIC_STAR = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
ITALIC_US = re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])")
STRIKE = re.compile(r"~~(.+?)~~", re.S)
ASCII_PATH = re.compile(r"(?<![\w~])((?:/[A-Za-z0-9._@+-]+){2,}/?[A-Za-z0-9._@+-]*)")

EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff"
    "\u2190-\u21ff\u2b00-\u2bff\ufe0e\ufe0f\u200b-\u200d\u2060\u00a0]"
)
ASCII_WORD = re.compile(r"[A-Za-z0-9]")
CJK = re.compile(r"[\u3000-\u9fff\u3040-\u30ff]")
TERMINAL = "。！？!?…；;：:，,、）)】》」』\"'’”"

CODE_MARK = "（代码略）"


def _inline(text):
    text = IMAGE.sub("", text)
    text = LINK.sub(r"\1", text)
    text = REF_LINK.sub(r"\1", text)
    text = FOOTNOTE.sub("", text)
    text = AUTOLINK.sub("（链接已省略）", text)
    text = BARE_URL.sub("（链接已省略）", text)
    text = INLINE_CODE.sub(r"\1", text)
    text = BOLD.sub(r"\2", text)
    text = STRIKE.sub(r"\1", text)
    text = ITALIC_STAR.sub(r"\1", text)
    text = ITALIC_US.sub(r"\1", text)
    text = ASCII_PATH.sub(lambda m: _short_path(m.group(1)), text)
    return text


def _short_path(path):
    parts = [p for p in path.split("/") if p]
    if len(parts) <= 2:
        return path
    return "/".join(parts[-2:])


def _line(text):
    text = HORIZONTAL.sub("", text)
    text = TABLE_RULE.sub("", text)
    text = HEADING.sub("", text)
    text = QUOTE.sub("", text)
    text = BULLET.sub("", text)
    if text.count("|") >= 2:
        cells = [c.strip() for c in text.strip("|").split("|")]
        text = "，".join(c for c in cells if c)
    text = _inline(text)
    text = EMOJI.sub("", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    text = re.sub(r" +([。！？，、；：,.;:!?])", r"\1", text)
    return text.strip(" ，,")


def _needs_space(left, right):
    if not left or not right:
        return False
    if ASCII_WORD.match(left[-1]) and ASCII_WORD.match(right[0]):
        return True
    if left.endswith(":") and ASCII_WORD.match(right[0]):
        return True
    if left[-1].isascii() and not left[-1].isspace() and CJK.match(right[0]):
        return True
    return False


def _terminate(line):
    if not line:
        return line
    if line[-1] in TERMINAL:
        return line
    if line[-1].isascii() and line[-1] in ".!?":
        return line
    return line + ("." if not CJK.search(line) else "。")


def to_speech(text):
    """Return a speakable version of a markdown answer."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    position = 0
    for match in FENCE.finditer(text):
        lines.extend(_lines(text[position:match.start()]))
        lines.append(CODE_MARK)
        position = match.end()
    lines.extend(_lines(text[position:]))
    out = ""
    for line in lines:
        if not line:
            continue
        if not out:
            out = line
        elif _needs_space(out, line):
            out += " " + line
        else:
            out += line
    out = re.sub(r"\.{3,}", "…", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def _lines(block):
    out = []
    for line in block.split("\n"):
        cleaned = _line(line)
        if cleaned:
            out.append(_terminate(cleaned))
    return out


def clip(text, limit, tail="完整内容请查看电脑端。"):
    """Shorten to `limit` characters, preferring a sentence boundary."""
    if not limit or len(text) <= limit:
        return text
    cut = text[:limit]
    best = -1
    for mark in ("。", "！", "？", "!", "?", "；"):
        best = max(best, cut.rfind(mark))
    if best >= int(limit * 0.5):
        cut = cut[:best + 1]
    else:
        cut = cut.rstrip(" ，,") + "…"
    if tail and not cut.endswith(tail):
        cut = cut + tail
    return cut


def summary_of(text, limit=60):
    """One short line for the 640x480 screen."""
    plain = to_speech(text)
    plain = re.sub(r"\s+", " ", plain).strip()
    if len(plain) <= limit:
        return plain
    return plain[:limit].rstrip(" ，,") + "…"


def is_speechable(text):
    return bool(to_speech(text).strip())
