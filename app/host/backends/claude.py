"""Claude backend (克劳德).

Drives the locally installed Claude Code CLI in headless mode::

    claude -p --output-format json [--resume <session-id>] [--model <name>] -- <prompt>

``-p`` prints one JSON result object and exits, so there is no event
stream to parse. The result carries ``result`` (the answer), ``session_id``
(for follow-up turns via ``--resume``), ``is_error`` and a ``subtype``.
The host keeps the session id in its state store so a follow-up question
keeps its context, without touching the user's own terminal sessions.

Headless turns have no approval channel: ``--permission-prompts none``
makes anything that would prompt be denied instead of hanging forever.
The permission mode is configurable (default ``acceptEdits``).

The CLI needs its own login (``claude /login`` in a terminal). An
unauthenticated run returns a normal result object whose text is
"Not logged in · Please run /login", so it is translated here into a
sentence the speaker can read out loud.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading

from backends.base import Backend, BackendError, report

# Model aliases Claude Code accepts on --model. The console also lets the
# user type a full model name by hand, so this list is only a shortcut.
CLAUDE_MODELS = ("sonnet", "opus", "haiku", "fable")

# The launcher is a self-contained shell script under ~/.local/bin, which
# is on PATH for both Ghostty and the host. The npm global bin directory
# is not, so keep these as fallbacks (same pattern as backends/codex.py).
CLAUDE_PATHS = (
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)


def _terminate(process):
    """Ask a process to stop, then make sure it does."""
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=3)
    except Exception:
        try:
            process.kill()
        except OSError:
            pass


def readable_error(value) -> str:
    """The CLI sometimes nests the message inside a JSON string."""
    if isinstance(value, dict):
        return readable_error(value.get("message") or value.get("error") or "")
    if isinstance(value, list):
        return "; ".join(readable_error(item) for item in value if item)
    text = str(value or "").strip()
    if text.startswith("{"):
        try:
            return readable_error(json.loads(text))
        except ValueError:
            pass
    return text


class ClaudeBackend(Backend):
    id = "claude"
    name = "克劳德"

    def __init__(self, settings, state_store=None):
        self.settings = settings or {}
        self.state_store = state_store
        self.command = list(self.settings.get("command")
                            or ["claude", "-p", "--output-format", "json"])
        self.timeout = int(self.settings.get("timeout_seconds") or 300)
        self.keep_session = bool(self.settings.get("keep_session", True))
        self.model = str(self.settings.get("model") or "").strip()
        self.permission_mode = str(self.settings.get("permission_mode")
                                   or "acceptEdits")
        self.session_key = str(self.settings.get("session_key")
                               or "claude_session")
        self.session_id = ""
        self._process = None
        self._lock = threading.Lock()

    # -- configuration --------------------------------------------------------

    def executable(self):
        """Locate the claude binary (the launcher wins over PATH)."""
        first = str(self.command[0]) if self.command else "claude"
        if os.sep in first:
            return first if os.path.exists(first) else None
        launcher = os.path.expanduser("~/.local/bin/" + first)
        if os.path.exists(launcher):
            return launcher
        found = shutil.which(first)
        if found:
            return found
        for candidate in CLAUDE_PATHS:
            path = os.path.expanduser(candidate)
            if os.path.exists(path):
                return path
        return None

    def health(self):
        path = self.executable()
        if not path:
            return False, "找不到 claude 命令，请先安装 Claude Code"
        return True, path

    def models(self):
        names = list(CLAUDE_MODELS)
        if self.model and self.model not in names:
            names.insert(0, self.model)
        return names

    def current_model(self):
        return self.model

    def switch_model(self, name):
        """Change the model. A resumed session keeps its original model, so
        switching drops the session and the next turn starts a fresh one."""
        name = str(name or "").strip()
        if name == self.model:
            return self.model
        if self.session_id or self._stored_session():
            self.reset_session()
        self.model = name
        return self.model

    # -- session --------------------------------------------------------------

    def _stored_session(self):
        if self.state_store is None:
            return ""
        return self.state_store.get(self.session_key) or ""

    def _remember_session(self, session_id):
        if not session_id or self.state_store is None:
            return
        self.state_store[self.session_key] = session_id

    def reset_session(self):
        """Forget the conversation; the next turn starts a fresh one."""
        self.session_id = ""
        if self.state_store is not None:
            self.state_store.pop(self.session_key, None)

    # -- running --------------------------------------------------------------

    def build_argv(self, text, session_id):
        argv = [self.executable() or "claude"] + list(self.command[1:])
        # A resumed session keeps its model, so --model only makes sense on
        # a fresh turn (same rule as the Codex backend).
        if session_id:
            argv += ["--resume", session_id]
        elif self.model:
            argv += ["--model", self.model]
        argv += ["--permission-mode", self.permission_mode]
        argv += ["--permission-prompts", "none"]
        # A sentence starting with a dash would be read as a flag.
        argv.append("--")
        argv.append(text)
        return argv

    def build_env(self):
        from sandbox import clean_env
        env = clean_env()
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("TERM", "dumb")
        # The WorkBuddy agent injects its own session id; Claude Code must
        # not inherit a conversation that belongs to a different process.
        env.pop("CLAUDE_SESSION_ID", None)
        return env

    def _log_path(self):
        root = self.settings.get("_root")
        if not root:
            return None
        path = os.path.join(str(root), "..", "runtime", "claude.log")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            return None
        return path

    def run(self, text, request_id, on_progress=None):
        """Answer one turn. A session the CLI no longer knows about makes
        every resume fail, so a dead session is dropped and retried once."""
        try:
            return self._run_once(text, request_id, on_progress)
        except BackendError as error:
            if self._session_gone(str(error)) and (
                    self.session_id or self._stored_session()):
                self.reset_session()
                return self._run_once(text, request_id, on_progress)
            raise

    @staticmethod
    def _session_gone(message):
        lowered = message.lower()
        return any(marker in lowered for marker in
                   ("no conversation found", "session not found",
                    "unknown session", "invalid session",
                    "no such session", "expired"))

    @staticmethod
    def friendly_error(message):
        """Turn the CLI's error text into something speakable."""
        text = (message or "").strip()
        lowered = text.lower()
        if "not logged in" in lowered or "/login" in lowered or "unauthorized" in lowered:
            return "克劳德还没登录，请在电脑终端运行 claude 登录后重试"
        if not text:
            return "克劳德没有返回内容"
        return "克劳德出错：" + text[:240]

    @staticmethod
    def parse_result(raw):
        """Return (answer, session_id, error_text) from the CLI's stdout.

        ``-p --output-format json`` prints one result object. Some builds
        prepend a warning line, so fall back to scanning lines for the last
        JSON object that looks like a result.
        """
        stripped = (raw or "").strip()
        candidate = None
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    candidate = parsed
            except ValueError:
                pass
        if candidate is None:
            for line in stripped.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and (
                        parsed.get("type") == "result"
                        or "result" in parsed or "is_error" in parsed):
                    candidate = parsed
        if candidate is None:
            return "", "", "没有读到有效结果"
        answer = str(candidate.get("result") or "").strip()
        session_id = str(candidate.get("session_id") or "").strip()
        error = ""
        if candidate.get("is_error"):
            error = readable_error(answer or candidate.get("subtype"))
        elif not answer:
            error = readable_error(candidate.get("terminal_reason")
                                   or candidate.get("subtype"))
        return answer, session_id, error

    def _run_once(self, text, request_id, on_progress=None):
        path = self.executable()
        if not path:
            raise BackendError("找不到 claude 命令，请先安装 Claude Code。")
        if not (text or "").strip():
            raise BackendError("没有听到任务内容。")

        session_id = self.session_id or self._stored_session()
        argv = self.build_argv(text, session_id)
        report(on_progress, "thinking", "正在理解任务")

        cwd = self.settings.get("cwd")
        if cwd and not os.path.isdir(str(cwd)):
            # A relative "workspace" is resolved against the host dir, just
            # like the other CLI backends started from app/host.
            root = self.settings.get("_root")
            candidate = os.path.join(str(root or ""), str(cwd))
            cwd = candidate if os.path.isdir(candidate) else None
        env = self.build_env()
        log_path = self._log_path()

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
                start_new_session=True,
            )
        except OSError as exc:
            raise BackendError("无法启动 Claude：" + type(exc).__name__)

        with self._lock:
            self._process = process

        stderr_lines = []

        def drain_stderr():
            try:
                for line in process.stderr:
                    entry = line.rstrip("\r\n")
                    stderr_lines.append(entry)
                    if log_path:
                        try:
                            with open(log_path, "a", encoding="utf-8") as handle:
                                handle.write(entry + "\n")
                        except OSError:
                            pass
            except Exception:
                pass

        reader = threading.Thread(target=drain_stderr,
                                  name="claude-stderr", daemon=True)
        reader.start()

        try:
            stdout, _ = process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            _terminate(process)
            raise BackendError("克劳德超时未返回（%d 秒）" % self.timeout)
        finally:
            with self._lock:
                self._process = None
        reader.join(timeout=2)

        code = process.returncode
        answer, new_session, error = self.parse_result(stdout)
        if new_session and self.keep_session:
            self.session_id = new_session
            self._remember_session(new_session)
        if error:
            raise BackendError(self.friendly_error(error))
        if code not in (0, None) and not answer:
            tail = "\n".join(stderr_lines[-8:])[:400] or ("退出码 " + str(code))
            raise BackendError(self.friendly_error(tail))
        if not answer:
            raise BackendError("克劳德没有返回内容")
        report(on_progress, "speaking", "")
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
