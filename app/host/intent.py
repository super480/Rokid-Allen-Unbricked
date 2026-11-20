"""Turn a spoken sentence into an intent.

The device hands us whatever the recogniser heard. This module decides
whether that sentence selects an assistant, exits, repeats, stops, or is a
task for the current assistant.

Chinese ASR picks a different character for the same syllable often
enough that matching on the raw string is a coin flip: "小赫" arrives as
"小鹤" or "小贺" depending on the day. Every assistant name is therefore
matched twice -- once exactly, once after folding the known homophones
together (see ``fold``).
"""
from __future__ import annotations

import re

from config import REGISTRY, enabled_agents

EXIT_WORDS = (
    "退出助手", "结束对话", "返回待机", "退出小赫", "退出小家", "退出克劳德",
    "退出hermes", "退出gpt", "退出巴蒂", "退出大肥鱼", "退出老寇",
)

REPEAT_WORDS = ("再说一遍", "再说一次", "重复一下", "重复", "没听清", "刚才说的什么")

STOP_WORDS = ("别说了", "别念了", "停下", "停止", "安静", "闭嘴", "不要说了")

DIRECT_WORDS = ("用大模型", "用大模型回答", "直接回答", "切回直连", "回到直连")

# Prefixes the user may put in front of an assistant name.
SELECT_PREFIXES = ("找", "切换到", "切换成", "切换", "叫", "请找", "我要用")

# Characters the recogniser swaps for one another. The first character of
# each group is the canonical one; the rest fold onto it.
#
# The groups are deliberately narrow: every character in them must be one
# that appears inside an assistant name. A character like "我" is a
# homophone of the "沃" in "沃克巴迪", but folding it would rewrite every
# ordinary sentence the user says ("帮我看看" -> "帮沃看看").
HOMOPHONE_GROUPS = (
    "赫鹤贺喝盒合和荷",   # hé
    "墨默馍摸",           # mò
    "斯撕思私丝",         # sī
    "尔耳而",             # ěr
    "寇叩蔻扣",           # kòu
    "肥非飞菲",           # féi
    "蒂帝第弟地",         # dì
    "巴八吧霸",           # bā
    "克客刻课",           # kè
)


def _homophone_map():
    """Map every non-canonical character onto its group's first character."""
    table = {}
    for group in HOMOPHONE_GROUPS:
        for char in group[1:]:
            table[char] = group[0]
    return table


HOMOPHONE = _homophone_map()


def normalize(text):
    """Strip punctuation and case so matching is forgiving."""
    return re.sub(r"[\s，。！？,.!?：:、]", "", (text or "")).lower()


def fold(text):
    """Normalise and then collapse known homophones onto one spelling.

    "找小鹤" and "找小赫" both become "找小赫". The characters that get
    folded only occur inside assistant names, so an ordinary sentence comes
    back unchanged.
    """
    value = normalize(text)
    if not value:
        return ""
    return "".join(HOMOPHONE.get(char, char) for char in value)


def strip_wake_word(text):
    """Remove a leading wake word; the device may deliver it with the command."""
    return re.sub(r"^(?:若琪|若弃|洛奇|罗琪)", "", (text or "").strip())


def _alias_table():
    """Map every spoken alias to its assistant id.

    Two tables come back: the exact one, and one keyed on the folded
    spelling. A folded key that would point at two different assistants is
    dropped rather than guessed at.
    """
    table = {}
    folded = {}
    for agent_id, item in REGISTRY.items():
        for alias in item.get("aliases", []):
            key = normalize(alias)
            table[key] = agent_id
            folded_key = fold(alias)
            if folded.get(folded_key, agent_id) != agent_id:
                # Two assistants collapse onto the same spelling; guessing
                # would switch the user to the wrong one.
                folded.pop(folded_key, None)
                continue
            folded[folded_key] = agent_id
    return table, folded


def _match_alias(value, table, folded_table):
    """Look a spoken name up, exactly first and folded second."""
    if value in table:
        return table[value]
    if value in folded_table:
        return folded_table[value]
    return None


def wake_prefix(text, spellings):
    """Split a spoken sentence into the wake word and what was said after it.

    A custom wake word is spoken at the front of the sentence, the same
    way "若琪现在几点了" carries the factory word. ``spellings`` maps an
    assistant id to the Chinese spellings of its word; the recogniser
    often picks a different character for the same syllable, so both the
    plain and the folded spellings are compared.

    Returns ``(agent_id, remainder)`` or ``(None, text)``. Longer words
    win, so a two-syllable word is not swallowed by a one-syllable one.
    """
    value = normalize(text or "")
    folded = fold(value)
    if not value:
        return None, text or ""
    pairs = []
    for agent_id, words in (spellings or {}).items():
        for word in words:
            key = normalize(word)
            if key:
                pairs.append((key, fold(key), agent_id))
    pairs.sort(key=lambda row: len(row[0]), reverse=True)
    for key, folded_key, agent_id in pairs:
        hit = None
        if value.startswith(key):
            hit = len(key)
        elif folded.startswith(folded_key):
            # The folded text can differ in length when a homophone maps
            # onto a character with another width: count from the folded
            # text, then take that many characters off the original.
            hit = len(folded_key)
        if hit is None:
            continue
        rest = _drop_leading_chars(text or "", hit)
        # Punctuation between the word and the question is normal.
        rest = re.sub(r"^[\s，。！？,.!?：:、]+", "", rest)
        return agent_id, rest
    return None, text or ""


# A build may print the Chinese characters it picked from the pinyin
# instead of the vt_word label we registered. The two English labels
# ("GPT" registered as ji4pi4ti4, "Hermes" as xiao3he4) are the ones
# that need this: the recogniser writes the sounds, not the letters.
WAKE_PHONETIC = {
    "da4fei2yu2": ("大非鱼", "大肥余", "大飞鱼"),
    "ba1di4": ("巴帝", "把弟", "八弟", "八八"),
    "ji4pi4ti4": ("鸡屁踢", "基皮提", "几皮提", "计皮提"),
    "xiao3he4": ("小赫", "小鹤", "小贺", "小和"),
    "xiao3jia1": ("小家", "小佳", "小嘉", "小加"),
    "ke4lao2de3": ("克劳德", "柯劳德", "科劳德", "克劳得", "克老德"),
}


def wake_word_agent(word, spellings, pinyins=None):
    """Map the wake word the engine actually matched to an assistant id.

    BlackSiren logs ``vt word with 大肥鱼 [...]`` at the moment it decides
    a word was spoken. That label is the vt_word we registered -- the
    assistant's name -- not the recognised sentence, which the NLP layer
    has already stripped of its wake word. ``pinyins`` covers builds that
    print the phonetic spelling instead of the label.

    Returns the assistant id, or None for the factory's own word
    ("若琪|洛奇") and for anything else we do not manage. Leaving the
    factory word alone keeps "若琪" behaving exactly as before.
    """
    if not word or not spellings:
        return None
    candidates = [normalize(part) for part in
                  re.split(r"[|｜,，/]", str(word))]
    candidates = [value for value in candidates if value]
    if not candidates:
        return None

    pairs = []
    for agent_id, words in spellings.items():
        for spelling in words:
            key = normalize(spelling)
            if key:
                pairs.append((key, fold(key), agent_id))
    for agent_id, pinyin in (pinyins or {}).items():
        key = normalize(pinyin)
        if key:
            pairs.append((key, key, agent_id))
        for written in WAKE_PHONETIC.get(str(pinyin).lower(), ()):
            key = normalize(written)
            if key:
                pairs.append((key, fold(key), agent_id))
    # A short alias must not swallow a longer word starting the same way.
    pairs.sort(key=lambda row: len(row[0]), reverse=True)

    for candidate in candidates:
        folded_candidate = fold(candidate)
        for key, folded_key, agent_id in pairs:
            if candidate == key or folded_candidate == folded_key:
                return agent_id
    # Some builds wrap the label in extra text. Fall back to containment,
    # but only for keys long enough not to match an ordinary word.
    for candidate in candidates:
        folded_candidate = fold(candidate)
        for key, folded_key, agent_id in pairs:
            if len(key) >= 2 and (key in candidate
                                  or folded_key in folded_candidate):
                return agent_id
    return None


def _drop_leading_chars(text, count):
    """Take ``count`` meaningful characters off the front.

    ``fold`` maps one character onto one character, but ``normalize``
    removes punctuation and whitespace first. Counting bytes off the raw
    text would mis-slice a sentence that begins with a comma.
    """
    seen = 0
    for index, char in enumerate(text):
        if normalize(char):
            seen += 1
            if seen == count:
                return text[index + 1:]
    return ""


# Playback controls for the bridge's own MediaPlayer. These words are
# deliberately absent from config's factory_owns rule: the factory's music
# skill drives a different (empty) player, so it must not swallow them.
# Keep the phrases short and unambiguous -- a long sentence that happens
# to contain one of them is a question, not a control.
MUSIC_CONTROL_WORDS = (
    ("pause", ("暂停", "暂停播放", "暂停音乐", "暂停一下", "暂定",
               "停一停", "音乐暂停", "先暂停")),
    ("resume", ("继续播放", "继续放", "接着放", "恢复播放", "继续听",
                "放回来", "恢复音乐")),
    ("next", ("下一首", "下一曲", "下一首歌", "换一首", "换首歌",
              "切歌", "跳过", "切下一首", "换一首歌")),
    ("prev", ("上一首", "上一曲", "上一首歌", "回到上一首",
              "切上一首", "换回上一首")),
    ("stop", ("停止播放", "停止音乐", "停止放歌", "别放了", "不听了",
              "关掉音乐", "关闭音乐", "别放音乐")),
)


# A question that happens to contain a control word is still a question.
MUSIC_QUESTION_WORDS = ("什么", "怎么", "为什么", "是谁", "意思", "讲讲",
                        "说说", "解释", "查一下", "吗", "呢", "故事",
                        "建议", "意见", "如何", "能不能", "可以吗",
                        "是不是", "有没有")

# "我想听晴天", "放一首周杰伦的歌", "来首歌吧". Longest first, so
# "给我放一首" wins over "放一首". Deliberately no bare "听" / "放":
# "听我说" and "放心吧" are conversation, not song requests. Keep this
# table in step with SpeechGate.REQUEST on the device.
MUSIC_REQUEST_PREFIXES = (
    "给我放一首", "给我来一首", "给我放", "给我来",
    "随便放一首", "随便来一首", "随便放", "随便来",
    "我想听", "我要听", "想听", "要听",
    "来一首", "放一首", "播一首", "点一首", "找一首",
    "听一下", "搜一下", "放点音乐", "来点音乐",
    "来首", "放首", "点首", "听首", "找首",
    "听歌", "播放",
)
MUSIC_TAIL_WORDS = ("好不好", "可以吗", "好吗", "行吗",
                    "吧", "啊", "呀", "嘛", "呗", "了")
MUSIC_RANDOM_WORDS = frozenset((
    "", "歌", "首歌", "一首歌", "音乐", "歌曲", "点歌",
    "随便", "随便的", "随便来", "什么都行", "任意",
))


def _strip_polite(value):
    for tail in MUSIC_TAIL_WORDS:
        if value.endswith(tail) and len(value) > len(tail):
            return value[:-len(tail)]
    return value


# Canned notices the factory speaks when a cloud service has expired. If the
# microphone hears our own speaker saying one, the ASR can forward it as a
# user turn. Answering that would make the speaker say it again, so drop it
# like an echo: it must not reach an assistant or show up in the console.
# There are two families: the old music-partnership notice, and the newer
# "free content service expired, renew in the Rokid app" notice. The latter
# is spoken as a fixed clip on every wake once the subscription lapses, so
# it is the one most likely to be heard in a loop.
FACTORY_NOTICE_WORDS = ("合作到期", "请用蓝牙播放", "版权合作",
                        "服务到期", "合作已到期",
                        "内容服务", "我的内容服务", "若琪app")


def factory_notice(text):
    """True for a canned factory service-expiry notice, not a user turn."""
    value = normalize(strip_wake_word(text))
    if not value or len(value) > 60:
        return False
    # A real question about the service is a user turn, not the notice.
    if any(marker in value for marker in ("吗", "呢", "什么", "怎么",
                                          "多少", "几号")):
        return False
    if any(word in value for word in FACTORY_NOTICE_WORDS):
        return True
    # "免费的内容服务已经到期了" does not contain the literal "服务到期".
    return "到期" in value and "服务" in value


# Volume words. Longest first within each action: "取消静音" must beat
# "静音", and "调大一点" must beat "调大". No bare "声音" -- that is far
# too common in ordinary conversation.
MUSIC_VOLUME_WORDS = (
    ("unmute", ("取消静音", "解除静音", "恢复声音", "打开声音",
                "不要静音", "别静音", "取消静音吧")),
    ("mute", ("静音", "别出声", "不要出声", "关掉声音", "关闭声音")),
    ("up", ("调大音量", "加大音量", "提高音量", "增大音量",
            "大点声", "大声点", "大一点", "声音大点",
            "调大", "调高", "音量加", "大声")),
    ("down", ("调小音量", "减小音量", "降低音量", "减小声音",
              "小点声", "小声点", "小一点", "声音小点",
              "调小", "调低", "小声")),
)
VOLUME_STOP_WORDS = ("朗读", "念一遍", "背诵", "读一下", "说话", "喊", "叫",
                     "好不好", "行不行", "可以吗")
_VOLUME_DIGITS = r"([0-9]{1,3}|[零一二两三四五六七八九十百]+)"
VOLUME_PERCENT = re.compile(r"百分之" + _VOLUME_DIGITS)
VOLUME_NUMBER = re.compile(
    r"(?:调到|调至|调成|音量|设为)" + _VOLUME_DIGITS)
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_number(raw):
    if raw.isdigit():
        return int(raw)
    if raw in ("百", "一百"):
        return 100
    if "十" in raw:
        head, _, tail = raw.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(raw)


def music_volume(text):
    """Return ``{"action", "value"}`` for a volume command, else None.

    Actions: up, down, mute, unmute, set (value is 0-100 percent).
    Short command-like sentences only, so "大声朗读这首诗" stays a
    question for the assistant.
    """
    value = normalize(strip_wake_word(text))
    if not value or len(value) > 14:
        return None
    if any(marker in value for marker in MUSIC_QUESTION_WORDS):
        return None
    if any(marker in value for marker in VOLUME_STOP_WORDS):
        return None
    for action, words in MUSIC_VOLUME_WORDS:
        for word in words:
            if value == word or (word in value
                                 and len(value) <= len(word) + 6):
                return {"action": action, "value": None}
    match = VOLUME_PERCENT.search(value) or VOLUME_NUMBER.search(value)
    if match:
        number = _cn_number(match.group(1))
        if number is not None:
            return {"action": "set", "value": max(0, min(100, number))}
    return None


def music_request(text):
    """Parse a spoken song request, or return None when it is not one.

    Result keys: ``random`` (no name, pick a local song), ``phrase``
    (everything after the prefix), ``title`` / ``artist`` (best-effort
    split on 的), ``queries`` (search terms, most specific first).
    Short, command-like sentences only: "我想听你讲讲为什么" is a
    question for the assistant, not a request to play a song.
    """
    value = normalize(strip_wake_word(text))
    if not value or len(value) > 40:
        return None
    if any(marker in value for marker in MUSIC_QUESTION_WORDS):
        return None
    prefix = ""
    for candidate in MUSIC_REQUEST_PREFIXES:
        if value.startswith(candidate):
            prefix = candidate
            break
    if not prefix:
        return None
    phrase = _strip_polite(value[len(prefix):])
    if phrase in MUSIC_RANDOM_WORDS or len(phrase) < 2:
        return {"random": True, "phrase": "", "title": "",
                "artist": "", "queries": []}
    artist, title = "", phrase
    if "的" in phrase:
        head, _, tail = phrase.rpartition("的")
        if head and tail and len(head) <= 12 and len(tail) >= 2:
            artist, title = head, tail
    queries = [phrase]
    if title != phrase:
        queries.append(title)
    return {"random": False, "phrase": phrase, "title": title,
            "artist": artist, "queries": queries}


def music_control(text):
    """Return a playback action, or ``""`` when the sentence is not one.

    Actions: pause, resume, next, prev, stop. Only short, command-like
    sentences qualify, so "帮我讲讲暂停是什么意思" stays a question and
    goes to the assistant instead of pausing the song.
    """
    value = normalize(strip_wake_word(text))
    if not value or len(value) > 18:
        return ""
    if any(marker in value for marker in MUSIC_QUESTION_WORDS):
        return ""
    for action, words in MUSIC_CONTROL_WORDS:
        for word in words:
            # The whole sentence is the command, or it carries a short
            # polite wrapper ("请暂停一下", "先暂停").
            if value == word or (word in value and len(value) <= len(word) + 4):
                return action
    return ""


def parse_intent(text):
    """Return ``(intent, argument)``.

    intent is one of: select, exit, repeat, stop, direct, command.
    For "select" the argument is the assistant id.
    """
    raw = strip_wake_word(text)
    value = normalize(raw)
    folded = fold(raw)
    if not value:
        return "command", ""

    if folded in tuple(fold(word) for word in EXIT_WORDS):
        return "exit", None
    if folded in tuple(fold(word) for word in REPEAT_WORDS):
        return "repeat", None
    if folded in tuple(fold(word) for word in STOP_WORDS):
        return "stop", None
    if folded in tuple(fold(word) for word in DIRECT_WORDS):
        return "direct", None

    table, folded_table = _alias_table()
    for candidate in (value, folded):
        hit = _match_alias(candidate, table, folded_table)
        if hit:
            return "select", hit
    for prefix in SELECT_PREFIXES:
        for candidate in (value[len(prefix):], folded[len(prefix):]):
            hit = _match_alias(candidate, table, folded_table)
            if hit:
                return "select", hit
    return "command", (text or "").strip()


def factory_owns(text, pattern):
    """True when the factory handles this turn on its own.

    The rule is a regular expression that the host and the device share:
    the host sends it down with the status, so both decide the same way
    which turn belongs to the factory. That matters because the device has
    to know before the factory speaks whether to silence it.
    """
    if not pattern:
        return False
    # The same normalisation the device does: wake word removed, punctuation
    # dropped, lower case. Without it "WiFi" would not match "wifi".
    value = normalize(strip_wake_word(text))
    try:
        return re.search(pattern, value) is not None
    except re.error:
        return False


def select_enabled(agent_id):
    """True when the assistant exists and may be selected."""
    item = REGISTRY.get(agent_id)
    return bool(item and item.get("enabled"))


def known_agents():
    """Every assistant id, enabled or not."""
    return list(REGISTRY)


def enabled_agent_ids():
    return enabled_agents()
