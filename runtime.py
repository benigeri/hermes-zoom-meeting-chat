from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from hermes_constants import hermes_home_key

from .client import RecallClient
from .compat import zero_tool_schema_preflight
from .config import BOT_NAME, PLATFORM, ZoomChatConfig
from .redact import redact_meeting_url, redact_text


@dataclass
class ActiveMeeting:
    meeting_url: str
    meeting_fingerprint: str
    generation: int
    pairing_phrase: str
    pairing_expires_at: float
    bot_id: str | None = None
    bot_participant_id: str | None = None
    operator_participant_id: str | None = None
    operator_name: str | None = None
    uncertain: bool = False
    created_at: float = field(default_factory=time.time)

    @property
    def paired(self) -> bool:
        return bool(self.operator_participant_id)


_RUNTIMES: dict[str, "ZoomChatRuntime"] = {}


def _current_home_key() -> str:
    return hermes_home_key()


def register_runtime(runtime: "ZoomChatRuntime") -> None:
    _RUNTIMES[runtime.home_key] = runtime


def unregister_runtime(home_key: str | None = None) -> None:
    _RUNTIMES.pop(home_key or _current_home_key(), None)


def get_runtime(home_key: str | None = None) -> "ZoomChatRuntime | None":
    return _RUNTIMES.get(home_key or _current_home_key())


def normalize_meeting_url(raw: str) -> tuple[str, str]:
    raw = (raw or "").strip()
    p = urlsplit(raw)
    if p.scheme != "https" or not p.netloc:
        raise ValueError("meeting_url must be an https Zoom meeting URL")
    host = p.netloc.lower()
    if not (host == "zoom.us" or host.endswith(".zoom.us")):
        raise ValueError("meeting_url must be hosted under zoom.us")
    clean = urlunsplit(("https", host, p.path.rstrip("/"), "", ""))
    fp = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    return clean, fp


def build_create_bot_payload(config: ZoomChatConfig, meeting_url: str) -> dict[str, Any]:
    return {
        "meeting_url": meeting_url,
        "bot_name": BOT_NAME,
        "recording_mode": "speaker_view",
        "transcript": None,
        "audio_mixed_raw": None,
        "audio_mixed_mp3": None,
        "video_mixed_mp4": None,
        "video_separate_mp4": None,
        "audio_separate_raw": None,
        "meeting_metadata": None,
        "participant_events": {},
        "retention": None,
        "real_time_endpoints": [
            {
                "type": "webhook",
                "url": f"{config.callback_public_base_url}/webhooks/recall/zoom-meeting-chat",
                "events": ["participant_events.chat_message"],
            }
        ],
        "automatic_leave": {
            "waiting_room_timeout": config.automatic_leave_timeout,
            "noone_joined_timeout": config.automatic_leave_timeout,
            "everyone_left_timeout": 60,
            "in_call_not_recording_timeout": config.in_call_not_recording_timeout,
        },
    }


class ZoomChatRuntime:
    def __init__(
        self,
        *,
        config: ZoomChatConfig,
        client: RecallClient,
        adapter: Any,
        loop: asyncio.AbstractEventLoop | None = None,
        dispatch: Callable[[Any], Any] | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.adapter = adapter
        self.loop = loop or asyncio.get_event_loop()
        self.home_key = _current_home_key()
        self.dispatch = dispatch
        self.join_lock = asyncio.Lock()
        self.admission_lock = asyncio.Lock()
        self.active: ActiveMeeting | None = None
        self.generation = 0
        self.chat_routes: dict[str, tuple[str, str, int]] = {}
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=config.queue_size)
        self.accepting_callbacks = True
        self.shutting_down = False

    def _status_dict(self, *, include_phrase: bool = False) -> dict[str, Any]:
        m = self.active
        if not m:
            return {"ok": True, "active": False, "platform": PLATFORM}
        out = {
            "ok": True,
            "active": True,
            "platform": PLATFORM,
            "meeting_url": redact_meeting_url(m.meeting_url),
            "bot_id": m.bot_id,
            "paired": m.paired,
            "operator_participant_id": m.operator_participant_id,
            "operator_name": m.operator_name,
            "uncertain": m.uncertain,
            "generation": m.generation,
            "pairing_expires_at": m.pairing_expires_at,
        }
        if include_phrase:
            out["pairing_phrase"] = m.pairing_phrase
        return out

    async def status(self) -> dict[str, Any]:
        return self._status_dict()

    async def join(self, meeting_url: str) -> dict[str, Any]:
        compat = zero_tool_schema_preflight()
        if not compat.ok:
            return {"ok": False, "error": compat.message, "create_bot_called": False}
        clean_url, fp = normalize_meeting_url(meeting_url)
        async with self.join_lock:
            if self.shutting_down:
                return {"ok": False, "error": "gateway adapter is shutting down"}
            if self.active:
                if self.active.meeting_fingerprint == fp:
                    out = self._status_dict(include_phrase=not self.active.paired)
                    out["duplicate"] = True
                    return out
                return {"ok": False, "error": "one Zoom meeting is already active or uncertain"}
            self.generation += 1
            phrase = secrets.token_urlsafe(18)
            self.active = ActiveMeeting(
                meeting_url=clean_url,
                meeting_fingerprint=fp,
                generation=self.generation,
                pairing_phrase=phrase,
                pairing_expires_at=time.time() + self.config.pairing_ttl_seconds,
            )
            payload = build_create_bot_payload(self.config, clean_url)
            try:
                resp = await self.client.create_bot(payload)
                bot_id = str(resp.get("id") or resp.get("bot_id") or "").strip()
                if not bot_id:
                    self.active.uncertain = True
                    return {"ok": False, "error": "Recall Create Bot response did not include a bot id; meeting state is uncertain", "uncertain": True}
                self.active.bot_id = bot_id
                self.active.bot_participant_id = str(resp.get("participant_id") or resp.get("bot_participant_id") or "").strip() or None
                out = self._status_dict(include_phrase=True)
                out["ok"] = True
                out["pairing_instructions"] = "Send pairing_phrase as a direct Zoom message to Hio within 10 minutes. It is shown only in this tool result."
                return out
            except Exception as exc:
                if self.active:
                    self.active.uncertain = True
                return {"ok": False, "error": f"Recall Create Bot did not complete; meeting state is uncertain: {type(exc).__name__}", "uncertain": True}

    async def leave(self) -> dict[str, Any]:
        async with self.join_lock:
            m = self.active
            if not m:
                return {"ok": True, "active": False, "left": False}
            self.accepting_callbacks = False
            self.chat_routes.clear()
            self.active = None
            if m.bot_id and not m.uncertain:
                try:
                    await self.client.leave_bot(m.bot_id)
                    return {"ok": True, "left": True, "bot_id": m.bot_id}
                except Exception as exc:
                    return {"ok": False, "left": False, "uncertain": True, "error": f"leave failed: {type(exc).__name__}"}
            return {"ok": False, "left": False, "uncertain": True, "error": "active bot id is unknown or uncertain; check Recall dashboard"}

    async def shutdown(self) -> dict[str, Any]:
        self.shutting_down = True
        self.accepting_callbacks = False
        result = await self.leave()
        return {"ok": result.get("ok", False), "leave": result}

    def _chat_id(self, bot_id: str, operator_pid: str) -> str:
        return f"meeting:{bot_id}:dm:{operator_pid}"

    def resolve_chat_route(self, chat_id: str) -> tuple[str, str, int] | None:
        route = self.chat_routes.get(chat_id)
        m = self.active
        if not route or not m:
            return None
        bot_id, pid, gen = route
        if gen != m.generation or bot_id != m.bot_id or pid != m.operator_participant_id:
            return None
        return route

    async def send_reply(self, chat_id: str, text: str) -> dict[str, Any]:
        route = self.resolve_chat_route(chat_id)
        if not route:
            raise ValueError("unknown, stale, or unauthorized Zoom DM chat_id")
        bot_id, pid, _gen = route
        return await self.client.send_chat_message(bot_id, pid, text)

    def build_operator_event(self, *, text: str, participant_id: str, participant_name: str, message_id: str) -> Any | None:
        m = self.active
        if not m or not m.bot_id or participant_id == m.bot_participant_id:
            return None
        if not m.paired:
            if time.time() > m.pairing_expires_at:
                return None
            if (text or "").strip() != m.pairing_phrase:
                return None
            m.operator_participant_id = participant_id
            m.operator_name = participant_name
            m.pairing_phrase = "<consumed>"
            chat_id = self._chat_id(m.bot_id, participant_id)
            self.chat_routes[chat_id] = (m.bot_id, participant_id, m.generation)
            return None
        if participant_id != m.operator_participant_id:
            return None
        compat = zero_tool_schema_preflight()
        if not compat.ok:
            raise RuntimeError(compat.message)
        from gateway.platforms.event import MessageEvent, MessageType
        source = self.adapter.build_source(
            chat_id=self._chat_id(m.bot_id, participant_id),
            chat_name="Zoom DM with Hio",
            chat_type="dm",
            user_id=participant_id,
            user_name=participant_name,
            message_id=message_id,
            role_authorized=True,
        )
        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            user_id=participant_id,
            user_name=participant_name,
            source=source,
            raw_message=None,
            message_id=message_id,
            internal=False,
            metadata={"zoom_meeting_chat": True, "bot_id": m.bot_id, "generation": m.generation},
            allow_gateway_control=False,
        )

    async def process_callback_event(self, event: dict[str, Any]) -> Any | None:
        participant = event.get("participant") if isinstance(event.get("participant"), dict) else {}
        message = event.get("message") if isinstance(event.get("message"), dict) else event
        text = str(message.get("text") or message.get("message") or "")
        participant_id = str(participant.get("id") or event.get("participant_id") or "")
        participant_name = str(participant.get("name") or event.get("participant_name") or "")
        message_id = str(event.get("webhook_id") or event.get("id") or message.get("id") or secrets.token_hex(8))
        chat_type = str(event.get("chat_type") or message.get("chat_type") or "dm").lower()
        to_bot = bool(event.get("to_bot", True))
        bot_id = str(event.get("bot_id") or event.get("bot", {}).get("id") if isinstance(event.get("bot"), dict) else event.get("bot_id") or "")
        m = self.active
        if not self.accepting_callbacks or not m or not m.bot_id:
            return None
        if bot_id and bot_id != m.bot_id:
            return None
        if chat_type != "dm" or not to_bot or len(text) > 4000 or not participant_id:
            return None
        return self.build_operator_event(text=text, participant_id=participant_id, participant_name=participant_name, message_id=message_id)

    async def consumer_once(self) -> Any | None:
        item = await self.queue.get()
        try:
            event = await self.process_callback_event(item)
            if event is not None and self.dispatch is not None:
                result = self.dispatch(event)
                if asyncio.iscoroutine(result):
                    await result
            return event
        finally:
            self.queue.task_done()
