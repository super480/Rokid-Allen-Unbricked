"""Runtime: cache, merge and present one mini-app query.

Both the voice route and the HTTP route call ``query()``. GET-style reads of
a cached result never query upstream again and never open another card. The
device owns the countdown and the TTL, so presenting a card is a single
command with a validated layout object.

This app is display-only (user decision 2026-11-15): no query path emits a
``speak`` command. ``QueryResult.speech`` is retained as a text summary for
the console, but never reaches TTS.

D040: the runtime is app-agnostic. ``registry`` declares which operations
each app has; ``_provider_for`` and ``_builders`` select the reader and the
layout builder. The two Mac-local apps share one ``MacSystemProvider`` (and
therefore one 1-second sampler), created lazily so a host that only ever
answers Codex queries never starts a metrics thread.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from app_store import is_installed
from . import bambu, cards, registry, system_cards
from .codex_usage import CodexError, CodexRpcClient, CodexUsageProvider
from .models import QueryResult
from .system_metrics import MacSystemProvider

CACHE_TTL = {"quota": 60.0, "overview": 60.0, "tokens": 300.0,
             # Live machine readings go stale almost immediately.
             "speed": 2.0, "resources": 2.0}
DEFAULT_CARD_TTL_MS = 30000
FORCE_MIN_GAP = 10.0
ERROR_BACKOFF = 30.0
STALE_MAX_AGE = 900.0


class _Flight:
    def __init__(self):
        self.event = threading.Event()
        self.result: QueryResult | None = None


class MiniAppService:
    def __init__(self, engine, provider=None, config: dict | None = None,
                 system_provider=None):
        self.engine = engine
        self._settings = config or {}
        settings = self._settings
        self.provider = provider or CodexUsageProvider(
            CodexRpcClient(executable=settings.get("codex_executable") or None))
        self._system_provider = system_provider
        self.printer = bambu.BambuPrinter(engine)
        self._builders = {
            registry.APP_ID: cards.build_result,
            registry.NETWORK_APP_ID: system_cards.build_result,
            registry.RESOURCES_APP_ID: system_cards.build_result,
        }
        # DEFAULTS["miniapps"] documents "card_ttl_ms". The older
        # "miniapp_card_ttl_ms" spelling is still accepted for configs that
        # were written against the first implementation.
        self.card_ttl_ms = int(settings.get("card_ttl_ms")
                               or settings.get("miniapp_card_ttl_ms")
                               or DEFAULT_CARD_TTL_MS)
        self.lock = threading.RLock()
        self._cache: dict[tuple[str, str], QueryResult] = {}
        self._fetched: dict[tuple[str, str], float] = {}
        self._inflight: dict[tuple[str, str], _Flight] = {}
        self._errors: dict[tuple[str, str], float] = {}
        self._last_force: dict[tuple[str, str], float] = {}
        self._last_operation_by_app = dict(registry.DEFAULT_OPERATION)
        self.default_app_id = registry.APP_ID
        # 2026-11-16: console-defined voice phrases are INSTANCE state, not
        # module globals, so two services (or two tests) cannot clobber each
        # other. ``settings`` is the engine's live config["miniapps"] dict;
        # writing into it is what makes engine._save_config() persist it.
        self._voice_phrases = registry.normalise_custom_phrases(
            settings.get("voice_phrases") or {})
        self._voice_rules = registry.build_custom_rules(self._voice_phrases)
        self._generation = 0
        self._card_id = ""
        self._current: tuple[dict, float] | None = None
        self._current_app_id = registry.APP_ID

    # -- public API -----------------------------------------------------------

    def default_operation(self, app_id: str) -> str:
        return registry.default_operation(app_id)

    def handle_text(self, text: str, request_id: str = "") -> dict | None:
        """Voice/text route. None means this sentence is not a mini-app one."""
        hit = registry.match(text, self._voice_rules)
        if hit is None:
            return None
        if hit.action == "hide":
            self.hide()
            return {"accepted": True, "intent": "miniapp",
                    "action": "hide", "app_id": hit.app_id}
        if not is_installed(self.engine, hit.app_id):
            return None
        with self.lock:
            last = self._last_operation_by_app.get(hit.app_id, "")
        if hit.operation:
            operation = hit.operation
            with self.lock:
                self._last_operation_by_app[hit.app_id] = operation
        else:
            operation = last or self.default_operation(hit.app_id)
        result = self.query(hit.app_id, operation,
                            force_refresh=hit.action == "refresh",
                            wants_today=hit.wants_today,
                            request_id=request_id)
        return {"accepted": True, "intent": "miniapp",
                "app_id": hit.app_id, "operation": operation,
                "status": result.status, "speech": result.speech}

    def query(self, app_id: str = registry.APP_ID, operation: str = "quota",
              present: bool = True,
              force_refresh: bool = False, wants_today: bool = False,
              request_id: str = "") -> QueryResult:
        if operation not in registry.operations_for(app_id):
            raise ValueError("unknown mini-app or operation")
        if not is_installed(self.engine, app_id):
            raise ValueError("应用已删除，请先在应用商店添加")
        key = (app_id, operation)
        with self.lock:
            flight = self._inflight.get(key)
            joined = flight is not None
            if not joined:
                flight = _Flight()
                self._inflight[key] = flight
        if joined:
            # Same account + app + operation at the same time: one upstream
            # read, and only the initiator presents, so no card is duplicated.
            flight.event.wait(timeout=35.0)
            result = flight.result or self._error(
                app_id, operation, "timeout", "查询超时", request_id)
            present = False
        else:
            result = None
            try:
                result = self._compute(key, operation, force_refresh,
                                       request_id)
            finally:
                if result is None:
                    result = self._error(
                        app_id, operation, "upstream_error", "查询失败",
                        request_id)
                with self.lock:
                    self._inflight.pop(key, None)
                    flight.result = result
                    flight.event.set()
        if wants_today and operation == "tokens":
            result = self._focus_today(result)
        if not result.request_id:
            result.request_id = request_id
        if present:
            self._present(result, request_id=request_id)
        return result

    def hide(self) -> None:
        with self.lock:
            card_id = self._card_id
            app_id = self._current_app_id
            self._current = None
        if card_id:
            self.engine.emit("miniapp_card", action="hide",
                             card_id=card_id, app_id=app_id)

    def current_command(self) -> dict | None:
        """Unexpired card for a reconnecting device. Never re-speaks."""
        with self.lock:
            if not self._current:
                return None
            payload, expires_ms = self._current
            if time.time() * 1000 >= expires_ms:
                self._current = None
                return None
            return dict(payload)

    def status(self) -> dict:
        with self.lock:
            active = bool(self._current and
                          time.time() * 1000 < self._current[1])
            custom = {key: list(value)
                      for key, value in self._voice_phrases.items()}
        apps = registry.list_apps()
        return {"apps": apps,
                "device_card": {"protocol": "miniapp_card.v1",
                                "active": active},
                "card_ttl_ms": self.card_ttl_ms,
                "cache_ttl_seconds": dict(CACHE_TTL),
                # Console · 设置 · 小应用 renders these three read-only.
                "voice_phrases": custom,
                "builtin_phrases": {
                    app["id"]: registry.builtin_phrases_for(app["id"])
                    for app in apps},
                "card_actions": registry.card_action_phrases()}

    def set_voice_phrases(self, app_id: str, phrases) -> dict:
        """Save ONE card's custom spoken phrases; effective immediately.

        The console posts a list (or one comma-separated string). Empty
        input clears that card's custom phrases. Built-in phrases always
        win, so this can never break an existing voice command.
        """
        if app_id not in registry.OPERATIONS_BY_APP:
            raise ValueError("unknown mini-app: %s" % app_id)
        if isinstance(phrases, str):
            phrases = registry.split_custom_phrases(phrases)
        if not isinstance(phrases, (list, tuple)):
            raise ValueError("phrases must be a list of strings")
        cleaned = registry.normalise_custom_phrases({app_id: phrases})
        with self.lock:
            if cleaned:
                self._voice_phrases[app_id] = cleaned[app_id]
            else:
                self._voice_phrases.pop(app_id, None)
            self._voice_rules = registry.build_custom_rules(self._voice_phrases)
            self._settings["voice_phrases"] = {
                key: list(value)
                for key, value in self._voice_phrases.items()}
            saved = {key: list(value)
                     for key, value in self._voice_phrases.items()}
        # The real Engine writes config.local.json; test doubles may not
        # have the method, which must not break an in-memory save.
        save = getattr(self.engine, "_save_config", None)
        if callable(save):
            save()
        return {"ok": True, "app_id": app_id,
                "phrases": saved.get(app_id, []),
                "voice_phrases": saved}

    # -- internals ------------------------------------------------------------

    def _provider_for(self, app_id: str):
        if app_id == registry.APP_ID:
            return self.provider
        if self._system_provider is None:
            with self.lock:
                if self._system_provider is None:
                    self._system_provider = MacSystemProvider(
                        config=self._settings)
        return self._system_provider

    def _error(self, app_id: str, operation: str, code: str, message: str,
               request_id: str) -> QueryResult:
        if app_id == registry.APP_ID:
            return cards.error_result(
                operation, CodexError(code, message), request_id)
        if app_id == bambu.APP_ID:
            return bambu.result({}, request_id, message)
        return system_cards.error_result(operation, code, message, request_id)

    def _compute(self, key, operation: str, force_refresh: bool,
                 request_id: str) -> QueryResult:
        app_id, _ = key
        if app_id == bambu.APP_ID:
            try:
                return bambu.result(self.printer.read(), request_id)
            except bambu.BambuError as error:
                return bambu.result({}, request_id, str(error))
        now_mono = time.monotonic()
        with self.lock:
            cached = self._cache.get(key)
            age = now_mono - self._fetched.get(key, 0.0)
            ttl = CACHE_TTL.get(operation, 60.0)
            since_force = now_mono - self._last_force.get(key, 0.0)
            last_error = self._errors.get(key, 0.0)
        if cached is not None and not force_refresh and (
                age < ttl or now_mono - last_error < ERROR_BACKOFF):
            return cached
        if cached is not None and force_refresh and since_force < FORCE_MIN_GAP:
            return cached
        if force_refresh:
            with self.lock:
                self._last_force[key] = now_mono

        try:
            payload = self._provider_for(app_id).read(operation)
            result = self._builders[app_id](operation, payload, request_id)
            with self.lock:
                self._cache[key] = result
                self._fetched[key] = time.monotonic()
                self._errors.pop(key, None)
            return result
        except Exception as error:  # never leak a provider traceback upward
            code = getattr(error, "code", "upstream_error")
            message = getattr(error, "message", str(error)[:120])
            if not isinstance(code, str):
                code = "upstream_error"
            if not isinstance(message, str):
                message = str(error)[:120]
            failed = self._error(app_id, operation, code, message, request_id)
            with self.lock:
                self._errors[key] = time.monotonic()
            if (isinstance(error, CodexError) and cached is not None
                    and cached.status != "error" and age < STALE_MAX_AGE):
                failed = self._stale(cached, request_id)
            with self.lock:
                self._cache[key] = failed
                self._fetched[key] = time.monotonic()
            return failed

    def _stale(self, cached: QueryResult, request_id: str) -> QueryResult:
        minutes = max(1, int((time.monotonic()
                              - self._fetched.get(
                                  (cached.app_id, cached.operation), 0)) // 60))
        speech = ("%s数据来自约%s分钟前的缓存，更新失败。"
                  % (cached.speech.split("。")[0] + "。", minutes))[:100]
        card = dict(cached.data["card"])
        card["status"] = "stale"
        card["footer"] = (card.get("footer", "") + " · 缓存/更新失败")[:120]
        card["notice"] = "更新失败，显示上次成功读取的结果"
        data = dict(cached.data)
        data["card"] = card
        return QueryResult(
            app_id=cached.app_id, operation=cached.operation, status="stale",
            data=data, speech=speech, source=cached.source,
            error_code="upstream_error", fetched_at=cached.fetched_at,
            data_through=cached.data_through, request_id=request_id)

    @staticmethod
    def _focus_today(result: QueryResult) -> QueryResult:
        data = result.data
        today = data.get("today_tokens")
        latest = data.get("latest_bucket_date")
        if result.status != "error":
            if today is not None:
                result.speech = ("Codex 今天已统计 %s token，按服务端日期口径。"
                                 % cards.human_tokens(today))[:100]
            else:
                result.speech = ("目前没有今天的 token 统计" +
                                 ("，最新日统计到" + cards.cn_date(latest)
                                  if latest else "") + "。")[:100]
        return result

    def _present(self, result: QueryResult, request_id: str) -> None:
        if not is_installed(self.engine, result.app_id):
            return
        card = result.data.get("card")
        if not card:
            return
        now_wall = time.time()
        with self.lock:
            is_printer = result.app_id == bambu.APP_ID
            ttl_ms = bambu.CARD_TTL_MS if is_printer else self.card_ttl_ms
            refresh = (self._current is not None and now_wall * 1000 < self._current[1]
                       and not is_printer and self._current_app_id != bambu.APP_ID)
            if refresh:
                # A refreshed card keeps its deadline: TTL is not extended.
                expires_ms = self._current[1]
            else:
                self._card_id = uuid.uuid4().hex[:12]
                expires_ms = int(now_wall * 1000) + ttl_ms
            # Wall-clock milliseconds, not a per-process counter: a host
            # restart must never produce a smaller generation than the card
            # the device is still holding, or every new card is rejected as
            # stale. The device also resets its baseline when the instance
            # id changes (see BridgeService.handleMiniAppCard).
            self._generation = max(self._generation + 1,
                                   int(time.time() * 1000))
            payload = {
                "action": "show",
                "app_id": result.app_id,
                "operation": result.operation,
                "card_id": self._card_id,
                "generation": self._generation,
                "server_instance_id": getattr(self.engine, "instance_id", ""),
                "request_id": request_id,
                "ttl_ms": ttl_ms,
                "expires_at_ms": int(expires_ms),
                "card": card,
            }
            self._current = (payload, expires_ms)
            self._current_app_id = result.app_id
        self.engine.emit("miniapp_card", **payload)
        # Printer announcements use device TTS without opening an assistant avatar.
        if is_printer and result.status != "error" and result.speech:
            self.engine.emit("speak", text=result.speech, request_id=request_id, agent="none",
                             card_id=payload["card_id"], expires_at_ms=payload["expires_at_ms"])
