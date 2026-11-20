"""Which models an assistant can answer with.

Each backend drives a CLI, and each CLI spells "use this model" differently:

* **Hermes** takes ``--model <name>`` on the command line.
* **DSH** has no such flag. The model lives in the profile's
  ``agent-default-model`` entry, which can be overridden with a patch file
  passed as ``--patch <file>``.
* **Codex** takes ``--model <name>`` on the command line, and the model it
  uses by default is written in ``~/.codex/config.toml``.

This module holds the knowledge both need: the candidate models, the current
one, and -- for DSH -- the patch file that carries the choice.

Everything here reads and writes files under the user's home directory, and
that is deliberate: the model list is whatever the installed CLI reports, not
a list hard-coded here that would go stale.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

# Hermes keeps its catalogue of models in a cache directory.
HERMES_HOME = Path.home() / ".hermes"

# The endpoint metadata cache maps an inference endpoint to the models it
# serves. The key is the base url of the endpoint.
HERMES_ENDPOINT_CACHE = HERMES_HOME / "cache" / "endpoint_model_metadata.json"

# The DSH patch file the backend passes with --patch. It lives next to the
# other runtime files so it is not committed and can be rebuilt at any time.
DSH_PATCH_FILE = "dsh-model.patch.yml"

# The models DSH's official provider serves. The profile picks one of them
# by name; anything else is rejected by the provider.
DSH_MODELS = (
    "deepseek-flash",
    "deepseek-chat",
    "deepseek-reasoner",
)

# The model the headless profile ships with.
DSH_DEFAULT_MODEL = "deepseek-flash"

# The provider that serves those models. A patch replaces the whole
# configuration of the plugin it targets, so the provider has to be in
# every patch: leaving it out makes the plugin fail to start.
DSH_DEFAULT_PROVIDER = "deepseek-official"

# Codex keeps its settings in ~/.codex/config.toml. The model it uses by
# default is the ``model`` key at the top level.
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
CODEX_CONFIG = CODEX_HOME / "config.toml"

# The CLI downloads the catalogue its signed-in account can reach into this
# cache. It holds one entry per model with a slug (the name the CLI takes),
# a display name and a description -- exactly what the console should offer.
CODEX_MODELS_CACHE = CODEX_HOME / "models_cache.json"

# Slugs that are not chat models a user would pick for a conversation.
# "codex-auto-review" reviews pull requests on its own; offering it as the
# speaker's brain would only confuse.
CODEX_NON_CHAT_SLUGS = ("codex-auto-review",)

# Fallback for when the cache is missing or unreadable: the workhorse and
# the cheap one. The console still allows typing any other name by hand.
CODEX_FALLBACK_MODELS = ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna")

# The model key in config.toml, matched without needing a TOML parser.
CODEX_MODEL_LINE = re.compile(r"^\s*model\s*=\s*\"([^\"]+)\"", re.M)



def _read_json(path):
    """Read a JSON file, or return None when it is missing or broken."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# The endpoints Hermes knows, and the provider each one belongs to.
#
# A model name alone is not enough to reach it: Hermes sends the name to
# the provider it is currently on, and an endpoint that does not know
# the name answers with an error, after which Hermes falls back to
# another model. So the endpoint a model lives on has to travel with
# the name -- and it is the provider, not the name, that decides where
# the request goes.
HERMES_ENDPOINTS = (
    # (base url, provider)
    ("https://inference-api.nousresearch.com/v1", "nous"),
    ("https://api.minimaxi.com/anthropic", "minimax-cn"),
    ("https://api.minimax.io/anthropic", "minimax"),
)


def hermes_model_endpoints():
    """Which provider serves which model.

    The cache holds one entry per endpoint, so it knows the endpoint a
    model lives on. Hermes needs that: the model name goes to the
    provider it is currently on, and an endpoint that does not know the
    name rejects it.
    """
    data = _read_json(HERMES_ENDPOINT_CACHE)
    if not isinstance(data, dict):
        return {}
    provider_of = {url: provider for url, provider in HERMES_ENDPOINTS}
    result = {}
    for url, endpoint in data.items():
        if not isinstance(endpoint, dict):
            continue
        models = endpoint.get("models")
        if not isinstance(models, dict):
            continue
        provider = provider_of.get(url)
        if not provider:
            # An endpoint we do not know a provider for: the name alone
            # is all there is to go on.
            provider = url
        for name in models:
            if name and name not in result:
                result[name] = provider
    return result


def hermes_models():
    """The models Hermes can currently use, most recent first.

    The list comes from the endpoint metadata cache: it holds exactly the
    models the configured provider serves, so it never offers a model the
    current account cannot reach. When the cache is missing the list is
    empty and the user can still type a model name by hand.
    """
    data = _read_json(HERMES_ENDPOINT_CACHE)
    if not isinstance(data, dict):
        return []
    names = []
    for endpoint in data.values():
        if not isinstance(endpoint, dict):
            continue
        models = endpoint.get("models")
        if not isinstance(models, dict):
            continue
        for name in models:
            if name and name not in names:
                names.append(name)
    return names


def hermes_current_model():
    """The model Hermes would use if nothing overrides it."""
    try:
        result = subprocess.run(
            ["hermes", "config", "get", "model.default"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def dsh_models():
    """The models DSH's provider serves."""
    return list(DSH_MODELS)


def dsh_current_model():
    """The model DSH would use if nothing overrides it.

    The headless profile ships with a default, so the console can show
    which model answers before the user picks one.
    """
    return DSH_DEFAULT_MODEL


def codex_models():
    """The models the console offers for Codex.

    The list comes from the CLI's own catalogue cache, so it holds exactly
    the models the signed-in account can reach and never offers one that
    answers with a failure. When the cache is missing the fallback list
    steps in, and the user can still type a model name by hand.
    """
    data = _read_json(CODEX_MODELS_CACHE)
    if isinstance(data, dict) and isinstance(data.get("models"), list):
        names = []
        for item in data["models"]:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or "").strip()
            if not slug or slug in CODEX_NON_CHAT_SLUGS or slug in names:
                continue
            names.append(slug)
        if names:
            return names
    return list(CODEX_FALLBACK_MODELS)


def codex_current_model():
    """The model Codex would use if nothing overrides it.

    It is written in ``~/.codex/config.toml``; the file is read rather than
    parsed with a TOML library so that a setting this project does not
    understand cannot break the console.
    """
    try:
        text = CODEX_CONFIG.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = CODEX_MODEL_LINE.search(text)
    return match.group(1) if match else ""



def write_dsh_patch(path, model, provider=""):
    """Write the patch file that makes DSH use ``model``.

    The profile's ``agent-default-model`` entry carries both the provider
    and the model. A patch **replaces** the whole configuration of the
    entry it targets rather than merging into it, so the provider has to
    be written every time: a patch with only the model leaves the
    provider missing and the plugin refuses to start.
    """
    lines = ["- id: agent-default-model", "  config:"]
    lines.append("    provider: %s" % (provider or DSH_DEFAULT_PROVIDER))
    lines.append("    model: %s" % model)
    text = "\n".join(lines) + "\n"
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name("." + target.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, target)
    return str(target)
