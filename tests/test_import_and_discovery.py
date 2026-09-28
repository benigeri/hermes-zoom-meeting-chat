from __future__ import annotations

import os
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
print('discovered tools:', ','.join(sorted(mgr._plugins['zoom_meeting_chat-platform'].tools_registered)))
"""
    proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(ROOT), text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "zoom_chat_join" in proc.stdout
