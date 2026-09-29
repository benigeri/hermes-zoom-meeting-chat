from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import ADMIN_TOOLSET, PLATFORM


_ALLOWED_RAW_TOOL_NAMES = {"x_search"}
_ALLOWED_TOOL_SEARCH_BRIDGE_NAMES = {"tool_search", "tool_describe", "tool_call"}


@dataclass
class CompatibilityResult:
    ok: bool
    message: str
    enabled_toolsets: list[str]
    tool_names: list[str]


def zero_tool_schema_preflight(config: dict[str, Any] | None = None) -> CompatibilityResult:
    """Run Hermes's real platform resolver and model-tool schema builder.

    The adapter's toolsets_for_source() returns ["no_mcp"]. Here we apply that
    override to the live config, require plugin toolset coverage in
    known_plugin_toolsets, and then require the model-facing schema list to be
    empty or the exact Tool Search bridge produced solely by ambient x_search.
    This is intentionally fail-closed for every other tool.
    """
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.tools_config import _get_platform_tools, _get_plugin_toolset_keys
        from model_tools import get_tool_definitions
    except Exception as exc:
        return CompatibilityResult(False, f"Hermes tool resolver unavailable: {exc}", [], [])

    cfg = dict(config if config is not None else (load_config_readonly() or {}))
    context_cfg = cfg.get("context") or {}
    engine = str(context_cfg.get("engine") or "compressor").strip().lower() if isinstance(context_cfg, dict) else "compressor"
    if engine and engine != "compressor":
        return CompatibilityResult(False, "Zoom Meeting Chat requires context.engine: compressor", [], [])

    plugin_keys = set(_get_plugin_toolset_keys())
    known = set(((cfg.get("known_plugin_toolsets") or {}).get(PLATFORM) or []))
    missing = sorted(plugin_keys - known)
    if missing:
        return CompatibilityResult(
            False,
            "Zoom Meeting Chat requires known_plugin_toolsets.zoom_meeting_chat to include all plugin toolsets: " + ", ".join(missing),
            [],
            [],
        )

    pts = dict(cfg.get("platform_toolsets") or {})
    pts[PLATFORM] = ["no_mcp"]
    cfg["platform_toolsets"] = pts
    enabled = sorted(_get_platform_tools(cfg, PLATFORM))
    raw_schemas = get_tool_definitions(
        enabled_toolsets=enabled,
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    raw_names = [s.get("function", {}).get("name", "") for s in raw_schemas]
    unsupported_raw = sorted(set(raw_names) - _ALLOWED_RAW_TOOL_NAMES)
    if unsupported_raw:
        return CompatibilityResult(
            False,
            "Zoom Meeting Chat found unsupported model tool schemas: " + ", ".join(unsupported_raw),
            enabled,
            raw_names,
        )

    schemas = get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True)
    names = [s.get("function", {}).get("name", "") for s in schemas]
    allowed_final = not names or (
        set(raw_names) == {"x_search"}
        and set(names) == _ALLOWED_TOOL_SEARCH_BRIDGE_NAMES
        and len(names) == len(_ALLOWED_TOOL_SEARCH_BRIDGE_NAMES)
    )
    if not allowed_final:
        return CompatibilityResult(
            False,
            "Zoom Meeting Chat found unsupported model tool schemas: " + ", ".join(names),
            enabled,
            names,
        )
    mode = "ambient x_search through Tool Search bridge" if names else "zero model tool schemas"
    return CompatibilityResult(True, f"compatible: {mode}", enabled, names)
