"""Screen and speech presentation for Home Assistant results."""
from __future__ import annotations

import re
import unicodedata

from miniapps.models import validate_card

from .models import HaResult
from .routing import ROOM_WORDS

# Spoken confirmations are brief (user decision: 控制结果默认播报).
MAX_SPEECH = 60

# What the row says per internal status when the AI gave no usable text.
FALLBACK_SPEECH = {
    "reported_success": "已发送执行",
    "partial": "部分完成",
    "failed": "没有执行成功",
    "unknown": "没有收到执行结果",
    "queued": "已排队",
    "running": "正在执行",
}


def speech_text(result: HaResult) -> str:
    """The one line to speak (already capped)."""
    message = (result.message or "").strip()
    if not message:
        message = FALLBACK_SPEECH.get(result.status, "")
    if result.verification == "state_mismatch":
        message = (message + "，没有确认到设备变化").strip("，，")
    if len(message) > MAX_SPEECH:
        message = message[:MAX_SPEECH - 1] + "…"
    return message


# All of these matches choose artwork after HA has processed the original turn.
# Their output is never used to decide which devices or services to call.
_SWITCH = re.compile(r"(?P<on>打开|开启|开一下|开开|开灯|开机)|"
                     r"(?P<off>关掉|关闭|关上|关一下|关灯|关机|待机)")
_LOCK = re.compile(r"(?P<on>上锁|锁上|锁门|锁好|反锁|(?:关闭|关上)门锁)|"
                   r"(?P<off>解锁|开锁|打开门锁|开门锁)")
_CURTAIN = re.compile(r"(?P<on>拉开|打开|开启)|(?P<off>拉上|合上|闭合|关上|关掉|关闭)")
_SCENE = re.compile(r"(?P<on>启动|启用|执行|激活|进入|打开|开启)|"
                    r"(?P<off>退出|停用|结束|关闭|关掉)")
_MULTI = re.compile(r"然后|同时|并且|并把|再把|再关|再开")
_DENIAL = re.compile(r"失败|无法|不能|没能|未能|没有|未确认|不确定|"
                     r"可能|试试|是否|尚未|请确认|并未|未知|不可用|准备|即将|稍后|"
                     r"未(?:打开|关闭|上锁|解锁)")
_ACK = re.compile(r"已(?:经)?|成功|结果为|当前|现在|了[。！!，,]?$")
_NUMBER = r"(?<![\d.\-+])(?<!\d,)([-+]?\d{1,3}(?:\.\d{1,2})?)\s*"
_TEMPERATURE = re.compile(_NUMBER + r"(°\s*[CFcf]|摄氏度|华氏度|度(?!电))")
_PERCENT = re.compile(_NUMBER + r"%")
_UNCERTAIN_VALUE = re.compile(r"大约|约|大概|左右|"
                              r"\d\s*(?:%|度|°[CF])?\s*(?:至|到|~|–|—|-)\s*[-+]?\d")
_ADJUST = re.compile(r"调[成到至为高低亮暗节整]|设置?|设[成到为]|"
                     r"换[成到为]|改[成到为]|变成|开到")
_TEMPERATURE_WORD = re.compile(r"温度|温湿度|多少度|几度")
_LIGHT = re.compile(r"灯|灯光")
_BRIGHTNESS = re.compile(r"亮度|调亮|调暗|亮一点|暗一点")
_WARM = re.compile(r"暖(?:色|光|白)|黄(?:色|光)|橙(?:色|光)|红(?:色|光)")
_COOL = re.compile(r"冷(?:色|光|白)|蓝(?:色|光)|青(?:色|光)|冰白")
_COLOR = re.compile(r"颜色|色温|彩色")
_APPLE = re.compile(r"apple\s*tv|苹果\s*(?:tv|电视|盒子)", re.I)
_APPLIANCE = re.compile(r"插座|开关|电器|风扇|凉霸|新风|浴霸|油烟机|加湿器|"
                        r"净化器|扫地机|机器人|地暖|暖气|洗衣机|烘干机")
_AC = re.compile(r"空调|空凋")
_CURTAIN_WORD = re.compile(r"窗帘|布帘|纱帘|卷帘|梦幻帘")
_SCENE_WORD = re.compile(r"场景|观影模式|离家模式|回家模式|睡眠模式")
_STATE_QUERY = re.compile(r"状态|开着|关着|开了[吗没]|关了[吗没]|开没开|关没关|开关")
_STATE_ON = re.compile(r"开启|打开|开着|开[了。]|开机|运行中|已通电")
_STATE_OFF = re.compile(r"关闭|关掉|关着|关[了。]|关机|待机|已断电|没开|没亮")


def result_card(result: HaResult, *, sentence: str = "") -> dict:
    """Project a confirmed reply or readable fallback to one or two centred pages."""
    message = " ".join((result.message or "").split())
    detail = message or FALLBACK_SPEECH.get(result.status, "没有收到执行结果")
    status = result.card_status
    pages = []
    if status == "ok" and message and not _DENIAL.search(message):
        pages = _specialized_pages(result, sentence, message)
    fallback = {
        "ok": "已收到回复", "stale": "未确认", "error": "执行失败",
        "partial": "部分完成", "loading": "进行中",
    }
    if not pages:
        light_switch = bool(_LIGHT.search(sentence) and _SWITCH.search(sentence)
                            and not _ADJUST.search(sentence)
                            and result.kind == "control" and "插座" not in sentence)
        confirmed = result.verification == "state_confirmed"
        if status == "ok" and (_DENIAL.search(message)
                               or (light_switch and not confirmed)):
            status = "stale"
        icon = "lightbulb-outline" if light_switch else "home-outline"
        pages = [_page(detail, fallback[status], icon)]
    card = {
        "template": "lines", "layout": "home_feedback", "title": "智能家居",
        "status": status, "rows": pages, "notice": detail[:120],
    }
    validate_card(card)
    return card


def _page(label: str, value: str, icon: str, **fields) -> dict:
    return {"label": label[:120], "value": value,
            "icon": f"ha-{icon}.png", **fields}


def _room_caption(sentence: str, metric: str) -> str:
    rooms = [room for room in ROOM_WORDS if room in sentence]
    return f"{rooms[0]} · {metric}" if len(rooms) == 1 else metric


def _temperature(message: str) -> tuple[str, str] | None:
    matches = _TEMPERATURE.findall(message)
    if len(matches) != 1 or _UNCERTAIN_VALUE.search(message):
        return None
    value, unit = matches[0]
    return value, "°F" if unit == "华氏度" or unit.upper().endswith("F") else "°C"


def _percent(message: str, metric: str, *, allow_bare: bool = True) -> str | None:
    if _UNCERTAIN_VALUE.search(message):
        return None
    matches = re.findall(rf"(?:{metric})\s*(?:已(?:经)?)?\s*"
                         r"(?:是|为|还剩|还有|剩余|(?:设置?|调(?:整|节)?)[为到成]|[:：])?\s*"
                         + _NUMBER + "%", message)
    if not matches and allow_bare:
        bare = _PERCENT.fullmatch(message.rstrip("。."))
        matches = [bare.group(1)] if bare else []
    if len(matches) == 1 and 0 <= float(matches[0]) <= 100:
        return matches[0]
    return None


def _name(result: HaResult, sentence: str, default: str, action=None) -> str:
    names = [target.get("name") for target in result.targets if target.get("name")]
    if names:
        return "、".join(names[:3])
    if action:
        sentence = sentence[:action.start()] + sentence[action.end():]
        sentence = re.sub(r"^(?:请|帮我|把|给我?)+|(?:一下|吧|谢谢)+$", "", sentence.strip())
        if sentence:
            return sentence
    return default


def _direction(pattern, text: str) -> str | None:
    matches = {match.lastgroup for match in pattern.finditer(text)}
    return matches.pop() if len(matches) == 1 else None


def _toggle(result, sentence, message, verbs, label, enabled, disabled):
    action = verbs.search(sentence)
    if not action or _MULTI.search(sentence) or not _ACK.search(message):
        return []
    direction = _direction(verbs, sentence)
    reply_direction = _direction(verbs, message)
    if verbs == _SWITCH:
        on, off = bool(_STATE_ON.search(message)), bool(_STATE_OFF.search(message))
        if on and off:
            return []
        if on != off:
            reply_direction = "on" if on else "off"
    if not direction or direction != reply_direction:
        return []
    icon, text = enabled if direction == "on" else disabled
    name = label if label == "Apple TV" else _name(result, sentence, label, action)
    return [_page(name, text, icon, tone="off" if direction == "off" else "normal")]


def _query_pages(result, sentence, message):
    if "电量" in sentence:
        reading = _percent(message, r"电量|电池")
        if reading is not None:
            icon = "battery-20" if float(reading) <= 20 else "battery-70"
            return [_page(_name(result, sentence, "电量查询"), reading, icon,
                          unit="%", level=float(reading))]
        return []

    if _TEMPERATURE_WORD.search(sentence) or "湿度" in sentence:
        if _ADJUST.search(message) or sum(room in sentence for room in ROOM_WORDS) > 1:
            return []
        temperature = _temperature(message)
        humidity = _percent(message, "湿度", allow_bare=not _TEMPERATURE_WORD.search(sentence))
        pages = []
        if temperature and (_TEMPERATURE_WORD.search(sentence) or "温度" in message):
            value, unit = temperature
            pages.append(_page(_room_caption(sentence, "温度"), value, "thermometer", unit=unit))
        if humidity is not None and ("湿度" in sentence or "湿度" in message):
            pages.append(_page(_room_caption(sentence, "湿度"), humidity, "water-percent", unit="%"))
        return pages

    if _STATE_QUERY.search(sentence):
        on, off = bool(_STATE_ON.search(message)), bool(_STATE_OFF.search(message))
        if on != off:
            label = _name(result, sentence, "开关状态")
            return [_page(label, "当前开启" if on else "当前关闭",
                          "toggle-switch" if on else "toggle-switch-off-outline",
                          tone="normal" if on else "off")]
    return []


def _specialized_pages(result: HaResult, sentence: str, message: str) -> list[dict]:
    sentence = unicodedata.normalize("NFKC", sentence).strip(" ，。？！,.?!")
    message = unicodedata.normalize("NFKC", message).replace("−", "-")
    domains = {target.get("entity_id", "").split(".", 1)[0] for target in result.targets}
    adjust = _ADJUST.search(sentence)
    if result.kind == "query" and not adjust:
        return _query_pages(result, sentence, message)
    if len(domains) > 1:
        return []

    if domains == {"light"} or (not domains and _LIGHT.search(sentence)
                                and "插座" not in sentence):
        brightness = bool(_BRIGHTNESS.search(sentence))
        color = bool(_COLOR.search(sentence) or _WARM.search(sentence) or _COOL.search(sentence))
        if brightness or color:
            if not _ACK.search(message):
                return []
            label = _name(result, sentence[:adjust.start()] if adjust else sentence, "灯光")
            if label == "灯光" and adjust:
                prefix = re.sub(r"^(?:请|帮我|把|给我)+", "", sentence[:adjust.start()].strip())
                label = prefix or label
            pages = []
            if color:
                warm, cool = bool(_WARM.search(message)), bool(_COOL.search(message))
                if warm != cool:
                    pages.append(_page(label + " · 颜色", "暖色" if warm else "冷色",
                                       "palette-outline", swatch="warm" if warm else "cool"))
            if brightness:
                reading = _percent(message, "亮度", allow_bare=False)
                if reading is not None:
                    pages.append(_page(label + " · 亮度", reading, "brightness-6",
                                       unit="%", level=float(reading)))
            return pages if len(pages) == int(color) + int(brightness) else []
        if not adjust and (not domains or domains == {"light"}):
            return _toggle(result, sentence, message, _SWITCH, "灯光",
                           ("lightbulb-on", "已开启"), ("lightbulb-off-outline", "已关闭"))
        return []

    if _AC.search(sentence) or domains == {"climate"}:
        temperature = _temperature(message)
        if adjust:
            if temperature and _ACK.search(message):
                value, unit = temperature
                label = _name(result, sentence, _room_caption(sentence, "空调"))
                return [_page(label + " · 设定温度", value, "air-conditioner", unit=unit)]
            return []
        return _toggle(result, sentence, message, _SWITCH, "空调",
                       ("air-conditioner", "已开启"), ("power-off", "已关闭"))

    if _APPLE.search(sentence) or any(_APPLE.search(t.get("name", "")) for t in result.targets):
        return _toggle(result, sentence, message, _SWITCH, "Apple TV",
                       ("apple-tv", "已开机"), ("apple-tv", "已进入待机"))
    if _LOCK.search(sentence) or domains == {"lock"}:
        return _toggle(result, sentence, message, _LOCK, "门锁",
                       ("lock", "已上锁"), ("lock-open-variant-outline", "已解锁"))
    if _CURTAIN_WORD.search(sentence) or domains == {"cover"}:
        return _toggle(result, sentence, message, _CURTAIN, "窗帘",
                       ("curtains", "已打开"), ("curtains-closed", "已闭合"))
    if _SCENE_WORD.search(sentence) or domains == {"scene"}:
        return _toggle(result, sentence, message, _SCENE, "场景",
                       ("movie-open-outline", "已启动"), ("movie-off-outline", "已退出"))
    if _APPLIANCE.search(sentence) or domains in ({"switch"}, {"fan"}, {"humidifier"}):
        return _toggle(result, sentence, message, _SWITCH, "电器",
                       ("power-plug", "已开启"), ("power-plug-off-outline", "已关闭"))
    return []
