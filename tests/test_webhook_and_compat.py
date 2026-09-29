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
    raw = secret.removeprefix("whsec_")
    key = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
    digest = hmac.new(key, f"{wid}.{ts}.".encode() + body, hashlib.sha256).digest()
    return {"webhook-id": wid, "webhook-timestamp": str(ts), "webhook-signature": "v1," + base64.b64encode(digest).decode()}


@pytest.mark.asyncio
async def test_hmac_admission_dedup_and_queue_full():
    load_plugin_pkg("zoom_webhook_pkg")
    wh = __import__("zoom_webhook_pkg.webhook", fromlist=["RecallWebhookReceiver"])
    runtime = SimpleNamespace(
        config=SimpleNamespace(webhook_secret="whsec_dGVzdA", webhook_replay_window_seconds=300),
        queue=asyncio.Queue(maxsize=1),
        admission_lock=asyncio.Lock(),
        active=SimpleNamespace(bot_id="bot-1"),
        accepting_callbacks=True,
        shutting_down=False,
        should_admit_chat_event=lambda candidate: candidate.recipient == "only_bot",
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


@pytest.mark.asyncio
async def test_signed_irrelevant_or_malformed_events_never_consume_queue_or_dedup():
    load_plugin_pkg("zoom_webhook_reject_pkg")
    wh = __import__("zoom_webhook_reject_pkg.webhook", fromlist=["RecallWebhookReceiver"])
    runtime = SimpleNamespace(
        config=SimpleNamespace(webhook_secret="whsec_dGVzdA", webhook_replay_window_seconds=300),
        queue=asyncio.Queue(maxsize=2),
        admission_lock=asyncio.Lock(),
        active=SimpleNamespace(bot_id="bot-1"),
        accepting_callbacks=True,
        shutting_down=False,
        should_admit_chat_event=lambda candidate: candidate.recipient == "only_bot",
    )
    receiver = wh.RecallWebhookReceiver(runtime)
    now = int(time.time())

    def body_for(*, bot="bot-1", participant=7, text="hi", to="only_bot"):
        return json.dumps({"event": "participant_events.chat_message", "data": {"data": {"participant": {"id": participant, "name": "Paul"}, "timestamp": {"absolute": "2026-09-28T00:00:00Z", "relative": 1.0}, "data": {"text": text, "to": to}}, "bot": {"id": bot}}}).encode()

    for wid, body, expected in (
        ("public", body_for(to="everyone"), 204),
        ("wrong-bot", body_for(bot="bot-2"), 204),
        ("missing-participant", body_for(participant=""), 400),
        ("overlong", body_for(text="x" * 4001), 400),
    ):
        result = await receiver.admit(method="POST", content_type="application/json", headers=signed(runtime.config.webhook_secret, wid, now, body), raw_body=body)
        assert result.status == expected
        assert runtime.queue.qsize() == 0
        assert not receiver.dedup.contains(wid)

    valid = body_for()
    accepted = await receiver.admit(method="POST", content_type="application/json", headers=signed(runtime.config.webhook_secret, "valid", now, valid), raw_body=valid)
    assert accepted.status == 202
    assert runtime.queue.qsize() == 1


@pytest.mark.asyncio
async def test_signed_public_mention_is_admitted_only_when_runtime_authorizes_it():
    load_plugin_pkg("zoom_webhook_public_pkg")
    wh = __import__("zoom_webhook_public_pkg.webhook", fromlist=["RecallWebhookReceiver"])
    runtime = SimpleNamespace(
        config=SimpleNamespace(webhook_secret="whsec_dGVzdA", webhook_replay_window_seconds=300),
        queue=asyncio.Queue(maxsize=2),
        admission_lock=asyncio.Lock(),
        active=SimpleNamespace(bot_id="bot-1"),
        accepting_callbacks=True,
        shutting_down=False,
        should_admit_chat_event=lambda candidate: (
            candidate.recipient == "everyone"
            and candidate.participant_id == "paul"
            and candidate.text.lower().lstrip().startswith("@hio")
        ),
    )
    receiver = wh.RecallWebhookReceiver(runtime)
    now = int(time.time())

    def body_for(participant, text):
        return json.dumps({"event": "participant_events.chat_message", "data": {"data": {"participant": {"id": participant, "name": "Paul"}, "timestamp": {"absolute": "2026-09-28T00:00:00Z", "relative": 1.0}, "data": {"text": text, "to": "everyone"}}, "bot": {"id": "bot-1"}}}).encode()

    for wid, participant, text, expected in (
        ("plain", "paul", "ordinary public chat", 204),
        ("other", "other", "@Hio private info", 204),
        ("mention", "paul", "@Hio status?", 202),
    ):
        body = body_for(participant, text)
        result = await receiver.admit(
            method="POST",
            content_type="application/json",
            headers=signed(runtime.config.webhook_secret, wid, now, body),
            raw_body=body,
        )
        assert result.status == expected

    assert runtime.queue.qsize() == 1


@pytest.mark.asyncio
async def test_signed_final_transcript_is_admitted_and_malformed_transcript_is_rejected():
    load_plugin_pkg("zoom_webhook_transcript_pkg")
    wh = __import__("zoom_webhook_transcript_pkg.webhook", fromlist=["RecallWebhookReceiver"])
    runtime = SimpleNamespace(
        config=SimpleNamespace(webhook_secret="whsec_dGVzdA", webhook_replay_window_seconds=300),
        queue=asyncio.Queue(maxsize=2),
        admission_lock=asyncio.Lock(),
        active=SimpleNamespace(bot_id="bot-1"),
        accepting_callbacks=True,
        shutting_down=False,
        should_admit_chat_event=lambda _candidate: False,
        should_admit_transcript_event=lambda candidate: candidate.bot_id == "bot-1",
    )
    receiver = wh.RecallWebhookReceiver(runtime)
    now = int(time.time())

    def body_for(*, bot="bot-1", participant="paul", words=None):
        if words is None:
            words = [{"text": "Hey Hio, summarize", "start_timestamp": {"relative": 1.0}, "end_timestamp": {"relative": 2.0}}]
        return json.dumps({"event": "transcript.data", "data": {"data": {"participant": {"id": participant, "name": "Paul"}, "language_code": "en", "words": words}, "bot": {"id": bot}}}).encode()

    valid = body_for()
    accepted = await receiver.admit(
        method="POST",
        content_type="application/json",
        headers=signed(runtime.config.webhook_secret, "transcript-1", now, valid),
        raw_body=valid,
    )
    assert accepted.status == 202

    malformed = body_for(words=[])
    rejected = await receiver.admit(
        method="POST",
        content_type="application/json",
        headers=signed(runtime.config.webhook_secret, "transcript-2", now, malformed),
        raw_body=malformed,
    )
    assert rejected.status == 400
    assert runtime.queue.qsize() == 1


@pytest.mark.asyncio
async def test_signed_terminal_status_bypasses_full_queue_and_cleans_runtime():
    load_plugin_pkg("zoom_webhook_status_pkg")
    wh = __import__("zoom_webhook_status_pkg.webhook", fromlist=["RecallWebhookReceiver"])
    handled = []

    async def handle_status(candidate):
        handled.append(candidate)
        return {"ok": True, "handled": True, "terminal": True}

    queue = asyncio.Queue(maxsize=1)
    queue.put_nowait({"already": "full"})
    runtime = SimpleNamespace(
        config=SimpleNamespace(webhook_secret="whsec_dGVzdA", webhook_replay_window_seconds=300),
        queue=queue,
        admission_lock=asyncio.Lock(),
        active=SimpleNamespace(bot_id="bot-1"),
        accepting_callbacks=False,
        shutting_down=False,
        handle_bot_status_event=handle_status,
    )
    receiver = wh.RecallWebhookReceiver(runtime)
    now = int(time.time())
    body = json.dumps({
        "event": "bot.call_ended",
        "data": {
            "data": {"code": "call_ended", "sub_code": "call_ended_by_host"},
            "bot": {"id": "bot-1", "metadata": {}},
        },
    }).encode()

    first = await receiver.admit(
        method="POST",
        content_type="application/json",
        headers=signed(runtime.config.webhook_secret, "status-1", now, body),
        raw_body=body,
    )
    duplicate = await receiver.admit(
        method="POST",
        content_type="application/json",
        headers=signed(runtime.config.webhook_secret, "status-1", now, body),
        raw_body=body,
    )

    assert first.status == 204
    assert duplicate.status == 204
    assert len(handled) == 1
    assert handled[0].bot_id == "bot-1"
    assert handled[0].code == "call_ended"
    assert runtime.queue.qsize() == 1


@pytest.mark.asyncio
async def test_status_webhook_rejects_malformed_payload_and_retries_cleanup_failure():
    load_plugin_pkg("zoom_webhook_status_failure_pkg")
    wh = __import__("zoom_webhook_status_failure_pkg.webhook", fromlist=["RecallWebhookReceiver"])

    async def fail_status(_candidate):
        return {"ok": False, "uncertain": True}

    runtime = SimpleNamespace(
        config=SimpleNamespace(webhook_secret="whsec_dGVzdA", webhook_replay_window_seconds=300),
        queue=asyncio.Queue(maxsize=1),
        admission_lock=asyncio.Lock(),
        active=SimpleNamespace(bot_id="bot-1"),
        accepting_callbacks=True,
        shutting_down=False,
        handle_bot_status_event=fail_status,
    )
    receiver = wh.RecallWebhookReceiver(runtime)
    now = int(time.time())

    malformed = json.dumps({
        "event": "bot.done",
        "data": {"data": {"code": "done"}, "bot": {"id": ""}},
    }).encode()
    malformed_result = await receiver.admit(
        method="POST",
        content_type="application/json",
        headers=signed(runtime.config.webhook_secret, "status-bad", now, malformed),
        raw_body=malformed,
    )
    assert malformed_result.status == 400

    valid = json.dumps({
        "event": "bot.done",
        "data": {"data": {"code": "done"}, "bot": {"id": "bot-1"}},
    }).encode()
    failed = await receiver.admit(
        method="POST",
        content_type="application/json",
        headers=signed(runtime.config.webhook_secret, "status-retry", now, valid),
        raw_body=valid,
    )
    assert failed.status == 503
    assert not receiver.dedup.contains("status-retry")


def test_compatibility_preflight_success_and_failures(monkeypatch):
    load_plugin_pkg("zoom_compat_pkg")
    compat = __import__("zoom_compat_pkg.compat", fromlist=["zero_tool_schema_preflight"])
    monkeypatch.setattr("hermes_cli.tools_config._get_plugin_toolset_keys", lambda: {"zoom_meeting_chat_admin"})
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda cfg, platform: set() if cfg["platform_toolsets"][platform] == ["no_mcp"] else {"bad"})
    monkeypatch.setattr(
        "model_tools.get_tool_definitions",
        lambda enabled_toolsets=None, quiet_mode=True, skip_tool_search_assembly=False: [],
    )
    ok = compat.zero_tool_schema_preflight({"known_plugin_toolsets": {"zoom_meeting_chat": ["zoom_meeting_chat_admin"]}})
    assert ok.ok is True
    missing = compat.zero_tool_schema_preflight({"known_plugin_toolsets": {"zoom_meeting_chat": []}})
    assert missing.ok is False and "known_plugin_toolsets" in missing.message
    nondefault_context = compat.zero_tool_schema_preflight({"context": {"engine": "lcm"}, "known_plugin_toolsets": {"zoom_meeting_chat": ["zoom_meeting_chat_admin"]}})
    assert nondefault_context.ok is False and "context.engine" in nondefault_context.message
    def bridge_only(enabled_toolsets=None, quiet_mode=True, skip_tool_search_assembly=False):
        names = ["x_search"] if skip_tool_search_assembly else ["tool_search", "tool_describe", "tool_call"]
        return [{"function": {"name": name}} for name in names]

    monkeypatch.setattr("model_tools.get_tool_definitions", bridge_only)
    bridge = compat.zero_tool_schema_preflight(
        {"known_plugin_toolsets": {"zoom_meeting_chat": ["zoom_meeting_chat_admin"]}}
    )
    assert bridge.ok is True

    def leaked_tool(enabled_toolsets=None, quiet_mode=True, skip_tool_search_assembly=False):
        names = ["terminal"] if skip_tool_search_assembly else ["terminal"]
        return [{"function": {"name": name}} for name in names]

    monkeypatch.setattr("model_tools.get_tool_definitions", leaked_tool)
    leak = compat.zero_tool_schema_preflight({"known_plugin_toolsets": {"zoom_meeting_chat": ["zoom_meeting_chat_admin"]}})
    assert leak.ok is False and "unsupported model tool schemas" in leak.message
