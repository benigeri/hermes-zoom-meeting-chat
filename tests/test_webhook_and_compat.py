from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest

from conftest import load_plugin_pkg


def signed(secret: str, wid: str, ts: int, body: bytes):
    wh = __import__("zoom_webhook_pkg.webhook", fromlist=["_secret_bytes"])
    digest = hmac.new(wh._secret_bytes(secret), f"{wid}.{ts}.".encode() + body, hashlib.sha256).digest()
    return {"webhook-id": wid, "webhook-timestamp": str(ts), "webhook-signature": "v1," + base64.b64encode(digest).decode()}


@pytest.mark.asyncio
async def test_hmac_admission_dedup_and_queue_full():
    load_plugin_pkg("zoom_webhook_pkg")
    wh = __import__("zoom_webhook_pkg.webhook", fromlist=["RecallWebhookReceiver"])
    runtime = SimpleNamespace(
        config=SimpleNamespace(webhook_secret="whsec_dGVzdA", webhook_replay_window_seconds=300),
        queue=asyncio.Queue(maxsize=1),
        admission_lock=asyncio.Lock(),
    )
    receiver = wh.RecallWebhookReceiver(runtime)
    now = int(time.time())
    body = json.dumps({"event": "participant_events.chat_message", "data": {"data": {"participant": {"id": 7, "name": "Paul"}, "timestamp": {"absolute": "2026-09-28T00:00:00Z", "relative": 1.0}, "data": {"text": "hi", "to": "only_bot"}}, "bot": {"id": "bot-1"}}}).encode()
    headers = signed(runtime.config.webhook_secret, "evt-1", now, body)
    first = await receiver.admit(method="POST", content_type="application/json", headers=headers, raw_body=body)
    dup = await receiver.admit(method="POST", content_type="application/json", headers=headers, raw_body=body)
    assert first.status == 202
    assert dup.status == 204
    body2 = body.replace(b"hi", b"yo")
    full = await receiver.admit(method="POST", content_type="application/json", headers=signed(runtime.config.webhook_secret, "evt-2", now, body2), raw_body=body2)
    assert full.status == 503
    assert not receiver.dedup.contains("evt-2")
    bad = await receiver.admit(method="GET", content_type="application/json", headers=headers, raw_body=body)
    assert bad.status == 405
    stale = await receiver.admit(method="POST", content_type="application/json", headers=signed(runtime.config.webhook_secret, "evt-old", now - 9999, body), raw_body=body)
    assert stale.status == 400


def test_compatibility_preflight_success_and_failures(monkeypatch):
    load_plugin_pkg("zoom_compat_pkg")
    compat = __import__("zoom_compat_pkg.compat", fromlist=["zero_tool_schema_preflight"])
    monkeypatch.setattr("hermes_cli.tools_config._get_plugin_toolset_keys", lambda: {"zoom_meeting_chat_admin"})
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda cfg, platform: set() if cfg["platform_toolsets"][platform] == ["no_mcp"] else {"bad"})
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda enabled_toolsets=None, quiet_mode=True: [])
    ok = compat.zero_tool_schema_preflight({"known_plugin_toolsets": {"zoom_meeting_chat": ["zoom_meeting_chat_admin"]}})
    assert ok.ok is True
    missing = compat.zero_tool_schema_preflight({"known_plugin_toolsets": {"zoom_meeting_chat": []}})
    assert missing.ok is False and "known_plugin_toolsets" in missing.message
    nondefault_context = compat.zero_tool_schema_preflight({"context": {"engine": "lcm"}, "known_plugin_toolsets": {"zoom_meeting_chat": ["zoom_meeting_chat_admin"]}})
    assert nondefault_context.ok is False and "context.engine" in nondefault_context.message
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda enabled_toolsets=None, quiet_mode=True: [{"function": {"name": "x_search"}}])
    leak = compat.zero_tool_schema_preflight({"known_plugin_toolsets": {"zoom_meeting_chat": ["zoom_meeting_chat_admin"]}})
    assert leak.ok is False and "zero final model tool schemas" in leak.message
