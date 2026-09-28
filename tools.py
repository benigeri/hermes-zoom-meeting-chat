from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

# Hermes may import a platform plugin's deferred tools.py as a bare module. When
# the current directory is the plugin root, that bare module name also shadows
# Hermes's own top-level tools package. Expose Hermes's tools package path so
# core imports like `tools.registry` still resolve, while loading this plugin's
# sibling modules through an internal package for relative imports.
if __name__ == "tools":
    here = Path(__file__).resolve().parent
    for entry in list(sys.path):
        candidate = Path(entry or ".").resolve() / "tools"
        if candidate.parent != here and (candidate / "registry.py").is_file():
            __path__ = [str(candidate)]  # type: ignore[name-defined]
            break


def _load_sibling(module: str):
    pkg_name = "_zoom_meeting_chat_plugin"
    here = Path(__file__).resolve().parent
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(here)]  # type: ignore[attr-defined]
        sys.modules[pkg_name] = pkg
    fq = f"{pkg_name}.{module}"
    if fq in sys.modules:
        return sys.modules[fq]
    spec = importlib.util.spec_from_file_location(fq, here / f"{module}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load sibling module {module}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[fq] = mod
    spec.loader.exec_module(mod)
    return mod


try:
    from .config import ADMIN_TOOLSET
    from .runtime import get_runtime
except ImportError:  # bare tools.py import during deferred tool discovery
    ADMIN_TOOLSET = _load_sibling("config").ADMIN_TOOLSET
    get_runtime = _load_sibling("runtime").get_runtime


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True)


def _run_on_runtime(coro, timeout: float = 30.0) -> dict[str, Any]:
    runtime = get_runtime()
    if runtime is None:
        return {"ok": False, "error": "gateway adapter not connected"}
    fut = asyncio.run_coroutine_threadsafe(coro(runtime), runtime.loop)
    try:
        return fut.result(timeout=timeout)
    except Exception as exc:
        return {"ok": False, "error": f"runtime call failed: {type(exc).__name__}: {exc}"}


def zoom_chat_join(args: dict[str, Any], **_: Any) -> str:
    meeting_url = str((args or {}).get("meeting_url") or "").strip()
    if not meeting_url:
        return _json({"ok": False, "error": "meeting_url is required"})
    return _json(_run_on_runtime(lambda rt: rt.join(meeting_url), timeout=45.0))


def zoom_chat_leave(args: dict[str, Any] | None = None, **_: Any) -> str:
    confirmed_absent = (args or {}).get("confirmed_absent") is True
    return _json(_run_on_runtime(lambda rt: rt.leave(confirmed_absent=confirmed_absent), timeout=30.0))


def zoom_chat_status(args: dict[str, Any] | None = None, **_: Any) -> str:
    return _json(_run_on_runtime(lambda rt: rt.status(), timeout=10.0))


def register_tools(ctx) -> None:
    ctx.register_tool(
        name="zoom_chat_join",
        toolset=ADMIN_TOOLSET,
        description="Start Hio as a DM-only Zoom meeting chat participant through Recall.ai.",
        schema={
            "name": "zoom_chat_join",
            "description": "Join one Zoom meeting as Hio and return a one-use DM pairing phrase. Fails closed unless the gateway adapter is connected and zero model tools are configured.",
            "parameters": {
                "type": "object",
                "properties": {"meeting_url": {"type": "string", "description": "HTTPS zoom.us meeting URL"}},
                "required": ["meeting_url"],
                "additionalProperties": False,
            },
        },
        handler=zoom_chat_join,
    )
    ctx.register_tool(
        name="zoom_chat_leave",
        toolset=ADMIN_TOOLSET,
        description="Leave the active Zoom meeting chat bot, if known.",
        schema={
            "name": "zoom_chat_leave",
            "description": "Leave the active Zoom meeting chat bot. Set confirmed_absent only after manually verifying in Recall that an unknown bot is absent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "confirmed_absent": {
                        "type": "boolean",
                        "description": "Clear unresolved local state only after manually verifying in Recall that the bot is absent."
                    }
                },
                "additionalProperties": False
            },
        },
        handler=zoom_chat_leave,
    )
    ctx.register_tool(
        name="zoom_chat_status",
        toolset=ADMIN_TOOLSET,
        description="Show redacted Zoom Meeting Chat runtime status.",
        schema={
            "name": "zoom_chat_status",
            "description": "Return redacted status for the active Zoom Meeting Chat runtime.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        handler=zoom_chat_status,
    )
