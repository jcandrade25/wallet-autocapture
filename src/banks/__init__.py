# -*- coding: utf-8 -*-
"""
Bank-adapter registry.

A bank adapter is a Python module in this package that exposes:
    SENDERS : list[str]          # From-address substrings to search for
    parse(text: str) -> dict|None  # one alert snippet -> NORMALIZED TXN or None

Adapters are loaded dynamically by name (``config.bank_adapter``), so adding
support for a new bank is just dropping a ``src/banks/<name>.py`` file in here
(copy ``TEMPLATE.py``) — no edits to the rest of the codebase required.
"""
import importlib


def get_adapter(name):
    """Import and return the bank-adapter module named ``name``.

    ``name`` is the bare module name (e.g. ``"bancolombia"``), matching the
    file ``src/banks/<name>.py`` and the ``bank_adapter`` key in config.json.

    Raises ``ValueError`` with a helpful message if the adapter does not exist
    or is missing the required ``parse``/``SENDERS`` interface.
    """
    if not name:
        raise ValueError("No bank adapter configured (config.bank_adapter is empty).")
    # Guard against path tricks / accidental sub-package names.
    if not name.replace("_", "").isalnum():
        raise ValueError(f"Invalid bank adapter name: {name!r}")
    try:
        module = importlib.import_module(f"{__name__}.{name}")
    except ImportError as exc:
        raise ValueError(
            f"Unknown bank adapter {name!r}: no module src/banks/{name}.py. "
            f"Copy src/banks/TEMPLATE.py to create one."
        ) from exc
    if not hasattr(module, "parse"):
        raise ValueError(f"Bank adapter {name!r} does not define parse(text).")
    if not hasattr(module, "SENDERS"):
        raise ValueError(f"Bank adapter {name!r} does not define SENDERS.")
    return module
