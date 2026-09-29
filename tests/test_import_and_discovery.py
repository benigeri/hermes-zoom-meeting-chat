from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys

from conftest import CHECKOUT, ROOT, load_plugin_pkg


def test_import_light_does_not_import_adapter_or_touch_network():
    pkg = load_plugin_pkg("zoom_import_light")
    assert "zoom_import_light.adapter" not in sys.modules
    assert hasattr(pkg, "register")


def test_tools_fail_closed_without_gateway_runtime():
    load_plugin_pkg("zoom_tools_closed")
    tools = __import__("zoom_tools_closed.tools", fromlist=["zoom_chat_status", "zoom_chat_join"])
    status = tools.zoom_chat_status({})
    join = tools.zoom_chat_join({"meeting_url": "https://example.zoom.us/j/123?pwd=secret"})
    assert "gateway adapter not connected" in status
    assert "gateway adapter not connected" in join


def test_calendar_join_keeps_zoom_url_out_of_tool_result(monkeypatch):
    load_plugin_pkg("zoom_tools_calendar")
    tools = __import__(
        "zoom_tools_calendar.tools",
        fromlist=["zoom_chat_join_calendar_event"],
    )
    secret_url = "https://example.zoom.us/j/123?pwd=secret"
    monkeypatch.setattr(
        tools,
        "resolve_event_for_join",
        lambda event_id: (
            {
                "event_id": event_id,
                "start": "2026-09-29T16:00:00+00:00",
                "end": "2026-09-29T16:30:00+00:00",
            },
            secret_url,
        ),
    )

    def fake_run(coro, timeout=30.0):
        class Runtime:
            async def join(self, meeting_url):
                assert meeting_url == secret_url
                return {
                    "ok": True,
                    "active": True,
                    "meeting_url": "https://example.zoom.us/j/123",
                }

        import asyncio

        return asyncio.run(coro(Runtime()))

    monkeypatch.setattr(tools, "_run_on_runtime", fake_run)
    out = tools.zoom_chat_join_calendar_event({"event_id": "evt"})
    parsed = json.loads(out)
    assert parsed["calendar_event"]["event_id"] == "evt"
    assert "title" not in parsed["calendar_event"]
    assert "meeting_url" not in parsed
    assert "secret" not in out
    assert "zoom.us" not in out


def test_real_external_plugin_discovery_registers_deferred_tools(tmp_path):
    home = tmp_path / "home"
    plugin_dir = home / "plugins" / "zoom_meeting_chat-platform"
    shutil.copytree(ROOT, plugin_dir, ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__"))
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [zoom_meeting_chat-platform]\nknown_plugin_toolsets:\n  zoom_meeting_chat: [zoom_meeting_chat_admin]\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = f"{CHECKOUT}:{env.get('PYTHONPATH','')}"
    code = """
from hermes_cli.plugins import PluginManager
from tools.registry import registry
mgr = PluginManager()
mgr.discover_and_load()
entry = registry.get_entry('zoom_chat_join')
assert entry is not None, 'zoom_chat_join missing'
assert getattr(entry, 'toolset', None) == 'zoom_meeting_chat_admin'
calendar_entry = registry.get_entry('zoom_chat_join_calendar_event')
assert calendar_entry is not None, 'zoom_chat_join_calendar_event missing'
assert getattr(calendar_entry, 'toolset', None) == 'zoom_meeting_chat_admin'
print('discovered tools:', ','.join(sorted(mgr._plugins['zoom_meeting_chat-platform'].tools_registered)))
"""
    proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(ROOT), text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "zoom_chat_join" in proc.stdout
