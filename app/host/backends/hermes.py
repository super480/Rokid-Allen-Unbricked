"""Hermes Agent backend.

Drives the locally installed ``hermes`` CLI in non-interactive mode. Hermes is
a full desktop agent: it can read files, run commands and browse. That makes
it a good match for "voice the task to the desktop agent" -- the speaker
becomes an input device for an agent that already knows how to use the
computer.

Two behaviours matter for the speaker:

* the session is kept between turns, so a follow-up question has context;
* the tool stream is reported as progress, so the screen shows what the
  agent is doing instead of a frozen "thinking" face.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading

from backends.base import Backend, BackendError, report
from sandbox import clean_env

# Hermes prints the session id on its own line as well as inside the result
# object; take it from the structured event, and keep this as a fallback.
SESSION_LINE = re.compile(r"^session_id:\s*(\S+)\s*$", re.M)

# A session that still has an unfinished turn queues the next one instead of
# running it: "Queued admission: <id> (waiting to run): <question>". The
# queue never drains, so the turn would wait for the timeout and then fail.
# Such a session is unusable and has to be dropped.
#
# Hermes prints one such line per queued turn, so the notice is matched on
# its own line.
QUEUED = re.compile(r"^Queued admission:.*\(waiting to run\)")

# Tool names mapped to a short phrase for the 640x480 screen.
TOOL_LABELS = {
    "terminal": "执行命令",
    "search_files": "查找文件",
    "read_file": "读取文件",
    "write_file": "写入文件",
    "edit_file": "修改文件",
    "list_directory": "浏览目录",
    "web_search": "联网搜索",
    "browser": "打开网页",
    "fetch_url": "抓取网页",
    "python": "运行脚本",
    "spawn_agent": "派生子任务",
}


def _terminate(process):
    """Stop a child and reap it, so it does not linger as a zombie."""
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    except Exception:
        pass


class HermesBackend(Backend):
    id = "hermes"
    name = "Hermes"

    def __init__(self, settings, state_store=None):
        self.settings = settings or {}
        self.state_store = state_store
        self.command = list(self.settings.get("command") or
                            ["hermes", "chat", "--oneshot", "--format", "stream-json"])
        self.timeout = int(self.settings.get("timeout_seconds") or 300)
        self.keep_session = bool(self.settings.get("keep_session", True))
        self.safe_mode = bool(self.settings.get("safe_mode", False))
        # The model to answer with. Empty means "whatever Hermes is
        # configured to use"; a name here overrides it for this assistant.
        self.model = str(self.settings.get("model") or "").strip()
        # The provider that serves the model. Hermes sends the model name
        # to the provider it is currently on, and an endpoint that does
        # not know the name rejects it -- after which Hermes silently
        # falls back to another model. So the provider has to be named
        # along with the model.
        self.provider = str(self.settings.get("provider") or "").strip()
        if not self.provider and self.model:
            self.provider = self.provider_for(self.model)
        # The key the session id is stored under. A caller that wants a
        # conversation of its own -- direct mode does -- passes its own key
        # so the two conversations stay apart.
        self.session_key = str(self.settings.get("session_key") or "hermes_session")
        # The key the model of that session is stored under. A resumed
        # session keeps the model it was launched with -- Hermes refuses to
        # switch the model of a running conversation -- so the session and
        # the model it runs on have to be recorded together and compared
        # before the session is reused.
        self.session_model_key = self.session_key + "_model"
        # The provider of that session, under its own key: a session
        # belongs to the endpoint it was started on, and Hermes refuses
        # to resume it with another one.
        self.session_provider_key = self.session_key + "_provider"
        self.session_id = ""
        # The model the last turn really ran on. Hermes falls back to
        # another model when the endpoint does not know the requested
        # one, and the fallback shows nowhere else.
        self.used_model = ""
        self._process = None
        self._lock = threading.Lock()

    # -- configuration --------------------------------------------------------

    def executable(self):
        """Locate the hermes binary.

        A configured command wins: if it names a path, that path is used as
        is; if it names a program, only PATH is searched for that exact
        program. The built-in fallbacks apply only when nothing is configured,
        so a misconfigured command never silently runs a different binary.
        """
        configured = self.settings.get("command")
        if configured:
            first = str(configured[0])
            if os.path.sep in first:
                return first if os.path.exists(first) else None
            return shutil.which(first)
        found = shutil.which("hermes")
        if found:
            return found
        for candidate in (os.path.expanduser("~/.local/bin/hermes"),
                          "/usr/local/bin/hermes",
                          "/opt/homebrew/bin/hermes"):
            if os.path.exists(candidate):
                return candidate
        return None

    def health(self):
        path = self.executable()
        if not path:
            return False, "找不到 hermes 命令"
        return True, path

    # -- models ---------------------------------------------------------------

    def models(self):
        """The models this assistant can answer with.

        The list comes from Hermes itself, so it never offers a model the
        current account cannot reach.
        """
        from backends import models as model_catalog
        names = model_catalog.hermes_models()
        if self.model and self.model not in names:
            names.insert(0, self.model)
        return names

    def current_model(self):
        """The model this assistant answers with right now."""
        if self.model:
            return self.model
        from backends import models as model_catalog
        return model_catalog.hermes_current_model()

    def switch_model(self, name):
        """Change the model. Returns the model now in use.

        Hermes cannot switch models on a resumed session: it keeps the
        launch model and rejects ``--model`` on resume. So changing the
        model drops the current session, and the next turn starts a fresh
        one with the new model.
        """
        name = str(name or "").strip()
        self.model = name
        # The provider follows the model: the endpoint a model lives on
        # is what serves it, and sending the name to another endpoint is
        # what makes Hermes fall back to a different model. A provider
        # that was set by hand is kept for a model no endpoint lists --
        # there is nothing better to say about that one.
        found = self.provider_for(name)
        if found:
            self.provider = found
        elif not self.provider:
            self.provider = ""
        # The kept session runs on the model it was started with, so it
        # only survives when that is the model in use now. This also
        # covers the case where the name did not change but the session
        # belongs to another model -- an older state file, or a model
        # changed outside the console.
        if not self._session_model_matches():
            self.reset_session()
        return self.model

    @staticmethod
    def provider_for(name):
        """The provider that serves a model, when it is known.

        A model that no endpoint lists has no known provider: Hermes
        then uses the one from its own configuration, which is what
        happened before this was known -- the model name went to an
        endpoint that did not know it.
        """
        name = str(name or "").strip()
        if not name:
            return ""
        from backends import models as model_catalog
        endpoints = model_catalog.hermes_model_endpoints()
        return str(endpoints.get(name) or "")

    # -- session --------------------------------------------------------------

    def _stored_session(self):
        if self.state_store is None:
            return ""
        value = self.state_store.get(self.session_key)
        return value or ""

    def _stored_session_model(self):
        """The model the stored session was started with."""
        if self.state_store is None:
            return ""
        value = self.state_store.get(self.session_model_key)
        return value or ""

    def _stored_session_provider(self):
        """The provider the stored session was started with."""
        if self.state_store is None:
            return ""
        value = self.state_store.get(self.session_provider_key)
        return value or ""

    def _session_model_matches(self):
        """True when the kept session runs on the model in use now.

        A resumed Hermes session keeps the model it was started with, so
        a session from another model can not carry the current one over:
        continuing it would answer with the old model no matter what the
        configuration says. The same holds for the provider: a session
        belongs to the endpoint it was started on, and Hermes refuses to
        resume it with another one.
        """
        if not (self.session_id or self._stored_session()):
            return True
        started_with = self._stored_session_model()
        if not self.model:
            # No model is configured, so Hermes uses its own default. A
            # session that was started with an explicit model runs on
            # that one and does not survive the switch to the default.
            return not started_with
        if started_with != self.model:
            return False
        # The provider has to match as well. An older state file has no
        # provider for its session: the model is then the only thing
        # that can be compared, and a session without a known provider
        # is not carried over when a provider is wanted now.
        started_provider = self._stored_session_provider()
        if self.provider and started_provider != self.provider:
            return False
        return True

    def _remember_session(self, session_id):
        if not session_id or self.state_store is None:
            return
        self.state_store[self.session_key] = session_id
        # The model is remembered with the session: this is the model the
        # conversation runs on, and a resumed session keeps it whatever
        # the configuration says afterwards. The provider belongs to it
        # for the same reason.
        self.state_store[self.session_model_key] = self.model
        self.state_store[self.session_provider_key] = self.provider

    def reset_session(self):
        """Forget the session; the next turn starts a fresh conversation."""
        self.session_id = ""
        if self.state_store is not None:
            self.state_store.pop(self.session_key, None)
            self.state_store.pop(self.session_model_key, None)
            self.state_store.pop(self.session_provider_key, None)

    # -- running --------------------------------------------------------------

    def _log_path(self):
        """Where to keep the child's stderr, if a log directory is known."""
        root = self.settings.get("_root")
        if not root:
            return None
        import os as _os
        path = _os.path.join(str(root), "..", "runtime", "hermes.log")
        try:
            _os.makedirs(_os.path.dirname(path), exist_ok=True)
        except OSError:
            return None
        return path

    def build_argv(self, text, session_id):
        argv = [self.executable() or "hermes", "chat"]
        argv += ["--oneshot", "--format", "stream-json"]
        if self.provider:
            # The provider decides which endpoint the model name is sent
            # to. Without it Hermes uses the one from its own config, and
            # a model of another endpoint is rejected there -- which ends
            # in a silent fallback to a different model.
            argv += ["--provider", self.provider]
        if session_id:
            # Resuming keeps the session's launch model; Hermes rejects a
            # --model that differs from how the session was started
            # ("resume without it and use /model after resuming to
            # switch"). So the model flag is only set when a fresh session
            # is created; a model change on an existing session is applied
            # by starting a new one (see switch_model).
            argv += ["--resume", session_id]
        elif self.model:
            # The flag is what makes the model choice take effect; without
            # it Hermes answers with whatever its own config says.
            argv += ["--model", self.model]
        argv += ["--query", text]
        return argv

    def session_for_model(self):
        """The session to continue, or none when it runs on another model.

        A resumed Hermes session keeps the model it was started with, so a
        session from a different model can not carry the current one over:
        continuing it would answer with the old model no matter what the
        configuration says. Such a session is dropped and the
        conversation starts over, which is the only way the new model
        actually takes effect.
        """
        session_id = self.session_id or self._stored_session()
        if not session_id:
            return ""
        if not self._session_model_matches():
            self.reset_session()
            return ""
        return session_id

    def run(self, text, request_id, on_progress=None):
        """Answer one turn, restarting once if a kept session turns out to be dead."""
        try:
            return self._run_once(text, request_id, on_progress)
        except BackendError as error:
            message = str(error)
            # A session that Hermes no longer knows about makes every resume
            # fail. Drop it and start a fresh conversation rather than
            # losing the turn.
            if self._session_gone(message) or self._session_stuck(message):
                self.reset_session()
                return self._run_once(text, request_id, on_progress)
            raise

    @staticmethod
    def _session_gone(message):
        lowered = message.lower()
        return any(marker in lowered for marker in
                   ("session not found", "no such session", "unknown session",
                    "invalid session", "session expired", "session not exist",
                    "no session found"))

    @staticmethod
    def _session_stuck(message):
        """True when the session still owes an answer and queues new turns."""
        return "会话队列未清空" in message

    def build_env(self):
        """The child's environment.

        The session-scoped sandbox state is dropped: the host outlives the
        shell that started it, and that state names a socket which is gone
        by the time a turn runs. See app/host/sandbox.py.
        """
        env = clean_env()
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    def _run_once(self, text, request_id, on_progress=None):
        path = self.executable()
        if not path:
            raise BackendError("找不到 hermes 命令，请在电脑上安装 Hermes Agent。")
        if not (text or "").strip():
            raise BackendError("没有听到任务内容。")

        cwd = self.settings.get("cwd")
        argv = self.build_argv(text, self.session_for_model())
        report(on_progress, "thinking", "正在理解任务")

        env = self.build_env()

        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd if cwd and os.path.isdir(str(cwd)) else None,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                # The host may itself have been started from a shell that has
                # since exited. Without a fresh session the child inherits
                # that broken process group and can fail with
                # "init_sys_streams: can't initialize sys standard streams".
                start_new_session=True,
            )
        except OSError as exc:
            raise BackendError("无法启动 Hermes：" + type(exc).__name__)

        with self._lock:
            self._process = process

        answer = ""
        tool_count = 0
        spoken = False
        stderr_lines = []
        # Keep the child's stderr on disk: when Hermes fails, its traceback is
        # the only useful diagnostic, and it is gone once the process exits.
        log_path = self._log_path()
        # stderr must be drained while stdout is read. A child that writes
        # more than the pipe buffer holds would otherwise block forever and
        # never close stdout.
        # The queued notice arrives on stderr; the stdout reader cannot see
        # it. Both readers watch for it so the session is dropped either way.
        queued = threading.Event()

        def drain_stderr():
            try:
                for line in process.stderr:
                    text = line.rstrip("\r\n")
                    stderr_lines.append(text)
                    if QUEUED.match(text):
                        queued.set()
                    if log_path:
                        try:
                            with open(log_path, "a", encoding="utf-8") as handle:
                                handle.write(text + "\n")
                        except OSError:
                            pass
            except Exception:
                pass

        reader = threading.Thread(target=drain_stderr, name="hermes-stderr", daemon=True)
        reader.start()

        # stdout is read on its own thread. The child may announce the
        # queue on stderr and then sit there with stdout open; reading in
        # the foreground would block until the timeout, so the lines arrive
        # through a queue instead.
        lines = queue.Queue()

        def pump_stdout():
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        pump = threading.Thread(target=pump_stdout, name="hermes-stdout", daemon=True)
        pump.start()

        try:
            while True:
                try:
                    line = lines.get(timeout=0.5)
                except queue.Empty:
                    if queued.is_set():
                        # The notice arrived on stderr and the child is
                        # waiting; the session can not serve this turn.
                        self.reset_session()
                        raise BackendError(
                            "Hermes 会话队列未清空：会话仍在等待上一条回答。")
                    if process.poll() is not None and lines.empty():
                        break
                    continue
                if line is None:
                    break
                line = line.strip()
                if not line:
                    continue
                if not line.startswith("{"):
                    match = SESSION_LINE.match(line)
                    if match:
                        self._remember_session(match.group(1))
                    elif QUEUED.match(line):
                        # The kept session still owes an answer, so this
                        # turn would sit in the queue for ever. Drop the
                        # session and let the caller start a fresh one.
                        self.reset_session()
                        raise BackendError(
                            "Hermes 会话队列未清空：" + line.strip()[:200])
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "system" and event.get("subtype") == "init":
                    # The init event names the model this turn really runs
                    # on. Hermes falls back to another model when the
                    # requested one is unknown to the endpoint, and the
                    # fallback is visible nowhere else -- so it is
                    # reported here, where the speaker can be told.
                    self.used_model = str(event.get("model") or "")
                elif kind == "tool_use":
                    tool_count += 1
                    label = TOOL_LABELS.get(event.get("name"), event.get("name") or "执行任务")
                    report(on_progress, "working", label)
                elif kind == "text":
                    # Hermes streams the answer token by token. The screen
                    # only needs to know that the answer has started.
                    if not spoken:
                        spoken = True
                        report(on_progress, "speaking", "")
                elif kind == "result":
                    answer = event.get("text") or ""
                    session = event.get("session_id")
                    failure = event.get("error") or ""
                    if failure:
                        # Hermes reports a failure in the result event,
                        # not only through the exit code.
                        raise BackendError("Hermes 出错：" + str(failure)[:400])
                    if session:
                        self.session_id = session
                        self._remember_session(session)
                    if event.get("exit_code"):
                        report(on_progress, "error", "Hermes 执行失败")

        except BackendError:
            # The child is still running; it must not outlive this call.
            _terminate(process)
            raise
        finally:
            with self._lock:
                self._process = None

        # The child may still be running when stdout closes. Give it a moment
        # to exit; a timeout here is a real failure, not an empty answer.
        try:
            code = process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            raise BackendError("Hermes 超时未返回（%d 秒）" % self.timeout)
        reader.join(timeout=2)
        if code not in (0, None) and not answer:
            # The child's own traceback is the most useful thing we can show.
            tail = "\n".join(stderr_lines[-8:])[:600] or ("退出码 " + str(code))
            raise BackendError("Hermes 出错：" + tail)

        answer = (answer or "").strip()
        if not answer:
            raise BackendError("Hermes 没有返回内容。")
        if tool_count:
            report(on_progress, "done", "")
        return answer

    def cancel(self, request_id):
        with self._lock:
            process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
                return True
            except OSError:
                return False
        return False

    def close(self):
        self.cancel(None)
