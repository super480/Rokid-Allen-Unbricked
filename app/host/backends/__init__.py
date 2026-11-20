"""Backend registry.

Maps an assistant id to the module that implements it. Adding a backend means
adding one entry here -- nothing else in the host changes.
"""
from __future__ import annotations

from backends.base import Backend, BackendError
from backends.claude import ClaudeBackend
from backends.codex import CodexBackend
from backends.dsh import DshBackend
from backends.ha_assistant import HaAssistantBackend
from backends.hermes import HermesBackend
from backends.neko import NekoBackend
from backends.workbuddy import WorkBuddyBackend

# id -> (module path, class name)
BACKENDS = {
    "hermes": ("backends.hermes", "HermesBackend"),
    "codex": ("backends.codex", "CodexBackend"),
    "dsh": ("backends.dsh", "DshBackend"),
    "workbuddy": ("backends.workbuddy", "WorkBuddyBackend"),
    "neko": ("backends.neko", "NekoBackend"),
    "xiaojia": ("backends.ha_assistant", "HaAssistantBackend"),
    "claude": ("backends.claude", "ClaudeBackend"),
}


def load_backend(backend_id, settings=None, state_store=None):
    """Instantiate a backend by id. Raises BackendError when unavailable."""
    entry = BACKENDS.get(backend_id)
    if not entry:
        raise BackendError("助手尚未接入：" + str(backend_id))
    module_name, class_name = entry
    module = __import__(module_name, fromlist=[class_name])
    factory = getattr(module, class_name)
    return factory(settings or {}, state_store)


def available():
    """The assistant ids that have a backend implementation."""
    return list(BACKENDS)
