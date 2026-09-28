"""Hermes Zoom Meeting Chat plugin entry point.

Importing this package has no network, process, config, or filesystem side
effects. register(ctx) wires the platform and registers the declared management
tools; the tools themselves fail closed until the gateway-owned adapter runtime
is connected.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _without_plugin_root_on_sys_path():
    """Prevent top-level tools.py from shadowing Hermes's tools package.

    Some Hermes validation/import paths run with the plugin directory as cwd.
    This repository must still ship tools.py, so temporarily removing that cwd
    entry before importing Hermes gateway modules keeps `tools.registry` and
    friends bound to Hermes core, not this plugin file.
    """
    here = Path(__file__).resolve().parent
    removed: list[tuple[int, str]] = []
    for idx in range(len(sys.path) - 1, -1, -1):
        entry = sys.path[idx]
        try:
            if Path(entry or ".").resolve() == here:
                removed.append((idx, entry))
                del sys.path[idx]
        except OSError:
            continue
    try:
        yield
    finally:
        for idx, entry in reversed(removed):
            sys.path.insert(min(idx, len(sys.path)), entry)


def register(ctx):
    with _without_plugin_root_on_sys_path():
        from .adapter import register as _register_adapter
        from .tools import register_tools

    _register_adapter(ctx)
    register_tools(ctx)
