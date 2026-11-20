"""Host-side facade for the Home Assistant integration.

Batch 1 owns the ``homeassistant`` config section and the read-only
console API. Batch 2 adds the voice path on top:

    handle_text -> routing.Gate -> command queue -> client.process
    (the whole sentence to HA's conversation agent) -> HaResult ->
    lines card + brief spoken confirmation.

Batch 3/4 harden that path: after a control turn the host polls the
target entities at 1/2/4 s and marks the result ``state_confirmed`` /
``state_mismatch`` instead of trusting HA's reply, and a failed or
questioning turn arms a short pending window in which a brief reply
("再试一次" / "茶几灯") continues the same HA conversation.

Local code never decides *which* device or *which* action -- that is the
AI's job on the HA side. The gate only decides whether the sentence is
a home-control turn at all. Red line stays: unit tests mock the
transport, and no real device is switched before the user names one.
"""
from __future__ import annotations

import collections
import queue
import re
import threading
import time
from datetime import datetime
from typing import Any

from .cards import result_card, speech_text
from .client import (HaAuthError, HaClient, HaError, HaNetworkError,
                     HaProtocolError, Transport)
from .models import HaResult, normalise_base_url
from .routing import Gate, looks_like_query

# Entity domains the console offers for aliasing. Read from every domain
# is possible via the API, but the voice integration only ever manages
# devices, not HA internals (updates, media players, ...).
VOICED_DOMAINS = ("light", "switch", "climate", "cover", "fan",
                  "humidifier", "vacuum", "scene", "script",
                  "sensor", "binary_sensor", "lock", "media_player")
ENTITY_CAP = 500

QUEUE_SIZE = 4
DEDUP_WINDOW = 300.0          # request_id / text re-delivery window
TEXT_DEDUP_WINDOW = 8.0       # ASR + logcat double delivery
JOBS_KEPT = 20
# 若琪 (forwarded) and 小家 (its own backend) both build their card through
# _show_card(), so this one value keeps the two paths identical. The result
# is also spoken, so a short display is enough; mini-app query cards keep
# their own 30 s window.
CARD_TTL_MS_DEFAULT = 8000

# State verification (batch 3): bounded polls after a control turn.
VERIFY_CHECKPOINTS = (1.0, 2.0, 4.0)

# Multi-turn clarification (batch 4): how long a follow-up line may
# continue the same HA conversation, and which short lines count as an
# answer when the gate did not match them.
PENDING_WINDOW = 300.0
MAX_CLARIFY_LEN = 16
_MAX_ANSWER_ENTITY_LEN = 8

_NEEDS_CLARIFY = re.compile(
    r"哪个|哪一|请问|你是指|你说的|还是|没有找到|找不到|未找到|"
    r"不确定|请.{0,6}(?:确认|具体|明确|再说)|需要.{0,8}(?:确认|哪个)")
_ANSWER_WORDS = re.compile(
    r"^(?:再试(?:一次)?|重试|再来(?:一次)?|好的?|嗯+|行|可以|是的?|对|没错|"
    r"不是|不对|算了|取消|不要?了|第一个|第二个|第三个|最后那个|"
    r"就(?:是)?(?:那个|这个|它)|都(?:打开|关掉|开启|关闭|开|关))$")

DEFAULTS = {
    "enabled": False,
    "base_url": "",
    "token": "",
    "language": "zh-CN",
    "agent_id": "",
    "request_timeout_seconds": 15,
    "speak_results": True,
    "card_ttl_ms": CARD_TTL_MS_DEFAULT,
    "overview_entities": [],
    # entity_id -> extra spoken names (ASR variants), "|"-separated.
    "aliases": {},
    # Free-form extra gate words ("那个啥"-style nicknames) joined into
    # the routing gate and the device-side rule.
    "gate_extras": [],
    "shortcuts": [],
}


class HomeAssistantService:
    """Config, read-only access, and the batch-2 voice command path."""

    def __init__(self, config: dict | None = None,
                 save=None, transport: Transport | None = None,
                 engine=None):
        self.config = config if isinstance(config, dict) else {}
        self._save = save
        self._transport = transport
        self.engine = engine
        self._last_test: dict | None = None
        # -- voice path state (batch 2) --
        self.lock = threading.RLock()
        self._gate: Gate | None = None
        self._gate_signature: tuple | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._worker: threading.Thread | None = None
        self._closing = False
        self._recent_requests: dict[str, float] = {}
        self._recent_texts: dict[str, float] = {}
        self._jobs: collections.deque = collections.deque(maxlen=JOBS_KEPT)
        # Multi-turn clarification state (batch 4): one pending turn.
        self._pending: dict | None = None
        # mini-app card channel bookkeeping (same contract as miniapps).
        self._generation = 0
        self._card_id = ""
        self._current: tuple[dict, float] | None = None

    # -- helpers -----------------------------------------------------------

    def _client(self) -> HaClient:
        base = str(self.config.get("base_url") or "").rstrip("/")
        token = str(self.config.get("token") or "")
        if not base or not token:
            raise HaNetworkError("还没有填写 Home Assistant 地址或令牌")
        timeout = self.config.get("request_timeout_seconds") or 15
        return HaClient(base, token, timeout=float(timeout),
                        transport=self._transport)

    def _persist(self) -> None:
        if self._save:
            self._save()

    def _configured(self) -> bool:
        return (bool(self.config.get("enabled"))
                and bool(self.config.get("base_url"))
                and bool(self.config.get("token")))

    # -- gate ---------------------------------------------------------------

    def _gate_words(self) -> list[str]:
        """Extra gate words: alias variants + free-form extras."""
        words: list[str] = []
        for value in (self.config.get("aliases") or {}).values():
            for part in str(value or "").split("|"):
                part = part.strip()
                if part:
                    words.append(part)
        for part in (self.config.get("gate_extras") or []):
            value = str(part or "").strip()
            if value:
                words.append(value)
        return words

    def gate(self) -> Gate:
        """The routing gate, rebuilt when alias config changes."""
        words = self._gate_words()
        signature = tuple(words)
        with self.lock:
            if self._gate is None or self._gate_signature != signature:
                self._gate = Gate(words)
                self._gate_signature = signature
            return self._gate

    def device_rule(self) -> str:
        """Regex for the device (SpeechGate.haRule); '' = never match.

        Disabled or unconfigured HA must send '' so the device reverts
        to factory behaviour with no stale rule left behind.
        """
        if not self._configured():
            return ""
        return self.gate().device_rule

    # -- settings (console) -------------------------------------------------

    def settings(self) -> dict:
        """Desensitised settings. The token is never part of any response."""
        return {
            "enabled": bool(self.config.get("enabled")),
            "base_url": str(self.config.get("base_url") or ""),
            "token_configured": bool(self.config.get("token")),
            "language": str(self.config.get("language") or "zh-CN"),
            "agent_id": str(self.config.get("agent_id") or ""),
            "request_timeout_seconds": int(
                self.config.get("request_timeout_seconds") or 15),
            "speak_results": bool(self.config.get("speak_results", True)),
            "overview_entities": list(self.config.get("overview_entities")
                                      or []),
            "aliases": dict(self.config.get("aliases") or {}),
            "gate_extras": list(self.config.get("gate_extras") or []),
        }

    def update_settings(self, enabled=None, base_url=None, token=None,
                        clear_token=False, language=None, agent_id=None,
                        speak_results=None, overview_entities=None,
                        aliases=None, gate_extras=None,
                        keep_token=False) -> dict:
        """Apply a console edit.

        ``token=""`` keeps the stored value (the console shows a blank
        box); wiping needs the explicit ``clear_token`` flag. Switching
        base_url keeps the token only if it was already configured --
        a token from instance A must not silently authorise calls to
        instance B. ``keep_token=True`` is the explicit exception used
        by the wake-word page: the same HA instance got a new LAN IP
        (DHCP), so the stored long-lived token is still valid.

        Any connection change also drops the cached 小家 backends, so
        the next turn uses the new address/token without a host restart.
        """
        before = (str(self.config.get("base_url") or ""),
                  str(self.config.get("token") or ""),
                  str(self.config.get("agent_id") or ""))
        if base_url is not None:
            raw = str(base_url or "").strip()
            # The wake-word page lets the user type a bare LAN IP. The
            # URL validator still insists on a scheme, so add the default
            # here rather than making every caller do it.
            if raw and "://" not in raw:
                raw = "http://" + raw
            new_base = normalise_base_url(raw)
            old_base = str(self.config.get("base_url") or "").rstrip("/")
            self.config["base_url"] = new_base
            if (new_base != old_base and new_base and not token
                    and not keep_token):
                # Different instance: refuse to reuse the old token.
                self.config["token"] = ""
        if clear_token:
            self.config["token"] = ""
        elif token:
            self.config["token"] = str(token)
        if enabled is not None:
            self.config["enabled"] = bool(enabled)
        if language is not None:
            self.config["language"] = str(language or "zh-CN")
        if agent_id is not None:
            self.config["agent_id"] = str(agent_id or "")
        if speak_results is not None:
            self.config["speak_results"] = bool(speak_results)
        if overview_entities is not None:
            values = overview_entities if isinstance(overview_entities, list) \
                else []
            cleaned = []
            for item in values:
                value = str(item or "").strip()
                if value and value not in cleaned:
                    cleaned.append(value)
            self.config["overview_entities"] = cleaned[:4]
        if aliases is not None:
            # entity_id -> "|"-separated extra spoken names. A replace-all
            # map from the console; friendly names are implicit aliases.
            table = {}
            if isinstance(aliases, dict):
                for entity_id, value in aliases.items():
                    key = str(entity_id or "").strip()
                    if "." not in key:
                        continue
                    names = str(value or "").strip()
                    if names:
                        table[key] = names[:120]
            self.config["aliases"] = table
        if gate_extras is not None:
            values = gate_extras if isinstance(gate_extras, list) else []
            cleaned = []
            for item in values:
                value = str(item or "").strip()
                if value and value not in cleaned:
                    cleaned.append(value)
            self.config["gate_extras"] = cleaned[:40]
        self._persist()
        after = (str(self.config.get("base_url") or ""),
                 str(self.config.get("token") or ""),
                 str(self.config.get("agent_id") or ""))
        if before != after and self.engine is not None:
            self.engine.refresh_ha_backends()
        return self.settings()

    # -- read-only health ---------------------------------------------------

    def test_connection(self) -> dict:
        """Reachability + auth + entity count. Sends nothing that could
        move a device; ``/api/`` and ``/api/states`` are GETs."""
        result: dict[str, Any] = {
            "ok": False,
            "reachable": False,
            "auth_ok": False,
            "message": "",
            "entity_count": 0,
        }
        try:
            client = self._client()
            check = client.api_check()
            result["reachable"] = True
            result["auth_ok"] = True
            result["message"] = check["message"]
            states = client.states()
            result["entity_count"] = len(states)
            result["ok"] = True
        except HaAuthError as error:
            result["reachable"] = True
            result["message"] = error.message
        except HaError as error:
            result["message"] = error.message
        self._last_test = dict(result)
        return result

    def last_test(self) -> dict | None:
        return dict(self._last_test) if self._last_test else None

    # -- entities ------------------------------------------------------------

    def entities(self, query: str = "") -> list[dict]:
        """Capped entity listing for the alias editor. Read-only."""
        states = self._client().states()
        needle = str(query or "").strip().lower()
        rows = []
        for entry in states:
            if not isinstance(entry, dict):
                continue
            entity_id = str(entry.get("entity_id") or "")
            domain = entity_id.split(".")[0] if "." in entity_id else ""
            if domain not in VOICED_DOMAINS:
                continue
            name = str((entry.get("attributes") or {}).get("friendly_name")
                       or "")
            if needle and needle not in entity_id.lower() \
                    and needle not in name.lower():
                continue
            rows.append({
                "entity_id": entity_id,
                "domain": domain,
                "friendly_name": name,
                "state": str(entry.get("state") or ""),
            })
        # Devices before raw sensors so the console list is useful even
        # when the cap truncates the long tail.
        order = {domain: index for index, domain in enumerate(VOICED_DOMAINS)}
        rows.sort(key=lambda row: (order.get(row["domain"], 99),
                                   row["friendly_name"]))
        return rows[:ENTITY_CAP]

    # -- voice path (batch 2) ------------------------------------------------

    def home_sentence(self, text: str) -> str:
        """Read-only gate probe for the engine's ownership decision.

        Returns the sentence HA would hear (forced prefix stripped), or
        ``""`` when this is not a home turn. No request, no queue, no
        device call -- the engine uses it to decide *who* may handle a
        home sentence before anything is executed.
        """
        if not self._configured():
            return ""
        take, sentence = self.gate().route(text)
        return sentence if take else ""

    def handle_text(self, text: str, request_id: str = "") -> dict | None:
        """Voice route. None means: not a home turn, keep going down the
        normal chain (volume / miniapps / assistant).

        Only 若琪 (no assistant selected) calls this: the bridge forwards
        the whole sentence to HA's conversation agent. 小家 talks to HA
        directly through its own backend, and every other assistant must
        never reach this path (user decision 2026-11-20).
        """
        if not self._configured():
            return None
        take, sentence = self.gate().route(text)
        conversation_id = ""
        if not take:
            # Batch 4: a short reply right after a failed/asking turn
            # continues the same HA conversation instead of falling
            # through to the assistant.
            conversation_id = self._take_pending_reply(text)
            if not conversation_id:
                return None
            sentence = text.strip()
            take = True
        key = (request_id or "").strip()
        now = time.time()
        if key:
            with self.lock:
                self._expire_locked(now)
                if key in self._recent_requests:
                    return {"accepted": True, "intent": "homeassistant",
                            "duplicate": True, "status": "dropped"}
                self._recent_requests[key] = now
        item = (sentence, request_id, now, conversation_id)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._speak("家居指令有点多，这条先放着没执行。", request_id)
            self._record(HaResult(request_id=request_id, status="unknown",
                                  message="队列已满，未执行"), sentence)
            return {"accepted": True, "intent": "homeassistant",
                    "status": "dropped", "speech": "队列已满"}
        self._ensure_worker()
        return {"accepted": True, "intent": "homeassistant",
                "status": "queued", "sentence": sentence,
                "continued": bool(conversation_id)}

    def command(self, text: str, request_id: str = "") -> dict:
        """Console test path: one sentence, executed synchronously.

        Goes through the same gate and the same HA conversation agent as
        voice; the user presses a button for it, so blocking is fine.
        """
        take, sentence = self.gate().route(text)
        if not take:
            return {"accepted": False, "intent": "homeassistant",
                    "status": "rejected",
                    "message": "没有命中家居指令门控，未发送"}
        result = self._execute(sentence, request_id)
        self._present(result, sentence, request_id)
        return {"accepted": True, "intent": "homeassistant",
                "status": result.status, "sentence": sentence,
                "result": result.to_dict()}

    def jobs(self) -> list[dict]:
        """Most recent command results (console status list)."""
        with self.lock:
            return [dict(item) for item in self._jobs]

    def close(self) -> None:
        self._closing = True
        with self.lock:
            worker = self._worker
        if worker and worker.is_alive():
            try:
                self._queue.put_nowait(None)  # type: ignore[arg-type]
            except queue.Full:
                pass
            worker.join(timeout=2.0)

    # -- queue plumbing ------------------------------------------------------

    def _ensure_worker(self) -> None:
        with self.lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._worker_loop,
                                            name="ha-command",
                                            daemon=True)
            self._worker.start()

    def _worker_loop(self) -> None:
        while not self._closing:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                self._queue.task_done()
                return
            sentence, request_id, _queued_at, conversation_id = item
            try:
                result = self._execute(sentence, request_id,
                                       conversation_id=conversation_id)
                self._present(result, sentence, request_id)
            except Exception as error:   # never kill the worker
                self._speak("智能家居出错了。", request_id)
                self._record(HaResult(request_id=request_id, status="failed",
                                      message=str(error)[:120]), sentence)
            finally:
                self._queue.task_done()

    def _expire_locked(self, now: float) -> None:
        for table, window in ((self._recent_requests, DEDUP_WINDOW),
                              (self._recent_texts, TEXT_DEDUP_WINDOW)):
            for key in [k for k, when in table.items() if now - when > window]:
                table.pop(key, None)

    def _record(self, result: HaResult, sentence: str) -> None:
        entry = result.to_dict()
        entry["sentence"] = sentence
        with self.lock:
            self._jobs.appendleft(entry)

    # -- execution + parsing -------------------------------------------------

    def _execute(self, sentence: str, request_id: str = "",
                 conversation_id: str = "") -> HaResult:
        """One conversation turn. No retry: the agent may have executed.

        ``conversation_id`` continues a previous HA session (clarify
        reply). After a fresh control turn the target entities are
        polled at 1/2/4 s so the result states what actually happened.
        """
        language = str(self.config.get("language") or "zh-CN")
        agent_id = str(self.config.get("agent_id") or "")
        kind = "query" if looks_like_query(sentence) else "control"
        started_at = time.time()
        try:
            data = self._client().process(sentence, language=language,
                                          agent_id=agent_id,
                                          conversation_id=conversation_id)
        except HaAuthError as error:
            return HaResult(request_id=request_id, kind=kind, status="failed",
                            message=error.message)
        except HaNetworkError:
            # A timeout or a dropped connection can still have run the
            # command; refusing to claim either way is the safe answer.
            return HaResult(request_id=request_id, kind=kind, status="unknown",
                            message="网络异常，没有收到执行结果")
        except (HaProtocolError, HaError) as error:
            return HaResult(request_id=request_id, kind=kind, status="failed",
                            message=error.message)
        result = self._parse(data, request_id)
        result.kind = kind
        result = self._verify(result, started_at)
        self._maybe_pend(result, sentence)
        return result

    def _verify(self, result: HaResult, started_at: float) -> HaResult:
        """Bounded state read-back after a control turn (batch 3).

        Polls the entities HA reported as touched at +1/+2/+4 s. A
        ``last_changed``/``last_updated`` after the command started is
        ``state_confirmed``; no movement within 4 s is ``state_mismatch``
        (the card then refuses to show green). Queries and turns without
        a known target stay ``not_checked``.
        """
        if result.kind != "control" or not result.targets:
            return result
        entity_ids = [str(t.get("entity_id") or "")
                      for t in result.targets if t.get("entity_id")]
        if not entity_ids:
            return result
        for checkpoint in VERIFY_CHECKPOINTS:
            wait = started_at + checkpoint - time.time()
            if wait > 0:
                time.sleep(wait)
            try:
                client = self._client()
                for entity_id in entity_ids:
                    state = client.state(entity_id)
                    if self._changed_since(state, started_at):
                        result.verification = "state_confirmed"
                        return result
            except HaError:
                # Could not read back: honest about not knowing.
                return result
        result.verification = "state_mismatch"
        return result

    @staticmethod
    def _changed_since(state: dict, started_at: float) -> bool:
        if not isinstance(state, dict):
            return False
        for key in ("last_changed", "last_updated"):
            raw = state.get(key)
            if not raw:
                continue
            try:
                stamp = datetime.fromisoformat(
                    str(raw).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if stamp > started_at:
                return True
        return False

    def _maybe_pend(self, result: HaResult, sentence: str) -> None:
        """Arm a clarify window when HA failed or asked back (batch 4)."""
        if result.kind != "control":
            self._pending = None
            return
        needs = (result.status in ("failed", "unknown")
                 or bool(_NEEDS_CLARIFY.search(result.message or "")))
        if not needs:
            self._pending = None
            return
        self._pending = {
            "conversation_id": result.conversation_id,
            "sentence": sentence,
            "expires_at": time.time() + PENDING_WINDOW,
        }

    def _take_pending_reply(self, text: str) -> str:
        """Return the pending conversation_id when ``text`` looks like
        the user answering "哪个灯？" / "再试一次？"; otherwise ''."""
        pending = self._pending
        if not pending:
            return ""
        now = time.time()
        if now > pending.get("expires_at", 0):
            self._pending = None
            return ""
        sentence = str(text or "").strip()
        if not sentence or len(sentence) > MAX_CLARIFY_LEN:
            return ""
        if _ANSWER_WORDS.match(sentence):
            self._pending = None
            return str(pending.get("conversation_id") or "")
        # "茶几灯" / "主卧那个": short, names a thing, but no verb so the
        # gate let it through -- the likely answer to "哪个？".
        if len(sentence) <= _MAX_ANSWER_ENTITY_LEN:
            words = self.gate().home_words
            if any(word and word in sentence for word in words):
                self._pending = None
                return str(pending.get("conversation_id") or "")
        return ""

    @staticmethod
    def _parse(data: dict, request_id: str = "") -> HaResult:
        """Turn a conversation/process payload into a HaResult.

        A polite "done" is ``reported_success`` (HA said so -- the state
        is confirmed separately by ``_verify``). Empty payload ->
        ``unknown``. The top-level ``conversation_id`` is kept so a
        follow-up line can continue this HA session.
        """
        if not isinstance(data, dict):
            data = {}
        conversation_id = str(data.get("conversation_id") or "")
        response = data.get("response") if isinstance(data, dict) else None
        if not isinstance(response, dict):
            response = {}
        message = ""
        speech = response.get("speech")
        if isinstance(speech, dict):
            plain = speech.get("plain")
            if isinstance(plain, dict):
                message = str(plain.get("speech") or "")
            elif isinstance(plain, str):
                message = plain
        payload = response.get("data")
        if not isinstance(payload, dict):
            payload = {}
        success = payload.get("success") or {}
        failed = payload.get("failed") or {}
        targets = []
        if isinstance(success, dict):
            for entity_id, info in success.items():
                name = ""
                if isinstance(info, dict):
                    name = str(info.get("name") or "")
                targets.append({"entity_id": str(entity_id), "name": name})
        elif isinstance(success, list):
            for item in success:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    targets.append({"entity_id": item["id"],
                                    "name": str(item.get("name") or "")})
        if response.get("response_type") == "error":
            return HaResult(request_id=request_id, status="failed",
                            message=message.strip(),
                            conversation_id=conversation_id)
        if not message and not success and not failed:
            return HaResult(request_id=request_id, status="unknown",
                            message="", targets=targets,
                            conversation_id=conversation_id)
        if failed and success:
            status = "partial"
        elif failed:
            status = "failed"
            if not message:
                message = "部分设备没有响应"
        else:
            status = "reported_success"
        return HaResult(request_id=request_id, status=status,
                        message=message.strip(), targets=targets,
                        conversation_id=conversation_id)

    # -- presentation ---------------------------------------------------------

    def _speak(self, text: str, request_id: str = "",
              card_id: str = "", expires_at_ms: int = 0) -> None:
        """Speak one line.

        A ``card_id`` binds the line to a card already on screen. The
        device then routes it through ``speakForCard``, which waits for
        the previous TTS request to finish and for the factory-cancel
        window to settle before submitting. A bare speak submitted while
        the factory's TTS session is still tearing down wedges the
        engine (``tts_error error=2``) and the line is never heard.
        """
        if self.engine is None or not text:
            return
        payload = {"text": text, "request_id": request_id}
        if card_id:
            payload["card_id"] = card_id
            payload["expires_at_ms"] = int(expires_at_ms)
        self.engine.emit("speak", **payload)

    def _present(self, result: HaResult, sentence: str,
                 request_id: str = "") -> None:
        self._record(result, sentence)
        if self.engine is not None:
            try:
                self.engine.log_transcript(
                    "system", "homeassistant",
                    "%s -> %s" % (sentence, result.status), request_id)
            except Exception:
                pass
        try:
            card = result_card(result, sentence=sentence)
        except ValueError:
            card = None
        binding = self._show_card(card, request_id) if card is not None else None
        if self.config.get("speak_results", True):
            card_id, expires = binding if binding else ("", 0)
            self._speak(speech_text(result), request_id,
                        card_id=card_id, expires_at_ms=expires)

    def present_xiaojia(self, payload: dict, sentence: str,
                        request_id: str = "") -> tuple[str, int]:
        """Show 小家's own result card and return its speech binding.

        小家 runs HA's conversation agent through its backend, so it
        never reaches ``handle_text``/``_present``. Without this the
        speaker shows no feedback card at all for 小家 turns. The
        backend keeps the raw ``conversation/process`` payload; parsing
        it here gives the same targets, entity names and specialised
        pages as a forwarded 若琪 turn.

        State read-back is deliberately not done here: the backend turn
        already answered, and polling would block the shared job worker
        for up to four seconds. ``not_checked`` keeps the card honest.
        """
        result = self._parse(payload, request_id)
        result.kind = "query" if looks_like_query(sentence) else "control"
        result.message = result.message or "小家没有听懂，换个说法试试"
        self._record(result, sentence)
        if self.engine is not None:
            try:
                self.engine.log_transcript(
                    "system", "homeassistant",
                    "%s -> %s" % (sentence, result.status), request_id)
            except Exception:
                pass
        try:
            card = result_card(result, sentence=sentence)
        except ValueError:
            return "", 0
        binding = self._show_card(card, request_id)
        return binding if binding else ("", 0)

    def _show_card(self, card: dict, request_id: str = "") -> tuple[str, int] | None:
        """Emit one mini-app card, returning (card_id, expires_at_ms).

        The caller binds the spoken line to this card so the device waits
        for the previous TTS request before playing it.
        """
        if self.engine is None:
            return None
        import uuid
        now = time.time()
        ttl_ms = int(self.config.get("card_ttl_ms") or CARD_TTL_MS_DEFAULT)
        with self.lock:
            self._card_id = uuid.uuid4().hex[:12]
            expires_ms = int(now * 1000) + ttl_ms
            self._generation = max(self._generation + 1,
                                   int(now * 1000))
            payload = {
                "action": "show",
                "app_id": "homeassistant",
                "operation": "command",
                "card_id": self._card_id,
                "generation": self._generation,
                "server_instance_id": getattr(self.engine, "instance_id", ""),
                "request_id": request_id,
                "ttl_ms": ttl_ms,
                "expires_at_ms": int(expires_ms),
                "card": card,
            }
            self._current = (payload, expires_ms)
        self.engine.emit("miniapp_card", **payload)
        return payload["card_id"], payload["expires_at_ms"]
