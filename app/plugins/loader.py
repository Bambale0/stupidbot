from __future__ import annotations

import importlib
import logging
from types import ModuleType

from aiogram import Dispatcher

from app.context import AppContext

logger = logging.getLogger(__name__)


def _import_plugin(name: str) -> ModuleType:
    return importlib.import_module(f"app.plugins.{name}.plugin")


def normalized_plugin_names(configured: list[str]) -> list[str]:
    plugins: list[str] = []
    for item in configured:
        name = str(item).strip()
        if name and name not in plugins:
            plugins.append(name)

    if "generation" in plugins and "references" not in plugins:
        plugins.insert(plugins.index("generation") + 1, "references")

    if "ux" in plugins:
        plugins = [name for name in plugins if name != "ux"]
        plugins.append("ux")
    return plugins


def load_plugins(dispatcher: Dispatcher, context: AppContext) -> list[str]:
    plugin_names = normalized_plugin_names(context.settings.enabled_plugins)
    context.settings.enabled_plugins = plugin_names
    loaded: list[str] = []
    for name in plugin_names:
        module = _import_plugin(name)
        setup = getattr(module, "setup", None)
        if setup is None:
            raise RuntimeError(f"Plugin {name!r} has no setup(dispatcher, context)")
        setup(dispatcher, context)
        loaded.append(name)
        logger.info("Loaded bot plugin: %s", name)
    return loaded
