from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from conftest import load_plugin_pkg


@dataclass
class Compat:
    ok: bool = True
    message: str = "ok"
    enabled_toolsets: list[str] | None = None
    tool_names: list[str] | None = None


class FakeTransport:
    def __init__(self):
        self.requests = []
        self.create_response = {"id": "bot-1", "participant_id": "hio-1"}
        self.raise_on_create = None

    async def request(self, method, url, *, headers, json_body=None, timeout=20.0):
        self.requests.append((method, url, json_body))
        if url.endswith("/api/v1/bot/"):
            if self.raise_on_create:
                raise self.raise_on_create
            return dict(self.create_response)
        if url.endswith("/chat_message/"):
            return {"id": "msg-out", "sent": True}
        if url.endswith("/leave/"):
            return {"left": True}
        return {}


class FakeAdapter:
    def __init__(self):
        self.sources = []
        self.events = []
        self.platform = "zoom_meeting_chat"
        self.gateway_runner = None

    def build_source(self, **kwargs):
        self.sources.append(kwargs)
        from gateway.session import SessionSource

        return SessionSource(
            platform="zoom_meeting_chat",
            chat_id=kwargs["chat_id"],
            chat_name=kwargs.get("chat_name"),
            chat_type=kwargs.get("chat_type", "dm"),
            user_id=kwargs.get("user_id"),
            user_name=kwargs.get("user_name"),
            message_id=kwargs.get("message_id"),
            role_authorized=kwargs.get("role_authorized", False),
        )

    async def handle_message(self, event):
        self.events.append(event)


@pytest.fixture()
def runtime(monkeypatch):
    load_plugin_pkg("zoom_runtime_pkg")
    rt_mod = __import__("zoom_runtime_pkg.runtime", fromlist=["ZoomChatRuntime"])
    client_mod = __import__("zoom_runtime_pkg.client", fromlist=["RecallClient"])
    cfg_mod = __import__("zoom_runtime_pkg.config", fromlist=["ZoomChatConfig"])
    monkeypatch.setattr(rt_mod, "zero_tool_schema_preflight", lambda: Compat(True, "ok", [], []))
    cfg = cfg_mod.ZoomChatConfig(
        api_key="key",
        webhook_secret="whsec_dGVzdA",
        recall_base_url="https://us-west-2.recall.ai",
        callback_public_base_url="https://callback.example",
        callback_bind_host="127.0.0.1",
        callback_bind_port=1,
        queue_size=2,
        pairing_ttl_seconds=600,
        in_call_not_recording_timeout=1800,
        automatic_leave_timeout=7200,
    )
    transport = FakeTransport()
    adapter = FakeAdapter()
    client = client_mod.RecallClient("key", "https://us-west-2.recall.ai", transport=transport)
    loop = asyncio.new_event_loop()
    rt = rt_mod.ZoomChatRuntime(config=cfg, client=client, adapter=adapter, loop=loop, dispatch=adapter.handle_message)
    return rt, transport, adapter


@pytest.mark.asyncio
async def test_join_payload_is_chat_only_and_redacted(runtime):
    rt, transport, _adapter = runtime
    result = await rt.join("https://example.zoom.us/j/123?pwd=secret")
    assert result["ok"] is True
    assert result["pairing_phrase"]
    payload = transport.requests[0][2]
    assert payload["bot_name"] == "Hio"
    assert payload["transcript"] is None
    assert payload["meeting_metadata"] is None
    assert payload["participant_events"] == {}
    assert payload["retention"] is None
    assert payload["video_mixed_mp4"] is None
    assert payload["audio_mixed_mp3"] is None
    assert payload["real_time_endpoints"][0]["events"] == ["participant_events.chat_message"]
    assert payload["automatic_leave"]["in_call_not_recording_timeout"] == 1800
    assert "pwd=" not in result["meeting_url"]


@pytest.mark.asyncio
async def test_pairing_consumes_phrase_and_operator_dm_dispatch(runtime):
    rt, _transport, adapter = runtime
    joined = await rt.join("https://example.zoom.us/j/123?pwd=secret")
    phrase = joined["pairing_phrase"]
    assert await rt.process_callback_event({"type": "participant_events.chat_message", "bot_id": "bot-1", "participant_id": "intruder", "message": {"text": "hello", "chat_type": "dm"}}) is None
    assert await rt.process_callback_event({"type": "participant_events.chat_message", "bot_id": "bot-1", "participant_id": "paul", "participant_name": "Paul", "message": {"text": phrase, "chat_type": "dm"}}) is None
    assert rt.active.operator_participant_id == "paul"
    event = await rt.process_callback_event({"type": "participant_events.chat_message", "bot_id": "bot-1", "participant_id": "paul", "participant_name": "Paul", "message": {"id": "m2", "text": "reply privately", "chat_type": "dm"}})
    assert event is not None
    assert event.source.chat_type == "dm"
    assert event.source.role_authorized is True
    assert event.internal is False
    assert event.allow_gateway_control is False
    assert adapter.sources[-1]["chat_id"] == "meeting:bot-1:dm:paul"
    assert await rt.process_callback_event({"type": "participant_events.chat_message", "bot_id": "bot-1", "participant_id": "other", "message": {"text": "ignored", "chat_type": "dm"}}) is None
    assert await rt.process_callback_event({"type": "participant_events.chat_message", "bot_id": "bot-1", "participant_id": "paul", "message": {"text": "public", "chat_type": "public"}}) is None


@pytest.mark.asyncio
async def test_exact_dm_send_route_and_stale_rejection(runtime):
    rt, transport, _adapter = runtime
    joined = await rt.join("https://example.zoom.us/j/123")
    await rt.process_callback_event({"type": "participant_events.chat_message", "bot_id": "bot-1", "participant_id": "paul", "message": {"text": joined["pairing_phrase"], "chat_type": "dm"}})
    resp = await rt.send_reply("meeting:bot-1:dm:paul", "hello")
    assert resp["id"] == "msg-out"
    assert transport.requests[-1][1].endswith("/api/v1/bot/bot-1/chat_message/")
    assert transport.requests[-1][2] == {"to": "paul", "message": "hello"}
    with pytest.raises(ValueError):
        await rt.send_reply("meeting:bot-1:dm:other", "nope")
    await rt.leave()
    with pytest.raises(ValueError):
        await rt.send_reply("meeting:bot-1:dm:paul", "late")


@pytest.mark.asyncio
async def test_uncertain_create_blocks_retry(runtime):
    rt, transport, _adapter = runtime
    transport.raise_on_create = TimeoutError("network")
    result = await rt.join("https://example.zoom.us/j/123")
    assert result["ok"] is False and result["uncertain"] is True
    transport.raise_on_create = None
    again = await rt.join("https://example.zoom.us/j/123")
    assert again["duplicate"] is True
    assert len([r for r in transport.requests if r[1].endswith("/api/v1/bot/")]) == 1
