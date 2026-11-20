"""Codex backend.

Drives the locally installed ``codex`` CLI in non-interactive mode
(``codex exec --json``). Codex is a desktop coding agent: it reads files,
runs commands and edits the working tree, which is exactly what the speaker
should be able to hand a task to.

The event stream is a sequence of JSON lines:

    {"type":"thread.started","thread_id":"..."}
    {"type":"turn.started"}
    {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
    {"type":"turn.completed","usage":{...}}

The thread id is what makes a follow-up question keep its context, so it is
remembered between turns and passed back with ``exec resume``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading

from backends.base import Backend, BackendError, report

# Item types the CLI reports while it works. Only the ones that say
# something about *what* is happening are mapped to a screen state.
ITEM_LABELS = {
    "command_execution": "执行命令",
    "file_change": "修改文件",
    "mcp_tool_call": "调用工具",
    "web_search": "联网搜索",
    "todo_list": "整理任务",
    "reasoning": "推理中",
}


def readable_error(message):
    """Turn whatever the CLI reported into one line a person can act on.

    The CLI nests the real reason inside a JSON string: the message of a
    failed turn is itself a JSON document holding ``error.message``. Read
    that out, so the speaker says "the model gpt-5 is not supported"
    instead of handing back the raw document.
    """
    text = str(message or "").strip()
    if not text:
        return ""
    for _ in range(2):
        if not (text.startswith("{") and text.endswith("}")):
            break
        try:
            inner = json.loads(text)
        except ValueError:
            break
        if not isinstance(inner, dict):
            break
        error = inner.get("error")
        if isinstance(error, dict) and error.get("message"):
            text = str(error["message"]).strip()
            continue
        if isinstance(error, str) and error:
            text = error.strip()
            continue
        if inner.get("message"):
            text = str(inner["message"]).strip()
            continue
        break
    return text


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


class CodexBackend(Backend):
    id = "codex"
    name = "GPT"

    def __init__(self, settings, state_store=None):
        self.settings = settings or {}
        self.state_store = state_store
        self.command = list(self.settings.get("command") or ["codex", "exec", "--json"])
        self.timeout = int(self.settings.get("timeout_seconds") or 600)
        self.keep_session = bool(self.settings.get("keep_session", True))
        self.model = str(self.settings.get("model") or "").strip()
        # The CLI refuses to run inside a repository it considers unsafe
        # unless the caller opts out. The speaker is not the one making
        # the change, so the decision belongs to the host, not the device.
        self.skip_git_check = bool(self.settings.get("skip_git_repo_check", True))
        self.session_key = str(self.settings.get("session_key") or "codex_thread")
        self.thread_id = ""
        self._process = None
        self._lock = threading.Lock()

    # -- configuration --------------------------------------------------------

    def executable(self):
        """Locate the codex binary.

        A configured command wins; otherwise the usual install locations are
        tried, because the CLI lives in a node prefix that is not always on
        the PATH of the process that starts the host.
        """
        configured = self.settings.get("command")
        if configured:
            first = str(configured[0])
            if os.path.sep in first:
                return first if os.path.exists(first) else None
            return shutil.which(first)
        found = shutil.which("codex")
        if found:
            return found
        for candidate in (os.path.expanduser("~/.local/bin/codex"),
                          "/usr/local/bin/codex",
                          "/opt/homebrew/bin/codex"):
            if os.path.exists(candidate):
                return candidate
        return None

    def health(self):
        path = self.executable()
        if not path:
            return False, "找不到 codex 命令"
        return True, path

    # -- models ---------------------------------------------------------------

    def models(self):
        """The models this assistant can answer with.

        The list comes from the CLI itself, so it never offers a model the
        signed-in account cannot reach.
        """
        from backends import models as model_catalog
        names = model_catalog.codex_models()
        if self.model and self.model not in names:
            names.insert(0, self.model)
        return names

    def current_model(self):
        if self.model:
            return self.model
        from backends import models as model_catalog
        return model_catalog.codex_current_model()

    def switch_model(self, name):
        """Change the model.

        A resumed thread keeps the model it was started with, so changing
        the model drops the thread and the next turn starts a fresh one.
        """
        name = str(name or "").strip()
        if name == self.model:
            return self.model
        if self.thread_id or self._stored_thread():
            self.reset_session()
        self.model = name
        return self.model

    # -- session --------------------------------------------------------------

    def _stored_thread(self):
        if self.state_store is None:
            return ""
        value = self.state_store.get(self.session_key)
        return value or ""

    def _remember_thread(self, thread_id):
        if not thread_id or self.state_store is None:
            return
        self.state_store[self.session_key] = thread_id

    def reset_session(self):
        """Forget the thread; the next turn starts a fresh conversation."""
        self.thread_id = ""
        if self.state_store is not None:
            self.state_store.pop(self.session_key, None)

    # -- running --------------------------------------------------------------

    def build_argv(self, text, thread_id):
        """The command line for one turn.

        A kept thread is continued with ``exec resume``; otherwise a new
        thread is started. The model flag is only set for a new thread:
        resuming keeps the model the thread was created with.

        The prompt is a positional argument. The CLI reads it from stdin
        when none is given, and the host has nothing to send there.
        """
        argv = [self.executable() or "codex", "exec", "--json"]
        if self.skip_git_check:
            argv.append("--skip-git-repo-check")
        if thread_id:
            argv += ["resume", thread_id]
        elif self.model:
            argv += ["--model", self.model]
        # A sentence that starts with a dash would be read as a flag, so
        # it is passed after "--", which ends the option list.
        argv.append("--")
        argv.append(text)
        return argv

    def build_env(self):
        from sandbox import clean_env
        env = clean_env()
        env.setdefault("PYTHONUNBUFFERED", "1")
        # The CLI refuses to run without a terminal in some versions when it
        # thinks it is interactive; a plain pipe is enough for --json.
        env.setdefault("TERM", "dumb")
        return env

    def run(self, text, request_id, on_progress=None):
        """Answer one turn.

        A thread the CLI no longer knows about makes every resume fail, so
        a dead thread is dropped and the turn is retried on a fresh one.
        """
        try:
            return self._run_once(text, request_id, on_progress)
        except BackendError as error:
            message = str(error)
            if self._thread_gone(message) and (self.thread_id or self._stored_thread()):
                self.reset_session()
                return self._run_once(text, request_id, on_progress)
            raise

    @staticmethod
    def _thread_gone(message):
        lowered = message.lower()
        return any(marker in lowered for marker in
                   ("not found", "no such thread", "unknown thread",
                    "invalid thread", "thread not found", "expired"))

    def _log_path(self):
        root = self.settings.get("_root")
        if not root:
            return None
        path = os.path.join(str(root), "..", "runtime", "codex.log")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            return None
        return path

    def _run_once(self, text, request_id, on_progress=None):
        path = self.executable()
        if not path:
            raise BackendError("找不到 codex 命令，请在电脑上安装 Codex。")
        if not (text or "").strip():
            raise BackendError("没有听到任务内容。")

        cwd = self.settings.get("cwd")
        thread_id = self.thread_id or self._stored_thread()
        argv = self.build_argv(text, thread_id)
        report(on_progress, "thinking", "正在理解任务")

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
            raise BackendError("无法启动 Codex：" + type(exc).__name__)

        with self._lock:
            self._process = process

        answer = ""
        spoken = False
        stderr_lines = []
        # What the CLI said went wrong. It reports failures in three
        # different shapes, and all three have to be read: an error item,
        # a top level error event, and turn.failed. Missing any of them
        # leaves the answer empty and the failure gets reported as the
        # last line of stderr, which is a progress message ("Reading
        # additional input from stdin...") and tells the user nothing.
        #
        # The three are not equally specific, so they are ranked. A failed
        # turn carries the reason the run stopped; an error item may only
        # say "model not found" while the turn says which model and why.
        cli_error = ""
        cli_priority = 0

        def note_error(message, priority):
            nonlocal cli_error, cli_priority
            text = readable_error(message)
            if text and priority >= cli_priority:
                cli_error = text
                cli_priority = priority

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

        reader = threading.Thread(target=drain_stderr, name="codex-stderr", daemon=True)
        reader.start()

        try:
            for line in process.stdout:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "thread.started":
                    thread = event.get("thread_id")
                    if thread:
                        self.thread_id = thread
                        self._remember_thread(thread)
                elif kind == "item.completed":
                    item = event.get("item") or {}
                    item_kind = item.get("type")
                    if item_kind == "agent_message":
                        # The CLI emits one agent_message per turn step. The
                        # last one is the answer; earlier ones are progress
                        # notes ("I'll run the command now.").
                        text = item.get("text") or ""
                        if text.strip():
                            answer = text.strip()
                        if not spoken:
                            spoken = True
                            report(on_progress, "speaking", "")
                    elif item_kind == "error":
                        # The least specific of the three: it may only say
                        # "model not found" without saying which model.
                        note_error(item.get("message"), 1)
                    else:
                        label = ITEM_LABELS.get(item_kind)
                        if label:
                            report(on_progress, "working", label)
                elif kind == "error":
                    note_error(event.get("message"), 2)
                elif kind == "turn.failed":
                    failed = event.get("error") or {}
                    # The most specific: it carries the reason the run
                    # stopped, so it wins over the two above.
                    note_error(failed.get("message") or failed, 3)
                    raise BackendError("Codex 出错：" + (cli_error or "本轮执行失败")[:400])
                elif kind == "turn.completed":
                    if event.get("error"):
                        raise BackendError("Codex 出错：" + str(event["error"])[:400])
        except BackendError:
            _terminate(process)
            raise
        finally:
            with self._lock:
                self._process = None

        try:
            code = process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            raise BackendError("Codex 超时未返回（%d 秒）" % self.timeout)
        reader.join(timeout=2)
        if code not in (0, None) and not answer:
            if cli_error:
                # The CLI said what went wrong. Say that, not the last
                # progress line it happened to print.
                raise BackendError("Codex 出错：" + cli_error[:400])
            tail = "\n".join(stderr_lines[-8:])[:600] or ("退出码 " + str(code))
            raise BackendError("Codex 出错：" + tail)

        answer = (answer or "").strip()
        if not answer:
            if cli_error:
                raise BackendError("Codex 出错：" + cli_error[:400])
            raise BackendError("Codex 没有返回内容。")
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
