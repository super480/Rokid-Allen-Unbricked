"""Small apps that answer a query and put one card on the Rokid screen.

The runtime is deliberately tiny and standard-library only (psutil is used
when available for Mac metrics, with a pure-stdlib CLI fallback):

    registry.py        voice phrases -> (app_id, operation, action)
    codex_usage.py     read-only Codex app-server RPC + normalisation
    system_metrics.py  Mac CPU / memory / network counters + 1s sampler
    cards.py           Codex QueryResult -> device layout JSON
    system_cards.py    network & resources cards -> the same layout JSON
    service.py         per-app dispatch, cache, presentation, command emission
    models.py          QueryResult + card schema validation

Adding another query app is three steps: write a provider module, add its
phrases/operations to ``registry.py``, and register a layout builder in
``service.py``. No image-rendering service is required; the device already
draws the general-purpose ``rings`` / ``lines`` templates natively.
"""
from .models import ERROR_CODES, STATUSES, QueryResult, validate_card
from .service import MiniAppService

__all__ = ["MiniAppService", "QueryResult", "STATUSES", "ERROR_CODES",
           "validate_card"]
