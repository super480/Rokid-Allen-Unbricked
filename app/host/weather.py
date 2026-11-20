"""The weather skill, rebuilt around the retired Rokid backend.

Rokid's cloud no longer serves weather: the API domain
(``weather-api.rokid.com``) has no DNS record any more, and asking the
speaker about the weather now draws the canned service-expiry notice.
The native weather app (``com.rokid.weather1``) itself is fine -- it
renders its card, animates it and speaks with the factory voice; it only
cannot fetch data.

This module puts the two halves back together, entirely on the LAN:

* a voice/text service that opens the native app over ADB with the NLP
  extra it expects (no APK change, no cloud NLP), and
* a local HTTPS replacement for the retired weather API, which the app
  reaches after the device wiring points the old hostname at this Mac
  (see ``scripts/weather_device_setup.py``: hosts entry, iptables DNAT
  and a trusted local CA).

Data comes from the public internet with automatic failover: Open-Meteo
first (best hourly resolution, no key), then wttr.in (also no key, a
different network path -- observed to survive days when Open-Meteo's
edge is unreachable from this LAN). The response is shaped exactly like
the bean classes in the app: ``code`` plus ``result`` with ``tts``,
``locCity`` and ``weatherUIInfos`` -- the original server composed the
spoken line too, so this service writes it as well.
"""
from __future__ import annotations

import os

from app_store import is_installed

import json
import re
import subprocess
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

# The D044 normalisation, shared with the mini-apps so the console and
# the recogniser fold a custom phrase to the same key.
from miniapps.registry import (MAX_CUSTOM_PHRASE_CHARS,  # noqa: E402
                               MAX_CUSTOM_PHRASES_PER_APP, match_key,
                               split_custom_phrases)

#: The API path hardcoded in WeatherModelImpl (v0.28.3).
API_PATH = "/weather-api/http/alien/weather/getWeather"

#: The native app and its single activity (exported, MAIN/LAUNCHER).
WEATHER_ACTIVITY = "com.rokid.weather1/.ui.view.MainActivity"

#: The NLP extra the app reads (RKExecutor puts the same key).
NLP_EXTRA = "nlp"

#: The domain MainActivity.checkNlpBean() demands.
NLP_DOMAIN = "com.rokid.weather1"

#: The hostname the device must be pointed at this host for.
API_HOST = "weather-api.rokid.com"

#: Voice turns that open the weather card. The set is the one the
#: factory's own rule used to own ("天气|气温|下雨"), plus the natural
#: temperature and snow phrasings the factory rule never needed.
PHRASE = re.compile(r"(天气|气温|温度|多少度|下雨|下雪|降温|冷不冷|热不热)")

#: The words the factory's own rule used to claim. While the override
#: switch is OFF, engine.factory_pattern() rejoins them into the stored
#: factory rule at runtime -- the stored config stays clean, and the
#: factory answers weather turns exactly the way it did before the
#: rebuilt skill.
FACTORY_WEATHER_WORDS = "天气|气温|下雨"

#: The keyword list the console card shows as its 内置说法. Unlike the
#: mini-apps' whole-sentence keys these match by substring.
BUILTIN_KEYWORDS = ["天气", "气温", "温度", "多少度", "下雨", "下雪",
                    "降温", "冷不冷", "热不热"]

# WMO weather code (Open-Meteo) -> (Yahoo icon code the app understands,
# the Chinese condition for the spoken line).
WMO_TO_APP = {
    0: (32, "晴"), 1: (34, "晴间少云"), 2: (30, "多云转晴"), 3: (26, "阴"),
    45: (20, "有雾"), 48: (20, "有雾凇"),
    51: (9, "有小毛毛雨"), 53: (9, "有毛毛雨"), 55: (9, "毛毛雨"),
    56: (8, "有冻毛毛雨"), 57: (8, "有冻毛毛雨"),
    61: (11, "有小雨"), 63: (12, "有雨"), 65: (40, "有大雨"),
    66: (10, "有冻雨"), 67: (10, "有冻雨"),
    71: (14, "有小雪"), 73: (16, "有雪"), 75: (42, "有大雪"), 77: (14, "有米雪"),
    80: (39, "有阵雨"), 81: (12, "有阵雨"), 82: (40, "有强阵雨"),
    85: (41, "有阵雪"), 86: (41, "有阵雪"),
    95: (37, "有雷阵雨"), 96: (4, "有雷阵雨伴冰雹"), 99: (4, "有雷阵雨伴冰雹"),
}
WMO_UNKNOWN_APP_ICON = 44  # "not available" -- the app has a placeholder.

CACHE_TTL = 300.0
OPEN_METEO_TIMEOUT = 8.0

#: The built-in data sources; the console card can replace either URL.
DEFAULT_API_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_WTTR_URL = "https://wttr.in"

# The factory reports these coordinates when the speaker has no real
# location: they are the well-known "centre of China" fallback that IP
# databases ship. A point within a hundredth of a degree of them means
# "no coordinates", and the city has to be geocoded instead.
FALLBACK_LAT = 35.86166
FALLBACK_LON = 104.195397


def _real_coordinates(lat: str, lon: str) -> tuple[str, str]:
    """(lat, lon) minus the factory's country-centroid default value."""
    try:
        if (abs(float(lat) - FALLBACK_LAT) < 0.01
                and abs(float(lon) - FALLBACK_LON) < 0.01):
            return "", ""
    except (ValueError, TypeError):
        return "", ""
    return lat, lon


def is_weather_query(text: str) -> bool:
    """True for the turns this service owns."""
    return bool(PHRASE.search(text or ""))


def app_result(city: str, icon: int, condition: str,
               temp: float, low: float, high: float) -> dict:
    """Build the ResponseBean-shaped dict the native app parses.

    Field names come from the app's bean classes (Gson matches them
    exactly): ``code``, ``result.tts``, ``result.locCity``,
    ``result.uiFlag`` and ``result.weatherUIInfos[i]`` with the icon as a
    Yahoo code and ``dataTemp`` 1 = plain temperature.
    """
    temp_i, low_i, high_i = int(round(temp)), int(round(low)), int(round(high))
    spoken = "%s今天%s，%d度，最高%d度，最低%d度。" % (
        city, condition or "天气未知", temp_i, high_i, low_i)
    if icon in (4, 8, 9, 10, 11, 12, 37, 39, 40, 45):
        spoken += "出门记得带伞。"
    return {
        "code": "OK", "msg": "", "msgtype": "",
        "result": {
            "intent": "query_weather", "locCity": city, "tts": spoken,
            "uiFlag": "",
            "weatherUIInfos": [{
                "time": "今天", "icon": icon, "valueMain": temp_i,
                "valueleftUp": high_i, "valueRightDown": low_i,
                "dataTemp": 1, "errorInfo": 0, "isOpposite": False,
            }],
        },
    }


def app_error(message: str) -> dict:
    """code != OK makes the app speak ``msg`` with the factory voice."""
    return {"code": "ERROR", "msg": message, "msgtype": "weather.error_loc"}


def _coordinates_from_address(address: str) -> tuple[str, str, str]:
    """(city, lat, lon) from the device location JSON, with blanks.

    ``RKLocationUtil.getRokidLocation()`` is a JSON object with city,
    latitude and longitude as strings -- the speaker reports its own
    location there. Anything missing comes back empty and the caller
    falls back to the configured city.
    """
    try:
        data = json.loads(address or "")
    except (ValueError, TypeError):
        return "", "", ""
    if not isinstance(data, dict):
        return "", "", ""
    city = str(data.get("city") or "").strip()
    lat = str(data.get("latitude") or "").strip()
    lon = str(data.get("longitude") or "").strip()
    return city, lat, lon


class OpenMeteo:
    """Open-Meteo reader (primary): current temperature plus today's range."""

    def __init__(self, url: str = "https://api.open-meteo.com/v1/forecast"):
        self.url = url

    def geocode(self, city: str) -> tuple[str, str]:
        """City name to (lat, lon) via the Open-Meteo geocoding API."""
        query = Request(
            "https://geocoding-api.open-meteo.com/v1/search?name=%s&count=1"
            "&language=zh" % _urlquote(city),
            headers={"User-Agent": "Rokid-Allen-weather/1.0"})
        with urlopen(query, timeout=OPEN_METEO_TIMEOUT) as response:
            data = json.load(response)
        results = data.get("results") or []
        if not results:
            return "", ""
        return (str(results[0]["latitude"]), str(results[0]["longitude"]))

    def read(self, lat: str, lon: str) -> tuple[int, str, float, float, float]:
        """(yahoo icon, condition, current, low, high) for one point."""
        query = Request(
            "%s?latitude=%s&longitude=%s&current_weather=true"
            "&daily=weathercode,temperature_2m_max,temperature_2m_min"
            "&forecast_days=1&timezone=auto" % (self.url, _urlquote(lat),
                                                 _urlquote(lon)),
            headers={"User-Agent": "Rokid-Allen-weather/1.0"})
        with urlopen(query, timeout=OPEN_METEO_TIMEOUT) as response:
            data = json.load(response)
        current = data["current_weather"]
        daily = data["daily"]
        wmo = int(current["weathercode"])
        icon, condition = WMO_TO_APP.get(wmo, (WMO_UNKNOWN_APP_ICON, ""))
        return (icon, condition, float(current["temperature"]),
                float(daily["temperature_2m_min"][0]),
                float(daily["temperature_2m_max"][0]))


# wttr.in (fallback) describes conditions in English words rather than
# one stable code table, so keywords -- checked worst-first, because
# "Patchy freezing drizzle" contains both "drizzle" and "freezing" --
# decide the icon and the Chinese wording. (icon, 中文, keywords)
DESC_RULES = [
    (17, "有冰雹", ("hail",)),
    (4, "有雷阵雨伴冰雹", ("thunderstorm",)),  # after plain hail
    (37, "有雷阵雨", ("thunder",)),
    (42, "有暴雪", ("blizzard",)),
    (8, "有冻雨", ("freezing drizzle",)),
    (10, "有冻雨", ("freezing rain", "ice pellets")),
    (40, "有大雨", ("heavy rain", "torrential", "moderate or heavy rain")),
    (40, "有强阵雨", ("heavy shower",)),
    (12, "有雨", ("moderate rain",)),
    (12, "有阵雨", ("shower",)),
    (11, "有小雨", ("light rain", "patchy light rain")),
    (9, "有毛毛雨", ("drizzle",)),
    (18, "有雨夹雪", ("sleet",)),
    (16, "有大雪", ("heavy snow",)),
    (14, "有小雪", ("light snow", "patchy snow")),
    (16, "有雪", ("snow",)),
    (20, "有雾凇", ("freezing fog",)),
    (20, "有雾", ("fog", "mist")),
    (19, "有沙尘", ("dust", "sand")),
    (26, "阴", ("overcast",)),
    (30, "多云转晴", ("partly cloudy",)),
    (26, "多云", ("cloudy",)),
    (32, "晴", ("sunny", "clear")),
]
DESC_DEFAULT = (44, "")


def description_to_app(description: str) -> tuple[int, str]:
    """wttr.in's English condition words to the app's icon and 中文."""
    text = (description or "").lower()
    for icon, condition, keywords in DESC_RULES:
        if any(word in text for word in keywords):
            return icon, condition
    return DESC_DEFAULT


class WttrIn:
    """wttr.in reader (fallback): takes coordinates or a plain city name."""

    def __init__(self, url: str = "https://wttr.in"):
        self.url = url

    def read(self, point: str) -> tuple[int, str, float, float, float]:
        """point is "lat,lon" or a city name, both accepted by the URL."""
        query = Request("%s/%s?format=j1" % (self.url, _urlquote(point)),
                        headers={"User-Agent": "Rokid-Allen-weather/1.0"})
        with urlopen(query, timeout=OPEN_METEO_TIMEOUT) as response:
            data = json.load(response)
        current = data["current_condition"][0]
        today = data["weather"][0]
        icon, condition = description_to_app(
            current["weatherDesc"][0]["value"])
        return (icon, condition, float(current["temp_C"]),
                float(today["mintempC"]), float(today["maxtempC"]))


def _urlquote(value: str) -> str:
    from urllib.parse import quote
    return quote(str(value), safe=".-")


def _adb_path(config: dict) -> Path:
    if os.environ.get("ROKID_ADB"):
        return Path(os.environ["ROKID_ADB"])
    tools = (config.get("adb_tools") or "").strip()
    if tools:
        return Path(tools) / "platform-tools" / "adb"
    return Path(__file__).resolve().parents[2] / "tools" / "platform-tools" / "adb"


class WeatherService:
    """Voice entry: open the native weather app over ADB.

    The engine calls ``handle_text`` for every turn, right after the
    mini-apps. A match never reaches the model or the factory: the app
    that opens speaks the answer itself, in the factory voice.

    The console card (设置 · 小应用 · 天气查询) talks to the same
    object: ``status`` / ``set_override`` / ``set_voice_phrases`` /
    ``query_now`` are its four endpoints, all persisted at once.
    """

    def __init__(self, engine, config: dict | None = None):
        self.engine = engine
        self.settings = config or {}
        self._last_launch = 0.0
        self.lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled")) and is_installed(self.engine, "weather")

    @property
    def voice_phrases(self) -> list[str]:
        """The console card's custom spoken phrases, as saved."""
        values = self.settings.get("voice_phrases") or []
        if isinstance(values, str):
            values = [values]
        return [str(v).strip() for v in values if str(v).strip()]

    def _custom_key_hit(self, text: str) -> bool:
        """A whole-sentence custom phrase, with the D044 normalisation.

        Both ends go through the mini-apps' shared ``match_key``, so a
        phrase pasted into the console with 「若琪，」 or punctuation
        folds to the same key the recogniser delivers.
        """
        key = match_key(text)
        if not key:
            return False
        return any(match_key(phrase) == key
                   for phrase in self.voice_phrases)

    def handle_text(self, text: str, request_id: str = "") -> dict | None:
        """None passes the turn on; a dict means the card was opened."""
        if (not self.enabled
                or not (is_weather_query(text) or self._custom_key_hit(text))):
            return None
        now = time.time()
        with self.lock:
            # The event bus and the logcat watcher report one utterance
            # as two turns; a launch every few seconds would flicker the
            # card. The window is small so a genuine repeat still works.
            if now - self._last_launch < 5.0:
                return {"accepted": True, "intent": "weather",
                        "launched": False, "duplicate": True}
            self._last_launch = now
        voice = text.strip()
        nlp = json.dumps({"domain": NLP_DOMAIN, "intent": "query_weather",
                          "voice": voice}, ensure_ascii=False)
        threading.Thread(target=self._launch, args=(nlp,),
                         name="weather-launch", daemon=True).start()
        self.engine.log_transcript("user", "", voice, request_id)
        self.engine.log_transcript(
            "agent", "", "（天气卡片已打开，原厂播报）", request_id)
        return {"accepted": True, "intent": "weather", "launched": True}

    def _launch(self, nlp: str):
        config = self.engine.config or {}
        serial = (config.get("device") or "").strip()
        if not serial:
            print("weather: no device serial in config", flush=True)
            return
        # shlex.quote keeps the JSON one shell word on the device side;
        # adb passes the bytes through unchanged.
        import shlex
        shell = ("am start -n %s --es %s %s"
                 % (WEATHER_ACTIVITY, NLP_EXTRA, shlex.quote(nlp)))
        try:
            result = subprocess.run(
                [str(_adb_path(config)), "-s", serial, "shell", shell],
                capture_output=True, text=True, timeout=15)
            note = (result.stdout or result.stderr or "").strip()
            print("weather launch: " + (note or "ok"), flush=True)
        except Exception as error:
            print("weather launch failed: %s" % error, flush=True)

    # -- console card (设置 · 小应用 · 天气查询) ---------------------------

    def status(self) -> dict:
        """What the console card needs on open."""
        return {
            "enabled": self.enabled,
            "city": str(self.settings.get("city") or ""),
            "api_url": str(self.settings.get("api_url") or ""),
            "wttr_url": str(self.settings.get("wttr_url") or ""),
            "voice_phrases": self.voice_phrases,
            "builtin_phrases": list(BUILTIN_KEYWORDS),
            "builtin_mode": "keywords",
        }

    def _persist(self) -> None:
        """Write the settings through, like the mini-apps service does."""
        save = getattr(self.engine, "_save_config", None)
        if callable(save):
            save()

    @staticmethod
    def _clean_url(url) -> str:
        """A source URL must be http(s) or empty (empty = the built-in)."""
        value = str(url or "").strip()
        if value and not value.lower().startswith(("http://", "https://")):
            raise ValueError("接口地址要以 http:// 或 https:// 开头")
        return value

    def set_settings(self, city=None, api_url=None, wttr_url=None) -> dict:
        """Save the card's 城市 and source URLs; empty clears each field.

        The city, once set, outranks the location the device reports --
        the user asked for that place. An empty city follows the device
        again. Source URLs are read live by the API server (Open-Meteo
        shaped primary, wttr.in shaped fallback), so a save takes effect
        on the app's very next query, with no host restart.
        """
        if city is not None:
            value = str(city).strip()
            if len(value) > 40:
                raise ValueError("城市名最多 40 个字")
            self.settings["city"] = value
        if api_url is not None:
            self.settings["api_url"] = self._clean_url(api_url)
        if wttr_url is not None:
            self.settings["wttr_url"] = self._clean_url(wttr_url)
        self._persist()
        return self.status()

    def set_override(self, enabled=None) -> dict:
        """Flip the 覆盖原厂天气查询 switch and persist it.

        ON: this service intercepts weather turns and the stored factory
        rule is used as-is (no weather words, so the factory stays
        silent for them). OFF: the turns go back to the factory, whose
        rule regains the weather words at runtime -- see
        ``engine.factory_pattern``. The replacement data API follows the
        switch too, so one console click starts / stops it with no host
        restart.
        """
        if enabled is not None:
            self.settings["enabled"] = bool(enabled)
            self._persist()
            api = getattr(self.engine, "weather_api", None)
            if api is not None:
                try:
                    api.start() if self.enabled else api.stop()
                except Exception as error:
                    print("weather api toggle failed: %s" % error,
                          flush=True)
        return self.status()

    def set_voice_phrases(self, phrases) -> dict:
        """Save the card's custom spoken phrases; empty clears them.

        Same normalisation as the mini-apps (the shared ``match_key`` on
        both ends, 12 phrases / 40 characters at most), so the console
        and the recogniser cannot disagree about what was said.
        """
        if isinstance(phrases, str):
            phrases = split_custom_phrases(phrases)
        seen: set[str] = set()
        cleaned: list[str] = []
        for value in list(phrases or []):
            phrase = str(value or "").strip()
            key = match_key(phrase)
            if (not phrase or not key
                    or len(phrase) > MAX_CUSTOM_PHRASE_CHARS
                    or key in seen):
                continue
            seen.add(key)
            cleaned.append(phrase)
            if len(cleaned) >= MAX_CUSTOM_PHRASES_PER_APP:
                break
        self.settings["voice_phrases"] = cleaned
        self._persist()
        return {"ok": True, "phrases": cleaned, "voice_phrases": cleaned}

    def query_now(self) -> dict:
        """The console button: open the card on the speaker right now.

        The Mac stays silent; the card and the answer are the factory's
        own, fed by the replacement API. Rejected while the override
        switch is off -- with the data API down the card would only say
        the factory's expiry notice.
        """
        if not self.enabled:
            return {"ok": False, "launched": False,
                    "error": "覆盖开关未开启：先打开「覆盖原厂天气查询」"}
        now = time.time()
        with self.lock:
            if now - self._last_launch < 5.0:
                return {"ok": True, "launched": False, "duplicate": True}
            self._last_launch = now
        nlp = json.dumps({"domain": NLP_DOMAIN, "intent": "query_weather",
                          "voice": "查询天气"}, ensure_ascii=False)
        threading.Thread(target=self._launch, args=(nlp,),
                         name="weather-console", daemon=True).start()
        return {"ok": True, "launched": True}

    def fix_wiring(self) -> dict:
        return {"ok": False, "error": "公开版保留设备的原厂网络与系统信任设置。"}



class Certs:
    """A tiny local CA plus the server certificate for the API hostname.

    Lives under ``runtime/weather/`` (never version-controlled). The
    device must be told to trust the CA once; the setup script does that.
    The extensions matter on both ends: modern verifiers reject a CA
    without ``authorityKeyIdentifier``, and Android 4.4 ignores what it
    does not know.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.ca_key = self.directory / "weather-ca.key"
        self.ca_cert = self.directory / "weather-ca.crt"
        self.server_key = self.directory / "weather-api.key"
        self.server_cert = self.directory / "weather-api.crt"

    def ensure(self) -> None:
        """Generate the CA and the server certificate when missing."""
        if self.server_cert.exists() and self.ca_cert.exists():
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        # A config file (rather than -addext, which LibreSSL lacks)
        # carries the CA extensions both certificate stacks accept.
        conf = self.directory / "weather-ca.cnf"
        conf.write_text(
            "[req]\nprompt = no\ndistinguished_name = dn\n"
            "x509_extensions = ca_ext\n"
            "[dn]\nCN = Rokid Allen Weather CA\n"
            "[ca_ext]\nbasicConstraints = critical,CA:TRUE\n"
            "keyUsage = critical,keyCertSign,cRLSign\n"
            "subjectKeyIdentifier = hash\n", encoding="utf-8")
        if not self.ca_cert.exists():
            _openssl("req", "-x509", "-newkey", "rsa:2048", "-sha256",
                     "-nodes", "-days", "3650",
                     "-config", str(conf),
                     "-keyout", str(self.ca_key), "-out", str(self.ca_cert))
        ext = self.directory / "weather-api.ext"
        ext.write_text(
            "subjectAltName=DNS:%s\nbasicConstraints=CA:FALSE\n"
            "keyUsage=digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\n"
            "authorityKeyIdentifier=keyid,issuer\n" % API_HOST,
            encoding="utf-8")
        csr = self.directory / "weather-api.csr"
        _openssl("req", "-new", "-newkey", "rsa:2048", "-sha256", "-nodes",
                 "-keyout", str(self.server_key), "-out", str(csr),
                 "-subj", "/CN=%s" % API_HOST)
        _openssl("x509", "-req", "-in", str(csr),
                 "-CA", str(self.ca_cert), "-CAkey", str(self.ca_key),
                 "-CAcreateserial", "-days", "825", "-sha256",
                 "-extfile", str(ext), "-out", str(self.server_cert))
        csr.unlink(missing_ok=True)


def _openssl(*args: str) -> None:
    subprocess.run(["openssl", *args], check=True, capture_output=True)


class WeatherApiServer:
    """HTTPS drop-in for the retired weather backend, on the LAN.

    The device wiring redirects ``weather-api.rokid.com:443`` to this
    host's configured port. Android 4.4's OkHttp speaks TLS 1.0 and
    validates against the system CA store, so the context allows the
    older protocols and the setup script installs the local CA.
    """

    def __init__(self, service: WeatherService, settings: dict,
                 meteo: OpenMeteo | None = None, wttr: WttrIn | None = None):
        self.service = service
        self.settings = settings
        # Explicit readers (the tests inject fakes); when they are absent
        # the readers are built from the live source URLs and rebuilt the
        # moment a saved URL changes -- see _sync_readers.
        self._fixed_meteo = meteo
        self._fixed_wttr = wttr
        self._reader_urls: tuple[str, str] | None = None
        self.meteo = meteo
        self.wttr = wttr
        self.port = int(settings.get("port") or 8766)
        self.ttl = float(settings.get("ttl_seconds") or CACHE_TTL)
        self._cache: dict[tuple[str, str], tuple[float, tuple]] = {}
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._sync_readers()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "RokidWeather/1.0"

            def do_POST(self):  # noqa: N802 -- stdlib naming
                if self.path != API_PATH:
                    self._send(app_error("接口路径不匹配"))
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(min(length, 65536)).decode(
                    "utf-8", "replace")
                form = parse_qs(body, keep_blank_values=True)
                # WeatherModelImpl sends the NLP payload under
                # RKContentRCMessageConstants.MESSAGE_TYPE, whose value is
                # "nlp"; "message" is accepted too for forward tolerance.
                message = ((form.get("nlp") or form.get("message")
                            or [""])[0])
                address = (form.get("address") or [""])[0]
                print("weather api: query message=%r address=%r"
                      % (message[:120], address[:200]), flush=True)
                self._send(outer.result_for(message, address))

            def do_GET(self):  # noqa: N802 -- health probe
                self._send({"code": "OK", "service": "weather",
                            "host": API_HOST})

            def log_message(self, fmt, *args):
                print("weather api: " + (fmt % args), flush=True)

            def _send(self, payload: dict):
                blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

        self.handler = Handler

    # -- live settings ------------------------------------------------------

    # The console saves city / source URLs at any time; every query reads
    # the values below straight from the settings dict, so a save is live
    # with the app's very next request -- no host restart, no rebuild.
    @property
    def city(self) -> str:
        return str(self.settings.get("city") or "").strip()

    @property
    def lat(self) -> str:
        return str(self.settings.get("latitude") or "").strip()

    @property
    def lon(self) -> str:
        return str(self.settings.get("longitude") or "").strip()

    @property
    def api_url(self) -> str:
        return (str(self.settings.get("api_url") or "").strip()
                or DEFAULT_API_URL)

    @property
    def wttr_url(self) -> str:
        return (str(self.settings.get("wttr_url") or "").strip()
                or DEFAULT_WTTR_URL)

    def _sync_readers(self) -> None:
        """(Re)build the source readers when a saved URL changed."""
        if self._fixed_meteo is not None or self._fixed_wttr is not None:
            return
        urls = (self.api_url, self.wttr_url)
        if self._reader_urls == urls:
            return
        self.meteo = OpenMeteo(urls[0])
        self.wttr = WttrIn(urls[1])
        self._reader_urls = urls

    # -- weather composition ------------------------------------------------

    def result_for(self, message: str, address: str) -> dict:
        """One query: pick the point, read the sources, shape the answer."""
        self._sync_readers()
        dev_city, dev_lat, dev_lon = _coordinates_from_address(address)
        dev_lat, dev_lon = _real_coordinates(dev_lat, dev_lon)
        # A city saved from the console outranks the device location: the
        # user asked for that place, the device only reports where its
        # last fix was. Without one the device wins as before.
        if self.city:
            city = self.city
            lat, lon = self.lat, self.lon
        else:
            city = dev_city
            lat, lon = (dev_lat or self.lat), (dev_lon or self.lon)
        try:
            nlp = json.loads(message or "")
        except (ValueError, TypeError):
            nlp = None
        voice = ""
        if isinstance(nlp, dict):
            voice = str(nlp.get("voice") or "")
        key = (lat or city, lon or "")
        try:
            icon, condition, temp, low, high = self._cached(key, city, lat, lon)
        except Exception as error:
            print("weather api: every source failed: %s" % error, flush=True)
            return app_error("天气数据暂时拿不到")
        return app_result(city or voice or "本地", icon, condition, temp,
                          low, high)

    def _fresh(self, city: str, lat: str, lon: str) -> tuple:
        """Read the sources in order; the first that answers wins.

        Open-Meteo has been observed unreachable from this LAN for days
        at a time (its edge, not the app), so wttr.in is the second leg
        with an independent network path.
        """
        if not lat and not lon and not city:
            raise IOError("no location configured")
        attempts = []
        if lat and lon:
            attempts.append(("open-meteo", lambda: self.meteo.read(lat, lon)))
            attempts.append(("wttr.in", lambda: self.wttr.read("%s,%s" % (lat, lon))))
        else:
            def geocoded():
                got_lat, got_lon = self.meteo.geocode(city)
                if not got_lat:
                    raise IOError("open-meteo geocode found nothing")
                return self.meteo.read(got_lat, got_lon)
            attempts.append(("open-meteo+geocode", geocoded))
            attempts.append(("wttr.in", lambda: self.wttr.read(city)))
        failures = []
        for name, attempt in attempts:
            try:
                return attempt()
            except Exception as error:
                failures.append("%s: %s" % (name, error))
                print("weather api: %s failed: %s" % (name, error),
                      flush=True)
        raise IOError("; ".join(failures) or "no source configured")

    def _cached(self, key: tuple, city: str, lat: str, lon: str) -> tuple:
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        value = self._fresh(city, lat, lon)
        self._cache[key] = (now, value)
        return value

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> bool:
        """Bind TLS on the configured port; False when already running."""
        if self._httpd:
            return True
        cert_dir = Path(self.settings.get("cert_dir")
                        or (Path(__file__).resolve().parents[1] / "runtime"
                            / "weather"))
        try:
            Certs(cert_dir).ensure()
        except Exception as error:
            print("weather api: certificates failed: %s" % error, flush=True)
            return False
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            # Android 4.4 negotiates TLS 1.0; modern OpenSSL hides it
            # behind the security level. Lower the level for this LAN
            # listener only -- it serves one app on one port.
            context.set_ciphers("DEFAULT@SECLEVEL=0")
            context.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
            context.load_cert_chain(str(Certs(cert_dir).server_cert),
                                     str(Certs(cert_dir).server_key))
            httpd = ThreadingHTTPServer(("0.0.0.0", self.port), self.handler)
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        except Exception as error:
            print("weather api: cannot listen on %d: %s" % (self.port, error),
                  flush=True)
            return False
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        name="weather-api", daemon=True)
        self._thread.start()
        print("weather api listening on https://0.0.0.0:%d (%s)"
              % (self.port, API_HOST), flush=True)
        return True

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None
        self._thread = None
