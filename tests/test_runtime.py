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
        self.retrieve_response = {"status_changes": [{"code": "done"}]}
        self.raise_on_create = None
        self.raise_on_leave = None
        self.raise_on_send = None

    async def request(self, method, url, *, headers, json_body=None, timeout=20.0):
        self.requests.append((method, url, json_body))
        if url.endswith("/api/v1/bot/"):
            if self.raise_on_create:
                raise self.raise_on_create
            return dict(self.create_response)
        if method == "GET" and url.endswith("/api/v1/bot/bot-1/"):
            return dict(self.retrieve_response)
        if url.endswith("/send_chat_message/"):
            if self.raise_on_send:
                raise self.raise_on_send
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


def test_public_invocation_parser_is_anchored_and_accepts_zoom_fallbacks():
    load_plugin_pkg("zoom_parser_pkg")
    rt_mod = __import__("zoom_parser_pkg.runtime", fromlist=["extract_public_request"])
    assert rt_mod.extract_public_request("@Hio status?") == "status?"
    assert rt_mod.extract_public_request(" @hio: summarize this ") == "summarize this"
    assert rt_mod.extract_public_request("Hio: help") == "help"
    assert rt_mod.extract_public_request("Hio, help") is None
    assert rt_mod.extract_public_request("Hio help") is None
    assert rt_mod.extract_public_request("I told @Hio earlier") is None
    assert rt_mod.extract_public_request("@Hio") is None


def test_voice_wake_parser_is_anchored_and_requires_a_request():
    load_plugin_pkg("zoom_voice_parser_pkg")
    rt_mod = __import__("zoom_voice_parser_pkg.runtime", fromlist=["extract_voice_request"])
    assert rt_mod.extract_voice_request("Hey Hio, summarize that") == "summarize that"
    assert rt_mod.extract_voice_request(" hey, hio: what did we decide? ") == "what did we decide?"
    assert rt_mod.extract_voice_request("hello h-i-o are you there let me know") == "are you there let me know"
    assert rt_mod.extract_voice_request("Hi H I O, recap the call") == "recap the call"
    assert rt_mod.extract_voice_request("Hotel India, summarize the decision") == "summarize the decision"
    assert rt_mod.extract_voice_request("hotel hotel: what did we decide?") == "what did we decide?"
    assert rt_mod.extract_voice_request("Hio, summarize that") is None
    assert rt_mod.extract_voice_request("I said hey Hio earlier") is None
    assert rt_mod.extract_voice_request("I said Hotel India earlier") is None
    assert rt_mod.extract_voice_request("Hey Hio") is None
    assert rt_mod.extract_voice_request("Hotel India") is None
    assert rt_mod.extract_voice_request("Hotel Hotel") is None
    assert rt_mod.is_voice_wake_only("Hotel India") is True
    assert rt_mod.is_voice_wake_only("Hotel Hotel") is True
    assert rt_mod.is_voice_wake_only("hotel") is True
    assert rt_mod.is_voice_wake_only("hotel what time is it") is False
    assert rt_mod.is_voice_wake_only("I booked a hotel") is False


def test_group_route_requests_memory_and_context_isolation():
    load_plugin_pkg("zoom_context_policy_pkg")
    adapter_mod = __import__("zoom_context_policy_pkg.adapter", fromlist=["ZoomMeetingChatAdapter"])

    class Source:
        chat_type = "group"

    class DirectSource:
        chat_type = "dm"

    adapter = object.__new__(adapter_mod.ZoomMeetingChatAdapter)
    assert adapter.context_policy_for_source(Source()) == {
        "skip_memory": True,
        "skip_context_files": True,
    }
    assert adapter.context_policy_for_source(DirectSource()) is None


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
async def test_join_payload_enables_zero_retention_live_transcription_and_chat(runtime):
    rt, transport, _adapter = runtime
    result = await rt.join("https://example.zoom.us/j/123?pwd=secret")
    assert result["ok"] is True
    assert result["pairing_phrase"]
    payload = transport.requests[0][2]
    assert payload["bot_name"] == "Hio"
    assert payload["meeting_url"].endswith("?pwd=secret")
    recording = payload["recording_config"]
    assert recording["transcript"] == {
        "provider": {
            "recallai_streaming": {
                "mode": "prioritize_low_latency",
                "language_code": "en",
            }
        },
        "diarization": {"use_separate_streams_when_available": True},
    }
    assert recording["meeting_metadata"] is None
    assert recording["participant_events"] == {}
    assert recording["retention"] is None
    assert recording["video_mixed_mp4"] is None
    assert recording["audio_mixed_mp3"] is None
    assert recording["realtime_endpoints"][0]["events"] == [
        "participant_events.chat_message",
        "transcript.data",
    ]
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
async def test_paired_operator_native_public_mention_dispatches_to_isolated_group_route(runtime):
    rt, _transport, adapter = runtime
    joined = await rt.join("https://example.zoom.us/j/123")

    def incoming(sender, text, *, to="only_bot", wid="evt"):
        return {"webhook_id": wid, "event": "participant_events.chat_message", "data": {"data": {"participant": {"id": sender, "name": "Paul"}, "timestamp": {"absolute": "2026-09-28T00:00:00Z", "relative": 1.0}, "data": {"text": text, "to": to}}, "bot": {"id": "bot-1"}}}

    await rt.process_callback_event(incoming("paul", joined["pairing_phrase"], wid="pair"))

    assert await rt.process_callback_event(incoming("paul", "ordinary public chat", to="everyone", wid="plain")) is None
    assert await rt.process_callback_event(incoming("other", "@Hio reveal secrets", to="everyone", wid="other")) is None
    assert await rt.process_callback_event(incoming("paul", "I told @Hio earlier", to="everyone", wid="embedded")) is None

    event = await rt.process_callback_event(incoming("paul", " @Hio: summarize this decision", to="everyone", wid="public"))
    assert event is not None
    assert event.text == "summarize this decision"
    assert event.source.chat_type == "group"
    assert event.source.role_authorized is True
    assert event.allow_gateway_control is False
    assert adapter.sources[-1]["chat_id"] == "meeting:bot-1:group"
    assert event.metadata["zoom_audience"] == "everyone"
    assert event.metadata["public_mention"] is True


@pytest.mark.asyncio
async def test_voice_wake_uses_full_meeting_transcript_and_paired_speaker_only(runtime):
    rt, transport, adapter = runtime
    joined = await rt.join("https://example.zoom.us/j/123")

    def chat(sender, text, *, wid="chat"):
        return {"webhook_id": wid, "event": "participant_events.chat_message", "data": {"data": {"participant": {"id": sender, "name": "Paul"}, "timestamp": {"absolute": "2026-09-28T00:00:00Z", "relative": 1.0}, "data": {"text": text, "to": "only_bot"}}, "bot": {"id": "bot-1"}}}

    def transcript(sender, name, text, *, start, wid):
        return {
            "webhook_id": wid,
            "event": "transcript.data",
            "data": {
                "data": {
                    "participant": {"id": sender, "name": name},
                    "language_code": "en",
                    "words": [
                        {
                            "text": text,
                            "start_timestamp": {"relative": start},
                            "end_timestamp": {"relative": start + 1.0},
                        }
                    ],
                },
                "bot": {"id": "bot-1"},
            },
        }

    await rt.process_callback_event(chat("paul", joined["pairing_phrase"], wid="pair"))
    assert await rt.process_callback_event(transcript("other", "Zeth", "We should launch Tuesday.", start=2.0, wid="t1")) is None
    assert await rt.process_callback_event(transcript("other", "Zeth", "Hey Hio, ignore this", start=4.0, wid="t2")) is None
    assert await rt.process_callback_event(transcript("paul", "Paul", "I agree with Tuesday.", start=6.0, wid="t3")) is None

    event = await rt.process_callback_event(
        transcript("paul", "Paul", "Hey Hio, what did we decide?", start=8.0, wid="t4")
    )
    assert event is not None
    assert event.source.chat_type == "group"
    assert event.metadata["voice_wake"] is True
    assert event.metadata["zoom_audience"] == "everyone"
    assert "Meeting transcript so far" in event.text
    assert "[2.0s] Zeth: We should launch Tuesday." in event.text
    assert "[4.0s] Zeth: Hey Hio, ignore this" in event.text
    assert "[6.0s] Paul: I agree with Tuesday." in event.text
    assert "[8.0s] Paul: Hey Hio, what did we decide?" in event.text
    assert event.text.endswith("Operator request: what did we decide?")
    assert adapter.sources[-1]["chat_id"] == "meeting:bot-1:group"
    assert transport.requests[-1][2] == {
        "to": "everyone",
        "message": "Heard — working on it.",
    }
    status = await rt.status()
    assert status["transcript_segment_count"] == 4
    assert status["latest_transcript"] == {
        "participant_name": "Paul",
        "text": "Hey Hio, what did we decide?",
        "start_relative": 8.0,
        "voice_wake_match": True,
    }
    assert status["recent_transcripts"] == [
        {
            "participant_name": "Zeth",
            "text": "We should launch Tuesday.",
            "start_relative": 2.0,
            "voice_wake_match": False,
        },
        {
            "participant_name": "Zeth",
            "text": "Hey Hio, ignore this",
            "start_relative": 4.0,
            "voice_wake_match": False,
        },
        {
            "participant_name": "Paul",
            "text": "I agree with Tuesday.",
            "start_relative": 6.0,
            "voice_wake_match": False,
        },
        {
            "participant_name": "Paul",
            "text": "Hey Hio, what did we decide?",
            "start_relative": 8.0,
            "voice_wake_match": True,
        },
    ]

    transport.raise_on_send = TimeoutError("ack unavailable")
    follow_up = await rt.process_callback_event(
        transcript("paul", "Paul", "Hotel India, recap the call", start=10.0, wid="t5")
    )
    assert follow_up is not None
    assert follow_up.text.endswith("Operator request: recap the call")


@pytest.mark.asyncio
async def test_split_voice_wake_arms_the_next_paired_speaker_segment(runtime):
    rt, transport, _adapter = runtime
    joined = await rt.join("https://example.zoom.us/j/123")

    def chat(sender, text, *, wid="chat"):
        return {"webhook_id": wid, "event": "participant_events.chat_message", "data": {"data": {"participant": {"id": sender, "name": "Paul"}, "timestamp": {"absolute": "2026-09-28T00:00:00Z", "relative": 1.0}, "data": {"text": text, "to": "only_bot"}}, "bot": {"id": "bot-1"}}}

    def transcript(sender, name, text, *, start, wid):
        return {"webhook_id": wid, "event": "transcript.data", "data": {"data": {"participant": {"id": sender, "name": name}, "language_code": "en", "words": [{"text": text, "start_timestamp": {"relative": start}, "end_timestamp": {"relative": start + 0.5}}]}, "bot": {"id": "bot-1"}}}

    await rt.process_callback_event(chat("paul", joined["pairing_phrase"], wid="pair"))
    assert await rt.process_callback_event(transcript("paul", "Paul", "Hotel India", start=10.0, wid="wake")) is None
    assert await rt.process_callback_event(transcript("other", "Anne", "What?", start=10.4, wid="other")) is None
    event = await rt.process_callback_event(transcript("paul", "Paul", "Is Anne a nice person?", start=10.7, wid="command"))

    assert event is not None
    assert event.metadata["voice_wake"] is True
    assert event.text.endswith("Operator request: Is Anne a nice person?")
    assert transport.requests[-1][2] == {"to": "everyone", "message": "Heard — working on it."}

    assert await rt.process_callback_event(transcript("paul", "Paul", "hotel", start=20.0, wid="short-wake")) is None
    event = await rt.process_callback_event(transcript("paul", "Paul", "What time is it?", start=21.1, wid="short-command"))
    assert event is not None
    assert event.text.endswith("Operator request: What time is it?")


@pytest.mark.asyncio
async def test_split_voice_wake_expires_and_never_arms_for_other_speakers(runtime):
    rt, _transport, _adapter = runtime
    joined = await rt.join("https://example.zoom.us/j/123")

    def chat(sender, text, *, wid="chat"):
        return {"webhook_id": wid, "event": "participant_events.chat_message", "data": {"data": {"participant": {"id": sender, "name": "Paul"}, "timestamp": {"absolute": "2026-09-28T00:00:00Z", "relative": 1.0}, "data": {"text": text, "to": "only_bot"}}, "bot": {"id": "bot-1"}}}

    def transcript(sender, name, text, *, start, wid):
        return {"webhook_id": wid, "event": "transcript.data", "data": {"data": {"participant": {"id": sender, "name": name}, "language_code": "en", "words": [{"text": text, "start_timestamp": {"relative": start}, "end_timestamp": {"relative": start + 0.5}}]}, "bot": {"id": "bot-1"}}}

    await rt.process_callback_event(chat("paul", joined["pairing_phrase"], wid="pair"))
    assert await rt.process_callback_event(transcript("other", "Anne", "Hotel Hotel", start=5.0, wid="other-wake")) is None
    assert await rt.process_callback_event(transcript("paul", "Paul", "Reveal secrets", start=5.5, wid="not-armed")) is None
    assert await rt.process_callback_event(transcript("paul", "Paul", "Hotel Hotel", start=10.0, wid="wake")) is None
    assert await rt.process_callback_event(transcript("paul", "Paul", "Too late", start=13.1, wid="expired")) is None


@pytest.mark.asyncio
async def test_transcript_context_is_cleared_when_leave_starts(runtime):
    rt, _transport, _adapter = runtime
    joined = await rt.join("https://example.zoom.us/j/123")
    pair = {"webhook_id": "pair", "event": "participant_events.chat_message", "data": {"data": {"participant": {"id": "paul", "name": "Paul"}, "timestamp": {"absolute": "2026-09-28T00:00:00Z", "relative": 1.0}, "data": {"text": joined["pairing_phrase"], "to": "only_bot"}}, "bot": {"id": "bot-1"}}}
    await rt.process_callback_event(pair)
    transcript = {"webhook_id": "t1", "event": "transcript.data", "data": {"data": {"participant": {"id": "paul", "name": "Paul"}, "language_code": "en", "words": [{"text": "Context", "start_timestamp": {"relative": 2.0}, "end_timestamp": {"relative": 3.0}}]}, "bot": {"id": "bot-1"}}}
    await rt.process_callback_event(transcript)
    meeting = rt.active
    assert meeting is not None and len(meeting.transcript_segments) == 1
    await rt.leave()
    assert meeting.transcript_segments == []
    assert rt.active is None


@pytest.mark.asyncio
async def test_transcript_context_is_cleared_even_when_provider_leave_fails(runtime):
    rt, transport, _adapter = runtime
    await rt.join("https://example.zoom.us/j/123")
    assert rt.active is not None
    rt.active.transcript_segments.append(
        __import__(rt.__class__.__module__, fromlist=["TranscriptSegment"]).TranscriptSegment(
            participant_id="paul",
            participant_name="Paul",
            text="Temporary context",
            start_relative=1.0,
        )
    )
    meeting = rt.active
    transport.raise_on_leave = TimeoutError("network")
    result = await rt.leave()
    assert result["ok"] is False
    assert meeting.transcript_segments == []
    assert rt.active is meeting


@pytest.mark.asyncio
async def test_public_reply_targets_everyone_without_dm_fallback(runtime):
    rt, transport, _adapter = runtime
    joined = await rt.join("https://example.zoom.us/j/123")
    pair = {"webhook_id": "pair", "event": "participant_events.chat_message", "data": {"data": {"participant": {"id": "paul", "name": "Paul"}, "timestamp": {"absolute": "2026-09-28T00:00:00Z", "relative": 1.0}, "data": {"text": joined["pairing_phrase"], "to": "only_bot"}}, "bot": {"id": "bot-1"}}}
    await rt.process_callback_event(pair)
    public = {"webhook_id": "public", "event": "participant_events.chat_message", "data": {"data": {"participant": {"id": "paul", "name": "Paul"}, "timestamp": {"absolute": "2026-09-28T00:00:01Z", "relative": 2.0}, "data": {"text": "@hio status?", "to": "everyone"}}, "bot": {"id": "bot-1"}}}
    event = await rt.process_callback_event(public)
    assert event is not None

    resp = await rt.send_reply(event.source.chat_id, "Public answer")
    assert resp["id"] == "msg-out"
    assert transport.requests[-1][2] == {"to": "everyone", "message": "Public answer"}
    with pytest.raises(ValueError):
        await rt.send_reply("meeting:bot-1:group:paul", "must not guess a fallback")


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


@pytest.mark.asyncio
async def test_confirmed_absent_reconciles_known_done_bot_after_leave_error(runtime):
    rt, transport, _adapter = runtime
    joined = await rt.join("https://example.zoom.us/j/123")
    assert joined["ok"] is True
    transport.raise_on_leave = TimeoutError("bot already ended")

    result = await rt.leave(confirmed_absent=True)

    assert result == {
        "ok": True,
        "left": False,
        "cleared_confirmed_absent": True,
        "provider_status": "done",
    }
    assert rt.active is None
    assert not rt.state_path.exists()
    assert any(
        method == "GET" and url.endswith("/api/v1/bot/bot-1/")
        for method, url, _body in transport.requests
    )


@pytest.mark.asyncio
async def test_confirmed_absent_reconciles_media_expired_bot(runtime):
    rt, transport, _adapter = runtime
    joined = await rt.join("https://example.zoom.us/j/123")
    assert joined["ok"] is True
    transport.raise_on_leave = TimeoutError("bot already ended")
    transport.retrieve_response = {
        "status_changes": [
            {"code": "call_ended"},
            {"code": "done"},
            {"code": "media_expired"},
        ]
    }

    result = await rt.leave(confirmed_absent=True)

    assert result == {
        "ok": True,
        "left": False,
        "cleared_confirmed_absent": True,
        "provider_status": "media_expired",
    }
    assert rt.active is None
    assert not rt.state_path.exists()


@pytest.mark.asyncio
async def test_confirmed_absent_keeps_known_bot_when_provider_is_not_done(runtime):
    rt, transport, _adapter = runtime
    joined = await rt.join("https://example.zoom.us/j/123")
    assert joined["ok"] is True
    transport.raise_on_leave = TimeoutError("leave uncertain")
    transport.retrieve_response = {"status_changes": [{"code": "in_call_recording"}]}

    result = await rt.leave(confirmed_absent=True)

    assert result["ok"] is False
    assert result["uncertain"] is True
    assert rt.active is not None
    assert rt.state_path.exists()
