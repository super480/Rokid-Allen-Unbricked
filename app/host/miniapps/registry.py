"""Static registry: voice phrases -> one app operation or one card action.

Matching is deterministic and whole-sentence. A bare "codex" or "token"
substring must never trigger a query, or asking an assistant about tokens
would open a card instead of getting an answer.

Every query phrase names its provider. The provider-less "查询用量" /
"查询剩余额度" fallbacks were removed on 2026-11-15: a second app
("Claude 额度") is planned, and those phrases would be ambiguous the day
it lands. Card actions such as "关闭卡片" stay provider-free on purpose --
they close whatever card happens to be on screen.

2026-11-15 (D038): the user-facing provider word is now "龙女", not "GPT".
"GPT" is also one of the device wake-word aliases, so saying "查询 GPT 额度"
can be cut up by the wake-word path before it ever reaches this table. A
Chinese provider word avoids that collision. Old GPT spellings are folded
onto "龙女" so anyone who still says them keeps working where the ASR does
deliver the text.

2026-11-15 (D040): two local Mac query apps join the same table --
"网络速度" (download/upload) and "系统资源" (CPU/RAM). Their phrases are
plain Chinese for the same wake-word reason; no Latin letters to mis-hear.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from . import bambu

APP_ID = "codex_usage"
APP_NAME = "Codex 用量"
PROVIDER = "龙女"
NETWORK_APP_ID = "network_speed"
NETWORK_APP_NAME = "网络速度"
RESOURCES_APP_ID = "system_resources"
RESOURCES_APP_NAME = "系统资源"

OPERATIONS = ("quota", "tokens", "overview")
OPERATIONS_BY_APP = {
    APP_ID: OPERATIONS,
    NETWORK_APP_ID: ("speed",),
    RESOURCES_APP_ID: ("resources",),
    bambu.APP_ID: (bambu.OPERATION,),
}
DEFAULT_OPERATION = {app: ops[0] for app, ops in OPERATIONS_BY_APP.items()}

APPS = [{
    "id": APP_ID,
    "name": APP_NAME,
    "kind": "query",
    "operations": list(OPERATIONS),
    "source": "codex_app_server",
    "present": True,
}, {
    "id": NETWORK_APP_ID,
    "name": NETWORK_APP_NAME,
    "kind": "query",
    "operations": list(OPERATIONS_BY_APP[NETWORK_APP_ID]),
    "source": "mac_local_metrics",
    "present": True,
}, {
    "id": RESOURCES_APP_ID,
    "name": RESOURCES_APP_NAME,
    "kind": "query",
    "operations": list(OPERATIONS_BY_APP[RESOURCES_APP_ID]),
    "source": "mac_local_metrics",
    "present": True,
}, {
    "id": bambu.APP_ID,
    "name": bambu.TITLE,
    "kind": "query",
    "operations": [bambu.OPERATION],
    "source": bambu.SOURCE,
    "present": True,
}]

# The factory ASR writes a short Chinese word more reliably than three
# Latin letters, but it may still pick a near-homophone or the much more
# common "小龙女". Fold those onto the canonical provider word.
PROVIDER_SPELLINGS = (
    "小龙女", "龙吕", "隆女", "笼女", "龙汝", "龙羽",
)
# Legacy "GPT" spellings, from the wake-word alias table
# (vt_word "GPT|gpt|计屁替|cpt|g p t|c p t") and the usual Chinese
# transcriptions of "ji pi ti". Kept as a compatibility fold only.
GPT_SPELLINGS = (
    "计屁替", "计屁踢", "计皮提", "鸡屁踢", "鸡屁提", "鸡皮踢",
    "几屁替", "几皮提", "基皮提", "吉皮提", "吉屁提", "寄皮提",
    "cpt", "gpt",
)
# Local-app wording folds: ASR variance for Chinese words, not new commands.
WORD_FOLDS = (
    ("中央处理器", "cpu"), ("处理器", "cpu"),
    ("网络速度", "网速"), ("网路速度", "网速"), ("上网速度", "网速"),
    ("运行内存", "内存"), ("运存", "内存"),
    ("一下", ""),
)

# (canonical phrase, app_id, operation, action, wants_today)
_PHRASES: list[tuple[str, str, str, str, bool]] = [
    # 龙女: the user-facing names (D038). "查询龙女额度" and
    # "查询龙女用量" are the two recommended ones; the rest are short
    # spoken variants the recogniser is likely to produce.
    ("查询龙女额度", APP_ID, "quota", "query", False),
    ("查询龙女用量", APP_ID, "quota", "query", False),
    ("查询龙女剩余额度", APP_ID, "quota", "query", False),
    ("查询一下龙女额度", APP_ID, "quota", "query", False),
    ("查询一下龙女用量", APP_ID, "quota", "query", False),
    ("查一下龙女额度", APP_ID, "quota", "query", False),
    ("查一下龙女用量", APP_ID, "quota", "query", False),
    ("查龙女额度", APP_ID, "quota", "query", False),
    ("查龙女用量", APP_ID, "quota", "query", False),
    ("看看龙女用量", APP_ID, "quota", "query", False),
    ("看看龙女额度", APP_ID, "quota", "query", False),
    ("龙女额度", APP_ID, "quota", "query", False),
    ("龙女用量", APP_ID, "quota", "query", False),
    ("龙女额度还剩多少", APP_ID, "quota", "query", False),
    ("龙女还剩多少额度", APP_ID, "quota", "query", False),
    ("查询龙女token用量", APP_ID, "tokens", "query", False),
    ("龙女累计用了多少token", APP_ID, "tokens", "query", False),
    ("龙女用了多少token", APP_ID, "tokens", "query", False),
    ("龙女今天用了多少token", APP_ID, "tokens", "query", True),
    ("龙女今日token用量", APP_ID, "tokens", "query", True),
    ("龙女用量总览", APP_ID, "overview", "query", False),
    ("看看龙女全部用量", APP_ID, "overview", "query", False),
    ("刷新龙女用量", APP_ID, "", "refresh", False),
    ("刷新龙女额度", APP_ID, "", "refresh", False),
    # Codex (the product name) stays as a compatibility alias for the
    # console and for anyone already used to saying it.
    ("查询codex额度", APP_ID, "quota", "query", False),
    ("查一下codex额度", APP_ID, "quota", "query", False),
    ("codex额度还剩多少", APP_ID, "quota", "query", False),
    ("codex还剩多少额度", APP_ID, "quota", "query", False),
    ("看看codex用量", APP_ID, "quota", "query", False),
    ("查询codex用量", APP_ID, "quota", "query", False),
    ("codex用量", APP_ID, "quota", "query", False),
    ("查询codextoken用量", APP_ID, "tokens", "query", False),
    ("codex累计用了多少token", APP_ID, "tokens", "query", False),
    ("codex用了多少token", APP_ID, "tokens", "query", False),
    ("codex今天用了多少token", APP_ID, "tokens", "query", True),
    ("codex今日token用量", APP_ID, "tokens", "query", True),
    ("codex用量总览", APP_ID, "overview", "query", False),
    ("看看codex全部用量", APP_ID, "overview", "query", False),
    ("刷新codex用量", APP_ID, "", "refresh", False),
    # D040: network speed (Mac counters, last-second deltas). All-Chinese
    # phrases -- "GPT" taught us that Latin letters collide with wake words.
    ("查询网速", NETWORK_APP_ID, "speed", "query", False),
    ("查网速", NETWORK_APP_ID, "speed", "query", False),
    ("看看网速", NETWORK_APP_ID, "speed", "query", False),
    ("网速多少", NETWORK_APP_ID, "speed", "query", False),
    ("网速怎么样", NETWORK_APP_ID, "speed", "query", False),
    ("现在网速多少", NETWORK_APP_ID, "speed", "query", False),
    ("查询下载速度", NETWORK_APP_ID, "speed", "query", False),
    ("查询上传速度", NETWORK_APP_ID, "speed", "query", False),
    ("查询下载和上传速度", NETWORK_APP_ID, "speed", "query", False),
    ("查询网络状态", NETWORK_APP_ID, "speed", "query", False),
    ("刷新网速", NETWORK_APP_ID, "", "refresh", False),
    # D040: CPU / memory. "cpu" is folded from 处理器/中央处理器 above.
    ("查询系统资源", RESOURCES_APP_ID, "resources", "query", False),
    ("查系统资源", RESOURCES_APP_ID, "resources", "query", False),
    ("看看系统资源", RESOURCES_APP_ID, "resources", "query", False),
    ("查询系统状态", RESOURCES_APP_ID, "resources", "query", False),
    ("查询电脑状态", RESOURCES_APP_ID, "resources", "query", False),
    ("查询电脑资源", RESOURCES_APP_ID, "resources", "query", False),
    ("查询资源占用", RESOURCES_APP_ID, "resources", "query", False),
    ("查询cpu占用", RESOURCES_APP_ID, "resources", "query", False),
    ("查询cpu和内存", RESOURCES_APP_ID, "resources", "query", False),
    ("查询cpu内存", RESOURCES_APP_ID, "resources", "query", False),
    ("查cpu内存", RESOURCES_APP_ID, "resources", "query", False),
    ("查询内存占用", RESOURCES_APP_ID, "resources", "query", False),
    ("查询内存用量", RESOURCES_APP_ID, "resources", "query", False),
    ("查看内存占用", RESOURCES_APP_ID, "resources", "query", False),
    ("查cpu", RESOURCES_APP_ID, "resources", "query", False),
    ("看看cpu", RESOURCES_APP_ID, "resources", "query", False),
    ("刷新系统资源", RESOURCES_APP_ID, "", "refresh", False),
    ("查询打印进度", bambu.APP_ID, bambu.OPERATION, "query", False),
    ("查看打印进度", bambu.APP_ID, bambu.OPERATION, "query", False),
    ("打印还要多久", bambu.APP_ID, bambu.OPERATION, "query", False),
    ("刷新打印进度", bambu.APP_ID, "", "refresh", False),
    # Card actions are provider-independent on purpose.
    ("关闭卡片", APP_ID, "", "hide", False),
    ("关掉卡片", APP_ID, "", "hide", False),
    ("退出用量卡片", APP_ID, "", "hide", False),
    ("关闭用量卡片", APP_ID, "", "hide", False),
    ("关掉用量卡片", APP_ID, "", "hide", False),
]


def canonical(text: str) -> str:
    """Fold width, case, punctuation, whitespace and provider spellings.

    The recogniser may write "查询 龙 女 额度", "查询小龙女额度" or
    "查询 G P T 额度" for the same command. All of them become
    "查询龙女额度" before the whole-sentence lookup, so the finite phrase
    table does not need a copy of every spelling.
    """
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[，。！？、,.!?:;：；~～\-—_/\\'\"“”‘’()（）\[\]【】<>《》]",
                   "", value)
    # "查询龙女的额度" is the same request as "查询龙女额度".
    value = value.replace("的", "")
    for spelling in PROVIDER_SPELLINGS + GPT_SPELLINGS:
        value = value.replace(spelling, PROVIDER)
    for source, target in WORD_FOLDS:
        value = value.replace(source, target)
    return value


# Keys are canonical too, so a phrase is stored and looked up the same way.
RULES = {canonical(phrase): (app, operation, action, today)
         for phrase, app, operation, action, today in _PHRASES}


@dataclass(frozen=True)
class Match:
    app_id: str
    operation: str
    action: str          # query | refresh | hide
    wants_today: bool
    phrase: str

    def to_dict(self) -> dict:
        return {"app_id": self.app_id, "operation": self.operation,
                "action": self.action, "wants_today": self.wants_today,
                "phrase": self.phrase}


def match_key(text: str) -> str:
    """The exact whole-sentence lookup key, shared by match and by saving.

    A phrase pasted into the console may carry "若琪，" or punctuation;
    it must fold to the SAME key as the words the recogniser delivers.
    """
    key = canonical(text)
    # The recogniser may leave the factory wake word in front of the phrase.
    key = re.sub(r"^(?:若琪|若弃|洛奇|罗琪)", "", key)
    # Some builds repeat the provider word in front of the command. Only
    # strip it when a command verb follows, so "龙女用量" stays valid.
    key = re.sub(r"^" + PROVIDER + r"(?=查询|查一下|查|看看|刷新)", "", key)
    return key


def match(text: str, extra_rules: dict | None = None) -> Match | None:
    """Return a match only for an exact canonical phrase.

    ``extra_rules`` holds the console-defined phrases owned by ONE service
    instance: canonical phrase -> (app, operation, action, wants_today).
    Built-in rules win, so a custom phrase can never silently break a
    system command. It is an argument rather than module-global state, so
    two services (or two tests) never overwrite each other's phrases.
    """
    key = match_key(text)
    if not key:
        return None
    if key in RULES:
        app, operation, action, today = RULES[key]
        return Match(app, operation, action, today, key)
    if extra_rules and key in extra_rules:
        app, operation, action, today = extra_rules[key]
        return Match(app, operation, action, today, key)
    return None


def phrases() -> list[str]:
    """The finite phrase list a host could push to the device later."""
    return sorted(RULES)


def list_apps() -> list[dict]:
    return [dict(app) for app in APPS]


def operations_for(app_id: str) -> tuple[str, ...]:
    return OPERATIONS_BY_APP.get(app_id, ())


def default_operation(app_id: str) -> str:
    return DEFAULT_OPERATION.get(app_id, "quota")


# --- Console-defined voice phrases (2026-11-16) -----------------------------
# The user can add their own spoken wording per card from
# 控制台 · 设置 · 小应用. They live in config["miniapps"]["voice_phrases"]
# and take effect on save, with no host restart. Matching stays exactly
# whole-sentence: a custom phrase is just another canonical key.

MAX_CUSTOM_PHRASES_PER_APP = 12
MAX_CUSTOM_PHRASE_CHARS = 40


def split_custom_phrases(text: str) -> list[str]:
    """One console text box; phrases separated by comma / newline / |."""
    return [part.strip() for part
            in re.split(r"[,，、;；|\n\r]+", str(text or ""))
            if part.strip()]


def normalise_custom_phrases(mapping: dict | None) -> dict[str, list[str]]:
    """Clean a ``{app_id: [phrase, ...]}`` mapping from the config file."""
    cleaned: dict[str, list[str]] = {}
    if not isinstance(mapping, dict):
        return cleaned
    for app_id, values in mapping.items():
        if app_id not in OPERATIONS_BY_APP:
            continue
        if isinstance(values, str):
            values = split_custom_phrases(values)
        seen: list[str] = []
        for value in list(values or []):
            phrase = str(value or "").strip()
            if (not phrase or len(phrase) > MAX_CUSTOM_PHRASE_CHARS
                    or phrase in seen):
                continue
            seen.append(phrase)
            if len(seen) >= MAX_CUSTOM_PHRASES_PER_APP:
                break
        if seen:
            cleaned[app_id] = seen
    return cleaned


def build_custom_rules(mapping: dict | None) -> dict[str, tuple]:
    """canonical phrase -> (app, operation, action, wants_today).

    Every custom phrase opens its app's DEFAULT operation; the finite
    built-in table remains the only place where tokens/overview/refresh
    are wired, so a typo in the console cannot invent a new command.
    """
    rules: dict[str, tuple] = {}
    for app_id, phrases in normalise_custom_phrases(mapping).items():
        for phrase in phrases:
            key = match_key(phrase)
            if key and key not in rules:
                rules[key] = (app_id, DEFAULT_OPERATION[app_id],
                              "query", False)
    return rules


def builtin_phrases_for(app_id: str) -> list[str]:
    """Built-in QUERY phrases for one card (for the console listing)."""
    return sorted(phrase for phrase, rule in RULES.items()
                  if rule[0] == app_id and rule[2] == "query")


def card_action_phrases() -> list[str]:
    """The provider-independent refresh / hide phrases."""
    return sorted(phrase for phrase, rule in RULES.items()
                  if rule[2] != "query")
