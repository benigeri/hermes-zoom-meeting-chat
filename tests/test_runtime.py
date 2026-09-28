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
        self.raise_on_leave = None

    async def request(self, method, url, *, headers, json_body=None, timeout=20.0):
        self.requests.append((method, url, json_body))
        if url.endswith("/api/v1/bot/"):
            if self.raise_on_create:
                raise self.raise_on_create
            return dict(self.create_response)
        if url.endswith("/send_chat_message/"):
            return {"id": "msg-out", "sent": True}
        if url.endswith("/leave_call/"):
            if self.raise_on_leave:
                raise self.raise_on_leave
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
def runtime(monkeypatch, tmp_path):
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
    rt = rt_mod.ZoomChatRuntime(
        config=cfg,
        client=client,
        adapter=adapter,
        loop=loop,
        dispatch=adapter.handle_message,
        state_path=tmp_path / "zoom-state.json",
    )
    return rt, transport, adapter


@pytest.mark.asyncio
async def test_join_payload_is_chat_only_and_redacted(runtime):
    rt, transport, _adapter = runtime
    result = await rt.join("https://example.zoom.us/j/123?pwd=secret")
    assert result["ok"] is True
    assert result["pairing_phrase"]
    payload = transport.requests[0][2]
    assert payload["bot_name"] == "Hio"
    assert payload["meeting_url"].endswith("?pwd=secret")
    recording = payload["recording_config"]
    assert recording["transcript"] is None
    assert recording["meeting_metadata"] is None
    assert recording["participant_events"] == {}
    assert recording["retention"] is None
    assert recording["video_mixed_mp4"] is None
    assert recording["audio_mixed_mp3"] is None
    assert recording["realtime_endpoints"][0]["events"] == ["participant_events.chat_message"]
    assert payload["automatic_leave"]["everyone_left_timeout"] == {"timeout": 60}
    assert payload["automatic_leave"]["in_call_not_recording_timeout"] == 1800
    assert "pwd=" not in result["meeting_url"]


@pytest.mark.asyncio
async def test_pairing_consumes_phrase_and_operator_dm_dispatch(runtime):
    rt, _transport, adapter = runtime
    joined = await rt.join("https://example.zoom.us/j/123?pwd=secret")
    phrase = joined["pairing_phrase"]
    def incoming(sender, text, *, to="only_bot", wid="evt"):
        return {"webhook_id": wid, "event": "participant_events.chat_message", "data": {"data": {"participant": {"id": sender, "name": "Paul"}, "timestamp": {"absolute": "2026-09-28T00:00:00Z", "relative": 1.0}, "data": {"text": text, "to": to}}, "bot": {"id": "bot-1"}}}
    assert await rt.process_callback_event(incoming("intruder", "hello", wid="evt-0")) is None
    assert await rt.process_callback_event(incoming("paul", phrase, wid="evt-1")) is None
    assert rt.active.operator_participant_id == "paul"
    event = await rt.process_callback_event(incoming("paul", "reply privately", wid="m2"))
    assert event is not None
    assert event.source.chat_type == "dm"
    assert event.source.role_authorized is True
    assert event.internal is False
    assert event.allow_gateway_control is False
    assert adapter.sources[-1]["chat_id"] == "meeting:bot-1:dm:paul"
    assert await rt.process_callback_event(incoming("other", "ignored", wid="m3")) is None
    assert await rt.process_callback_event(incoming("paul", "public", to="everyone", wid="m4")) is None


@pytest.mark.asyncio
async def test_exact_dm_send_route_and_stale_rejection(runtime):
    rt, transport, _adapter = runtime
    joined = await rt.join("https://example.zoom.us/j/123")
    incoming = {"webhook_id": "evt-1", "event": "participant_events.chat_message", "data": {"data": {"participant": {"id": "paul", "name": "Paul"}, "timestamp": {"absolute": "2026-09-28T00:00:00Z", "relative": 1.0}, "data": {"text": joined["pairing_phrase"], "to": "only_bot"}}, "bot": {"id": "bot-1"}}}
    await rt.process_callback_event(incoming)
    resp = await rt.send_reply("meeting:bot-1:dm:paul", "hello")
    assert resp["id"] == "msg-out"
    assert transport.requests[-1][1].endswith("/api/v1/bot/bot-1/send_chat_message/")
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


@pytest.mark.asyncio
async def test_failed_leave_keeps_uncertain_state_and_blocks_second_join(runtime):
    rt, transport, _adapter = runtime
    joined = await rt.join("https://example.zoom.us/j/123")
    assert joined["ok"] is True
    transport.raise_on_leave = TimeoutError("network")
    left = await rt.leave()
    assert left["ok"] is False and left["uncertain"] is True
    assert rt.active is not None and rt.active.uncertain is True
    again = await rt.join("https://example.zoom.us/j/456")
    assert again["ok"] is False
    assert len([r for r in transport.requests if r[1].endswith("/api/v1/bot/")]) == 1


@pytest.mark.asyncio
async def test_pairing_phrase_is_revealed_only_once(runtime):
    rt, _transport, _adapter = runtime
    first = await rt.join("https://example.zoom.us/j/123")
    assert first.get("pairing_phrase")
    duplicate = await rt.join("https://example.zoom.us/j/123")
    assert duplicate["duplicate"] is True
    assert "pairing_phrase" not in duplicate


@pytest.mark.asyncio
async def test_failed_shutdown_survives_new_runtime_and_blocks_second_bot(runtime):
    rt, transport, adapter = runtime
    rt_mod = __import__(rt.__class__.__module__, fromlist=["ZoomChatRuntime"])
    joined = await rt.join("https://example.zoom.us/j/123")
    assert joined["ok"] is True
    transport.raise_on_leave = TimeoutError("network")
    shutdown = await rt.shutdown()
    assert shutdown["ok"] is False

    second_transport = FakeTransport()
    client_mod = __import__("zoom_runtime_pkg.client", fromlist=["RecallClient"])
    second_client = client_mod.RecallClient("key", "https://us-west-2.recall.ai", transport=second_transport)
    fresh = rt_mod.ZoomChatRuntime(
        config=rt.config,
        client=second_client,
        adapter=adapter,
        loop=rt.loop,
        dispatch=adapter.handle_message,
        state_path=rt.state_path,
    )
    assert fresh.active is not None and fresh.active.uncertain is True
    blocked = await fresh.join("https://example.zoom.us/j/456")
    assert blocked["ok"] is False
    assert not second_transport.requests

    recovered = await fresh.leave()
    assert recovered["ok"] is True and recovered["left"] is True
    assert not rt.state_path.exists()


@pytest.mark.asyncio
async def test_unknown_persisted_bot_requires_explicit_confirmed_absent(runtime):
    rt, transport, _adapter = runtime
    transport.raise_on_create = TimeoutError("network")
    uncertain = await rt.join("https://example.zoom.us/j/123")
    assert uncertain["uncertain"] is True and rt.state_path.exists()
    ordinary = await rt.leave()
    assert ordinary["ok"] is False and rt.state_path.exists()
    cleared = await rt.leave(confirmed_absent=True)
    assert cleared["ok"] is True and cleared["cleared_confirmed_absent"] is True
    assert rt.active is None and not rt.state_path.exists()


@pytest.mark.asyncio
async def test_confirmed_absent_does_not_skip_leave_for_known_bot(runtime):
    rt, transport, _adapter = runtime
    joined = await rt.join("https://example.zoom.us/j/123")
    assert joined["ok"] is True
    result = await rt.leave(confirmed_absent=True)
    assert result["ok"] is True and result["left"] is True
    assert any(request[1].endswith("/leave_call/") for request in transport.requests)
