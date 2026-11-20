"""Child processes inherit the security policy of the user session."""
import os


def clean_env(base=None):
    return dict(os.environ if base is None else base)
