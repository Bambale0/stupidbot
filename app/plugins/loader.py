from __future__ import annotations

import importlib
import logging
from types import ModuleType

from aiogram import Dispatcher

from app.context import AppContext

logger = logging.getLogger(__name__)


def _import_plugin(name: str) -> ModuleType:
    return importlib.import_module(f"app.plugins.{name}.plugin")


def load_plugins(dispatcher: Dispatcher, context: AppContext) -> list[str]:
    loaded: list[str] = []
    for name in context.settings.enabled_plugins:
        module = _import_plugin(name)
        setup = getattr(module, "setup", None)
        if setup is None:
            raise RuntimeError(f"Plugin {name!r} has no setup(dispatcher, context)")
        setup(dispatcher, context)
        loaded.append(name)
        logger.info("Loaded bot plugin: %s", name)
    return loaded

