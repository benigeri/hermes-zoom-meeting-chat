from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping

from gateway.platforms.helpers import MessageDeduplicator

from .config import decode_webhook_secret
from .runtime import (
    parse_recall_bot_status_event,
    parse_recall_chat_event,
    parse_recall_transcript_event,
)

MAX_BODY_BYTES = 64 * 1024
CHAT_EVENT = "participant_events.chat_message"
TRANSCRIPT_EVENT = "transcript.data"
ACCEPTED_EVENTS = {CHAT_EVENT, TRANSCRIPT_EVENT}
BOT_STATUS_PREFIX = "bot."


@dataclass
class WebhookAdmission:
    status: int
    body: str = ""


def _secret_bytes(secret: str) -> bytes:
    return decode_webhook_secret(secret)


def _candidate_sigs(header: str) -> list[str]:
    vals: list[str] = []
    # Recall documents space-separated signatures in ``v1,<base64>`` form.
    # Accept the common ``v1=<base64>`` spelling as a compatibility courtesy,
    # but never accept an unversioned value.
    for part in str(header or "").split():
        part = part.strip()
        if not part:
            continue
        if part.startswith("v1,"):
            vals.append(part[3:])
        elif part.startswith("v1="):
            vals.append(part[3:])
    return vals


def verify_recall_signature(headers: Mapping[str, str], raw_body: bytes, secret: str, *, now: float | None = None, window_seconds: int = 300) -> str:
    lower = {str(k).lower(): str(v) for k, v in headers.items()}
    wid = lower.get("webhook-id", "")
    ts_s = lower.get("webhook-timestamp", "")
    sig_h = lower.get("webhook-signature", "")
    if not (wid and ts_s and sig_h):
        raise ValueError("missing Recall webhook signature headers")
    try:
        ts = int(ts_s)
    except ValueError as exc:
        raise ValueError("invalid webhook-timestamp") from exc
    if abs((now if now is not None else time.time()) - ts) > window_seconds:
        raise ValueError("stale webhook timestamp")
    signed = f"{wid}.{ts_s}.".encode("utf-8") + raw_body
    digest = hmac.new(_secret_bytes(secret), signed, hashlib.sha256).digest()
    expected_b64 = base64.b64encode(digest).decode("ascii")
    expected_hex = digest.hex()
    if not any(hmac.compare_digest(c, expected_b64) or hmac.compare_digest(c, expected_hex) for c in _candidate_sigs(sig_h)):
        raise ValueError("invalid Recall webhook signature")
    return wid


class RecallWebhookReceiver:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.dedup = MessageDeduplicator(max_size=2000, ttl_seconds=runtime.config.webhook_replay_window_seconds)
        self._runner = None
        self._site = None

    async def admit(self, *, method: str, content_type: str, headers: Mapping[str, str], raw_body: bytes) -> WebhookAdmission:
        if method.upper() != "POST":
            return WebhookAdmission(405, "method not allowed")
        if len(raw_body) > MAX_BODY_BYTES:
            return WebhookAdmission(413, "body too large")
        if "json" not in content_type.lower():
            return WebhookAdmission(415, "content type must be json")
        try:
            wid = verify_recall_signature(headers, raw_body, self.runtime.config.webhook_secret, window_seconds=self.runtime.config.webhook_replay_window_seconds)
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            return WebhookAdmission(400, str(exc))
        event_type = str(payload.get("event") or payload.get("type") or "")
        if event_type.startswith(BOT_STATUS_PREFIX):
            payload["webhook_id"] = wid
            try:
                candidate = parse_recall_bot_status_event(payload)
            except ValueError as exc:
                return WebhookAdmission(400, str(exc))
            async with self.runtime.admission_lock:
                if self.dedup.contains(wid):
                    return WebhookAdmission(204, "")
                result = await self.runtime.handle_bot_status_event(candidate)
                if not result.get("ok"):
                    return WebhookAdmission(503, "lifecycle cleanup failed")
                self.dedup.is_duplicate(wid)
                return WebhookAdmission(204, "")
        if event_type not in ACCEPTED_EVENTS:
            return WebhookAdmission(204, "")
        payload["webhook_id"] = wid
        try:
            if event_type == CHAT_EVENT:
                candidate = parse_recall_chat_event(payload)
            else:
                candidate = parse_recall_transcript_event(payload)
        except ValueError as exc:
            return WebhookAdmission(400, str(exc))
        async with self.runtime.admission_lock:
            active = self.runtime.active
            if (
                not self.runtime.accepting_callbacks
                or self.runtime.shutting_down
                or active is None
                or not active.bot_id
                or candidate.bot_id != active.bot_id
            ):
                return WebhookAdmission(204, "")
            if event_type == CHAT_EVENT:
                admitted = self.runtime.should_admit_chat_event(candidate)
            else:
                admitted = self.runtime.should_admit_transcript_event(candidate)
            if not admitted:
                return WebhookAdmission(204, "")
            if self.dedup.contains(wid):
                return WebhookAdmission(204, "")
            try:
                self.runtime.queue.put_nowait(payload)
            except asyncio.QueueFull:  # type: ignore[name-defined]
                return WebhookAdmission(503, "queue full")
            if self.dedup.is_duplicate(wid):
                self.dedup.discard(wid)
                return WebhookAdmission(204, "")
            return WebhookAdmission(202, "accepted")

    async def start(self) -> None:
        import aiohttp.web
        app = aiohttp.web.Application(client_max_size=MAX_BODY_BYTES)

        async def handler(request):
            raw = await request.read()
            admitted = await self.admit(method=request.method, content_type=request.content_type or "", headers=request.headers, raw_body=raw)
            return aiohttp.web.Response(status=admitted.status, text=admitted.body)

        app.router.add_post("/webhooks/recall/zoom-meeting-chat", handler)
        self._runner = aiohttp.web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = aiohttp.web.TCPSite(self._runner, self.runtime.config.callback_bind_host, self.runtime.config.callback_bind_port)
        await self._site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

# asyncio imported after class body for static import lightness in tests using admit only.
import asyncio  # noqa:E402
