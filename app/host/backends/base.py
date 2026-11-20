"""The contract every assistant backend implements.

A backend receives one spoken sentence and returns the answer as plain text.
It may report progress along the way so the screen can show what the desktop
agent is doing -- the user watches the agent work without looking at the
computer.

Progress callbacks use the screen state names from
``docs/contract/avatar-states.md``:

    idle / listening / thinking / working / speaking / confirmation / done / error
"""
from __future__ import annotations


class BackendError(RuntimeError):
    """Raised when a backend cannot answer. The message is safe to show."""


class Backend:
    """Base class. Subclasses override :meth:`run`."""

    #: Assistant id, matching a key in ``config.REGISTRY``.
    id = ""

    #: Display name shown on the speaker.
    name = ""

    def run(self, text, request_id, on_progress=None):
        """Answer one turn.

        text         the user's sentence, wake word already stripped
        request_id   unique id for this turn
        on_progress  optional ``callable(state, detail)`` for screen updates

        Returns the answer as plain text. Raises :class:`BackendError` on
        failure.
        """
        raise NotImplementedError

    def cancel(self, request_id):
        """Best-effort interruption of a running turn."""

    def close(self):
        """Release any resources."""

    # -- optional capabilities ------------------------------------------------

    def list_approvals(self):
        """Pending approvals, as a list of dicts. Empty when unsupported."""
        return []

    def resolve_approval(self, approval_id, decision):
        """Approve or reject one pending item. False when unsupported."""

    def models(self):
        """Available model names. Empty when unsupported."""
        return []

    def switch_model(self, name):
        """Change the model. False when unsupported."""
        return False

    def health(self):
        """Return (ok, detail) describing whether the backend can be used."""
        return True, "ok"


def report(on_progress, state, detail=""):
    """Call the progress callback, never letting it break the turn."""
    if on_progress is None:
        return
    try:
        on_progress(state, detail)
    except Exception:
        pass
