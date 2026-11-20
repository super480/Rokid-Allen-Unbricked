"""Voice routing gate for Home Assistant turns.

The bridge does NOT judge which device or which action: the whole
sentence is handed to HA's conversation agent. This module answers only
one question -- "is this a home-control turn at all?" -- so the sentence
is not swallowed by the volume rule below it and chat is not stolen from
the assistant above it.

Dual gate, both conditions required:

    1. a home verb (打开 / 关掉 / 调到 / 开着吗 / ...), and
    2. a home word: a device-type word (灯/空调/窗帘/...), a room word
       (客厅/主卧/...), or a user-configured alias (config
       ``homeassistant.aliases`` values + a free-form extra list).

"调到 50%" has no home word -> the volume rule keeps it. "书房灯" alone
has no verb -> nobody takes it. Per-entity names are deliberately NOT
the gate vocabulary: "黑板灯" is captured by the type word "灯", the AI
resolves which device from the raw sentence. A question of explanation
("怎么打开书房灯") stays with the assistant; "吗"-style queries
("书房灯开着吗") are legal HA turns.

The same regex is pushed to the device (SpeechGate.haRule) so the
factory speaker stays silent for turns the host will answer. It is a
lookahead AND that both Java's ``Matcher.find()`` and Python's
``search()`` understand:

    (?=.{0,N}VERB)(?=.{0,N}HOMES)

Bounded at MAX_RULE_CHARS because the rule travels inside the device
status poll; longer rules are truncated at an alternation boundary and
the host keeps the full form for its own matching.
"""
from __future__ import annotations

import re

# Forced-override prefix: still accepted so a turn can be aimed at HA
# explicitly ("智能家居，把加湿器打开"), but everyday speech does not
# need it (user decision 2026-11-18: no prefix, direct takeover).
FORCED_PREFIXES = ("智能家居", "智能家具", "家居")

# Finite verb vocabulary. Deliberately excludes bare 调大/调小/声音/大点声
# -- those are volume words and must stay with the music chain. The
# X成/X到/X为 family (调成/设成/换成/...) is bounded on purpose: bare
# 调/设/换/改 would swallow 「把这首歌改成单曲循环」-style turns.
HOME_VERBS = (
    "打开", "开启", "开一下", "开开", "关掉", "关闭", "关上", "关一下",
    "调亮", "调暗", "调到", "调高", "调低", "亮一点", "暗一点",
    "调成", "调至", "调为", "设成", "设到", "设为", "设置成",
    "换成", "换到", "换为", "改成", "改到", "改为", "变成", "开到",
    "拉开", "拉上", "合上",
    "开着吗", "关着吗", "开着没", "关着没", "开没开", "关没关",
    "多少度", "几度", "温度", "湿度",
    "开灯", "关灯",
    "电量", "开机", "关机", "待机", "上锁", "锁上", "锁门", "解锁", "开锁",
    "启动", "启用", "退出", "停用", "执行", "闭合", "开关状态",
)

# Device-type words. A substring hit is enough: "黑板灯" is caught by
# "灯", so every lamp in the house is covered without an entity list.
DEVICE_WORDS = (
    "灯", "空调", "空凋", "地暖", "暖气",
    "窗帘", "布帘", "纱帘", "卷帘", "梦幻帘",
    "风扇", "凉霸", "新风", "浴霸", "油烟机",
    "加湿器", "净化器", "扫地机", "机器人", "插座", "开关",
    "洗衣机", "烘干机", "电器", "门锁", "入户门", "场景", "观影模式", "离家模式",
    "回家模式", "睡眠模式", "apple tv", "appletv", "苹果tv", "苹果电视", "苹果盒子",
)

# Room / place words: "主卧多少度" has no device word but is a query.
ROOM_WORDS = (
    "客厅", "主卧", "次卧", "卧室", "书房", "厨房", "卫生间", "浴室",
    "阳台", "餐厅", "吧台", "玄关", "走廊", "花园", "阁楼", "地下室",
    "茶几", "床头", "镜前",
)

# Sentence is a request for explanation or code -> assistant. Narrower
# than the music question list on purpose: "吗"/"呢" stay legal here.
QUESTION_MARKERS = (
    "怎么", "为什么", "怎么回事", "如何", "解释", "讲讲", "说说",
    "写个", "写一", "写个脚本", "代码", "脚本", "程序",
)

# "Is it on / what is the reading" turns. Used to label the result card
# 查询 instead of 指令 -- the gate already routes them to HA either way.
QUERY_MARKERS = ("吗", "没", "多少度", "几度", "温度", "湿度",
                 "开着", "关着", "状态", "电量")


def looks_like_query(text: str) -> bool:
    """True for state questions ("书房灯开着吗" / "主卧多少度")."""
    return any(marker in text for marker in QUERY_MARKERS)

# Longer sentences are chat / storytelling, not one home-control turn.
MAX_GATE_LEN = 30

# The device-side regex cap (~verbs + home words + extras + syntax).
MAX_RULE_CHARS = 512

# How far a verb and a home word may sit apart inside one sentence.
_SPAN = 16


def strip_forced_prefix(text: str) -> tuple[bool, str]:
    """Return (forced, remainder) for the explicit HA prefix."""
    value = (text or "").strip()
    for prefix in FORCED_PREFIXES:
        if value.startswith(prefix):
            rest = value[len(prefix):].lstrip("，, 　、")
            return True, rest
    return False, value


def question_excluded(text: str) -> bool:
    """True when the sentence wants an explanation, not a command."""
    return any(marker in text for marker in QUESTION_MARKERS)


def _alternation(words) -> str:
    # Longest first so "开着吗" wins over "开" inside the alternation.
    return "|".join(re.escape(w) for w in sorted(set(words), key=len,
                                                 reverse=True))


def build_gate_regex(extra_aliases: list[str] | None = None
                     ) -> tuple[str, list[str]]:
    """Compile the dual-gate regex. Returns (pattern, home_words_used).

    ``extra_aliases`` are user-configured spoken names (config aliases
    values + extras); they join DEVICE_WORDS/ROOM_WORDS as gate words.
    The whole pattern is kept inside MAX_RULE_CHARS at build time: core
    words always fit, extras are added while space remains. Truncating
    afterwards (fit_device_rule) is only a guard -- a rule cut inside
    the verb table would degrade to verb-only and swallow music turns.
    """
    extras: list[str] = []
    for name in extra_aliases or []:
        value = (name or "").strip()
        if value and value not in extras:
            extras.append(value)
    verb_alt = _alternation(HOME_VERBS)

    def assemble(home_alt: str) -> str:
        return r"(?i)(?=[\s\S]{0,%d}(?:%s))(?=[\s\S]{0,%d}(?:%s))" % (
            _SPAN, verb_alt, _SPAN, home_alt)

    budget = MAX_RULE_CHARS - len(assemble(""))
    used: list[str] = []
    size = 0

    def add(word: str) -> bool:
        nonlocal size
        cost = len(re.escape(word)) + (1 if used else 0)
        if size + cost > budget:
            return False
        used.append(word)
        size += cost
        return True

    # Core vocabulary first: the house must stay covered even when a
    # pile of user aliases would blow the device-rule budget.
    core = list(DEVICE_WORDS) + list(ROOM_WORDS)
    for word in sorted(set(core), key=len, reverse=True):
        add(word)
    for word in sorted(set(extras), key=len, reverse=True):
        add(word)
    if not used:
        # An empty home group would make the lookahead always succeed,
        # i.e. verb-only. Refuse instead: gate never matches.
        return "", []
    return assemble("|".join(re.escape(w) for w in used)), used


def fit_device_rule(pattern: str) -> str:
    """Shrink a gate regex for the status poll; empty means 'never match'.

    HA switched off -> callers pass "" and get "" back, so the device
    reverts to factory behaviour with no stale rule left behind.
    build_gate_regex() already respects MAX_RULE_CHARS, so this is a
    guard: cut at an alternation boundary, close the two open groups
    and keep only something that compiles. Anything else -> "" (never
    match), never a half-broken rule the device would silently keep.
    """
    if not pattern:
        return ""
    if len(pattern) <= MAX_RULE_CHARS:
        return pattern
    cut = pattern[:MAX_RULE_CHARS]
    while cut:
        bar = cut.rfind("|")
        if bar <= 0:
            break
        candidate = cut[:bar] + "))"
        try:
            re.compile(candidate)
            return candidate
        except re.error:
            cut = cut[:bar]
    return ""


class Gate:
    """Dual-condition matcher. Stateless besides the compiled regex."""

    def __init__(self, extra_aliases: list[str] | None = None):
        self.pattern, self.home_words = build_gate_regex(extra_aliases)
        self._regex = re.compile(self.pattern) if self.pattern else None

    @property
    def device_rule(self) -> str:
        """The regex string the device should hold (bounded)."""
        return fit_device_rule(self.pattern)

    def matches(self, text: str) -> bool:
        if self._regex is None:
            return False
        return self._regex.search(text) is not None

    def route(self, text: str) -> tuple[bool, str]:
        """Decide one turn. Returns (take_it, sentence_for_ha).

        take_it False -> the sentence continues down the normal chain
        (volume / miniapps / assistant). The forced prefix takes the
        turn even when the gate would not (explicit override).
        """
        forced, rest = strip_forced_prefix(text)
        if not rest:
            return False, ""
        if forced:
            return True, rest
        if len(rest) > MAX_GATE_LEN:
            return False, ""
        if question_excluded(rest):
            return False, ""
        if not self.matches(rest):
            return False, ""
        return True, rest
