"""Plugin loader: packages exposing the `rbit.plugins` entry-point group.

A plugin is a callable `setup(app)` receiving the RbitApp instance; it may register
extra slash commands via `app.storage` custom commands or append bindings."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("rbit.plugins")


def load_plugins(app: Any) -> list[str]:
    """Loads every installed `rbit.plugins` entry point; returns plugin names."""
    loaded: list[str] = []
    try:
        from importlib.metadata import entry_points

        eps = entry_points(group="rbit.plugins")
    except Exception:
        return loaded
    for ep in eps:
        try:
            obj = ep.load()
            obj(app)
            loaded.append(ep.name)
            logger.info("plugin loaded: %s", ep.name)
        except Exception:
            logger.debug("plugin %s failed", getattr(ep, "name", "?"), exc_info=True)
    return loaded
