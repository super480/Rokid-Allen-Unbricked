"""DeepSeek Harness (DSH) backend.

Drives the locally installed ``dsh`` CLI in its headless profile. Like
Hermes, DSH is a full desktop agent -- it can run commands, read files and
browse -- so it fits the same role: the speaker becomes an input device for
an agent that already knows how to use the computer.

The CLI surface differs from Hermes, so the mapping is:

============  ==========================================
Hermes        DSH
============  ==========================================
``--oneshot`` ``headless --json``
``--resume``  ``--session-id``
``result``    ``final``
``tool_use``  ``tool_call`` / ``tool_result``
============  ==========================================

Two behaviours matter for the speaker:

* the session is kept between turns, so a follow-up question has context;
* the tool stream is reported as progress, so the screen shows what the
  agent is doing instead of a frozen "thinking" face.

One environment detail is decisive. DSH runs commands inside a sandbox by
default (``workspace-write``). On a macOS host without a usable sandbox
backend every command fails with ``SANDBOX_UNAVAILABLE`` and the agent
retries until the turn times out. ``DSH_PERMISSION_MODE`` selects the mode
and also decides whether an approval channel is needed at all, so the
backend sets it explicitly rather than inheriting whatever the shell
happened to have.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time

from backends import models
from backends.base import Backend, BackendError, report
from sandbox import clean_env

# Tool names mapped to a short phrase for the 640x480 screen.
TOOL_LABELS = {
    "bash": "执行命令",
    "glob": "查找文件",
    "grep": "搜索内容",
    "read": "读取文件",
    "read_file": "读取文件",
    "write": "写入文件",
    "write_file": "写入文件",
    "edit": "修改文件",
    "str_replace": "修改文件",
    "web_search": "联网搜索",
    "web_fetch": "抓取网页",
    "todo": "整理任务",
    "skill": "调用技能",
    "task": "派生子任务",
    "ask_user": "询问用户",
}

# A session id the CLI refuses to adopt. The session is then unusable and
# the turn has to start a fresh conversation.
SESSION_GONE = "does not exist"

# The session file exists but cannot be written right now. Two causes, both
# of which a fresh session side-steps:
#
# * another process still holds the session's write lock -- a previous turn
#   that was killed, or a second host running against the same session;
# * the session was recorded under a different working directory, so the
#   CLI refuses to resume it here.
#
# Without this, the assistant stays unusable for good: every turn fails the
# same way and the session is never dropped.
SESSION_UNUSABLE = (
    "already owned by an active write handle",
    "was recorded in",
)

# The upstream stream ended before the model finished its turn. The turn is
# lost but the session is fine, so it is worth one more try.
STREAM_ENDED_EARLY = "stream ended before message_stop"

# The upstream service is overloaded. Nothing is wrong with the session or
# the request, so the same turn is worth one more try.
UPSTREAM_BUSY = (
    "Service is too busy",
    "SERVER: Service is too busy",
)

# How long to wait before asking again after the upstream service refused
# the request because it was overloaded.
RETRY_PAUSE = 5.0

# How many times one turn may be attempted. A retry that is not bounded
# turns a temporary overload into a turn that never ends.
MAX_ATTEMPTS = 3


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


class DshBackend(Backend):
    id = "dsh"
    name = "大肥鱼"

    def __init__(self, settings, state_store=None):
        self.settings = settings or {}
        self.state_store = state_store
        self.command = list(self.settings.get("command") or
                            ["dsh", "headless", "--json"])
        self.timeout = int(self.settings.get("timeout_seconds") or 180)
        self.keep_session = bool(self.settings.get("keep_session", True))
        self.permission_mode = str(
            self.settings.get("permission_mode") or "workspace-write")
        # The model to answer with, and the provider that serves it. Empty
        # means "whatever the profile is configured to use".
        self.model = str(self.settings.get("model") or "").strip()
        self.model_provider = str(self.settings.get("model_provider") or "").strip()
        # The key the session id is stored under. Direct mode passes its own
        # key so its conversation stays apart from the assistant's.
        self.session_key = str(self.settings.get("session_key") or "dsh_session")
        self.session_id = ""
        self._process = None
        self._lock = threading.Lock()

    # -- configuration --------------------------------------------------------

    def executable(self):
        """Locate the dsh binary.

        A configured absolute path is used as is. A configured program name
        is looked up in the known locations first and in PATH second: the
        launcher under ``~/.local/bin`` is self-contained and survives a
        managed-node upgrade, while a copy inside a managed-node directory
        disappears with it. A configured command never silently runs a
        different program.
        """
        configured = self.settings.get("command")
        if configured:
            first = str(configured[0])
            if os.path.sep in first:
                return first if os.path.exists(first) else None
            # A configured program name is honoured. The known launcher only
            # counts when it is the program that was asked for, so a
            # misconfigured command never silently runs a different binary.
            if os.path.basename(first) == "dsh":
                found = self._known_paths()
                if found:
                    return found
            return shutil.which(first)
        found = self._known_paths()
        if found:
            return found
        return shutil.which("dsh")

    @staticmethod
    def _known_paths():
        """The self-contained launcher, in the order it should be tried."""
        for candidate in (os.path.expanduser("~/.local/bin/dsh"),
                          "/usr/local/bin/dsh",
                          "/opt/homebrew/bin/dsh"):
            if os.path.exists(candidate):
                return candidate
        return None

    def health(self):
        path = self.executable()
        if not path:
            return False, "找不到 dsh 命令"
        return True, path

    # -- session --------------------------------------------------------------

    def _stored_session(self):
        if self.state_store is None:
            return ""
        value = self.state_store.get(self.session_key)
        return value or ""

    def _remember_session(self, session_id):
        if not session_id or self.state_store is None:
            return
        self.state_store[self.session_key] = session_id

    def reset_session(self):
        """Forget the session; the next turn starts a fresh conversation."""
        self.session_id = ""
        if self.state_store is not None:
            self.state_store.pop(self.session_key, None)

    # -- running --------------------------------------------------------------

    def _log_path(self):
        """Where to keep the child's stderr, if a log directory is known."""
        root = self.settings.get("_root")
        if not root:
            return None
        path = os.path.join(str(root), "..", "runtime", "dsh.log")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            return None
        return path

    def build_argv(self, text, session_id):
        argv = [self.executable() or "dsh", "headless"]
        # The patch flag has to sit between the profile name and the
        # profile's own flags: "dsh --patch x headless" is rejected,
        # because the launcher has not been told which profile to boot.
        argv += self.build_model_argv()
        argv += ["--json"]
        if session_id:
            argv += ["--session-id", session_id]
        argv += [text]
        return argv

    def model_patch_path(self):
        """Where the patch file that carries the model choice lives."""
        root = self.settings.get("_root")
        if not root:
            return None
        return os.path.join(str(root), "..", "runtime", models.DSH_PATCH_FILE)

    def build_model_argv(self):
        """The extra arguments that make DSH use the chosen model.

        The headless profile has no command line flag for the model, so the
        choice travels in a patch file. The flag has to come *after* the
        profile name: ``dsh --patch x headless`` is rejected, because the
        launcher has not been told which profile to boot yet.
        """
        if not self.model:
            return []
        path = self.model_patch_path()
        if not path:
            return []
        try:
            models.write_dsh_patch(path, self.model, self.model_provider)
        except OSError:
            return []
        return ["--patch", str(path)]

    # -- models ---------------------------------------------------------------

    def models(self):
        """The models this assistant can answer with."""
        from backends import models as model_catalog
        names = model_catalog.dsh_models()
        if self.model and self.model not in names:
            names.insert(0, self.model)
        return names

    def current_model(self):
        """The model this assistant answers with right now."""
        if self.model:
            return self.model
        from backends import models as model_catalog
        return model_catalog.dsh_current_model()

    def switch_model(self, name):
        """Change the model. Returns the model now in use."""
        self.model = str(name or "").strip()
        return self.model

    def build_env(self):
        """The child's environment.

        ``DSH_PERMISSION_MODE`` decides both the sandbox mode and whether an
        approval channel is required. Leaving it unset means the CLI falls
        back to ``workspace-write`` and asks for approval -- and the headless
        profile has no approval channel, so every command would fail.
        """
        env = clean_env()
        env["DSH_PERMISSION_MODE"] = self.permission_mode
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    def working_directory(self):
        """Where the assistant runs, as an absolute path.

        The CLI records the session under the directory it ran in, and it
        refuses to resume a session recorded elsewhere ("session ... was
        recorded in X, not Y"). A relative path would therefore depend on
        the directory the host happened to be started from, and the same
        saved session would stop working after a restart. So a relative
        setting is resolved against the config folder and used absolute.
        """
        configured = str(self.settings.get("cwd") or "").strip()
        if not configured:
            return None
        path = os.path.expanduser(configured)
        if not os.path.isabs(path):
            root = str(self.settings.get("_root") or "").strip()
            base = os.path.abspath(root) if root else os.getcwd()
            path = os.path.join(base, path)
        path = os.path.abspath(path)
        return path if os.path.isdir(path) else None

    def run(self, text, request_id, on_progress=None):
        """Answer one turn, retrying a few times when the cause allows it.

        Some failures are worth another attempt: the session was lost, the
        session file is unusable, the upstream stream was cut, or the
        upstream service was overloaded. Others are the request's own
        fault and would fail the same way again.

        The number of attempts is bounded. A retry that is not bounded
        turns a temporary overload into a turn that never ends, and the
        engine then waits for an answer that will never come.
        """
        message = ""
        for attempt in range(MAX_ATTEMPTS):
            try:
                return self._run_once(text, request_id, on_progress)
            except BackendError as error:
                message = str(error)
                # A session the CLI no longer knows about makes every
                # resume fail. Drop it and start a fresh conversation
                # rather than losing the turn.
                if SESSION_GONE in message:
                    self.reset_session()
                    continue
                # The session file is there but cannot be used: another
                # process holds its write lock, or it belongs to a
                # different working directory. Resuming it again would
                # fail identically, so the session is dropped and a new
                # one started.
                if any(marker in message for marker in SESSION_UNUSABLE):
                    self.reset_session()
                    continue
                # The upstream stream was cut before the model finished.
                # The session is intact, so the same turn is worth one
                # more try.
                if STREAM_ENDED_EARLY in message:
                    continue
                # The upstream service is overloaded. The request was
                # fine, so the same turn is worth one more try -- after a
                # short pause, because asking again straight away would
                # be refused again.
                if any(marker in message for marker in UPSTREAM_BUSY):
                    time.sleep(RETRY_PAUSE)
                    continue
                raise
        raise BackendError(message or "DeepSeek 没有返回内容。")

    def _run_once(self, text, request_id, on_progress=None):
        path = self.executable()
        if not path:
            raise BackendError("找不到 dsh 命令，请在电脑上安装 DeepSeek Harness。")
        if not (text or "").strip():
            raise BackendError("没有听到任务内容。")

        cwd = self.working_directory()
        argv = self.build_argv(text, self.session_id or self._stored_session())
        report(on_progress, "thinking", "正在理解任务")

        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd if cwd and os.path.isdir(str(cwd)) else None,
                env=self.build_env(),
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
            raise BackendError("无法启动 DeepSeek：" + type(exc).__name__)

        with self._lock:
            self._process = process

        answer = ""
        tool_count = 0
        spoken = False
        stderr_lines = []
        # Keep the child's stderr on disk: when DSH fails, its traceback is
        # the only useful diagnostic, and it is gone once the process exits.
        log_path = self._log_path()

        def drain_stderr():
            try:
                for line in process.stderr:
                    text = line.rstrip("\r\n")
                    stderr_lines.append(text)
                    if log_path:
                        try:
                            with open(log_path, "a", encoding="utf-8") as handle:
                                handle.write(text + "\n")
                        except OSError:
                            pass
            except Exception:
                pass

        reader = threading.Thread(target=drain_stderr, name="dsh-stderr", daemon=True)
        reader.start()

        # stdout is read on its own thread so the main loop stays responsive
        # to the process state and can bail out early.
        lines = queue.Queue()

        def pump_stdout():
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        pump = threading.Thread(target=pump_stdout, name="dsh-stdout", daemon=True)
        pump.start()

        try:
            while True:
                try:
                    line = lines.get(timeout=0.5)
                except queue.Empty:
                    if process.poll() is not None and lines.empty():
                        break
                    continue
                if line is None:
                    break
                line = line.strip()
                if not line:
                    continue
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "session":
                    session = event.get("sessionId")
                    if session:
                        self.session_id = session
                        self._remember_session(session)
                elif kind == "thinking":
                    # DSH narrates its reasoning before every step.
                    report(on_progress, "thinking", "正在思考")
                elif kind == "tool_call":
                    tool_count += 1
                    name = event.get("tool") or "执行任务"
                    label = TOOL_LABELS.get(name, name)
                    report(on_progress, "working", label)
                elif kind == "tool_result":
                    if event.get("status") == "error":
                        report(on_progress, "working", "工具出错")
                elif kind == "text":
                    # DSH streams the answer token by token. The screen only
                    # needs to know that the answer has started.
                    if not spoken:
                        spoken = True
                        report(on_progress, "speaking", "")
                elif kind == "final":
                    answer = event.get("text") or ""
                elif kind == "error":
                    raise BackendError("DeepSeek 出错：" + str(event.get("message"))[:400])
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
            # The child ignores the end of its output and keeps running.
            # It has to be killed and reaped: a process that is killed but
            # never waited for stays a zombie, and its session stays
            # locked, so the next turn cannot use it either.
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise BackendError("DeepSeek 超时未返回（%d 秒）" % self.timeout)
        reader.join(timeout=2)
        # Close the pipes explicitly: the reader threads are done, and a
        # forgotten handle shows up as a ResourceWarning in the host log.
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        if code not in (0, None) and not answer:
            # The child's own traceback is the most useful thing we can show.
            tail = "\n".join(stderr_lines[-8:])[:600] or ("退出码 " + str(code))
            raise BackendError("DeepSeek 出错：" + tail)

        answer = (answer or "").strip()
        if not answer:
            raise BackendError("DeepSeek 没有返回内容。")
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
