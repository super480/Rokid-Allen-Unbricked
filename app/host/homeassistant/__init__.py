"""Home Assistant: the house's devices as one voice entry point.

Standard library only. The bridge never judges which device or which
action -- a home turn's whole sentence goes to HA's conversation agent.
Local code only routes (``routing``) and presents (``cards``).

    models.py    HaResult status machine + base_url validation
    client.py    REST transport with typed errors, injectable for tests
    routing.py   dual gate: verb + home word -> is this a home turn?
    cards.py     HaResult -> lines card + one brief spoken line
    service.py   config, console API, command queue, presentation

Nothing in this package may touch a real device in tests
(00-START-HERE §5.8).
"""
from .client import (HaAuthError, HaClient, HaError, HaNetworkError,
                     HaNotFoundError, HaProtocolError)
from .models import CARD_STATUS, HaResult, normalise_base_url
from .routing import Gate
from .service import HomeAssistantService
