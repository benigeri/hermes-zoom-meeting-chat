from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from hermes_constants import get_hermes_home, hermes_home_key

from .client import RecallClient
from .compat import zero_tool_schema_preflight
from .config import BOT_NAME, PLATFORM, ZoomChatConfig
from .redact import redact_meeting_url, redact_text


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscriptSegment:
    participant_id: str
    participant_name: str
    text: str
    start_relative: float


@dataclass
class ActiveMeeting:
    meeting_url: str
    meeting_fingerprint: str
    generation: int
    pairing_phrase: str
    pairing_expires_at: float
    phrase_revealed: bool = False
    bot_id: str | None = None
    bot_participant_id: str | None = None
    operator_participant_id: str | None = None
    operator_name: str | None = None
    uncertain: bool = False
    created_at: float = field(default_factory=time.time)
    transcript_segments: list[TranscriptSegment] = field(default_factory=list)
    pending_voice_wake_participant_id: str | None = None
    pending_voice_wake_start_relative: float | None = None

    @property
    def paired(self) -> bool:
        return bool(self.operator_participant_id)


_RUNTIMES: dict[str, "ZoomChatRuntime"] = {}


@dataclass(frozen=True)
class RecallChatEvent:
    bot_id: str
    participant_id: str
    participant_name: str
    text: str
    recipient: str
    message_id: str


@dataclass(frozen=True)
class RecallTranscriptEvent:
    bot_id: str
    participant_id: str
    participant_name: str
    text: str
    start_relative: float
    message_id: str


_PUBLIC_MENTION_RE = re.compile(
    r"^\s*(?:@hio\s*:?[ \t]+|hio\s*:[ \t]+)(?P<request>\S(?:.*\S)?)\s*$",
    re.IGNORECASE,
)
_VOICE_WAKE_RE = re.compile(
    r"^\s*(?:"
    r"(?:hey|hello|hi)\s*,?\s+h[\s.-]*i[\s.-]*o"
    r"|hotel\s+(?:india|hotel)"
    r")\s*[:,]?\s+(?P<request>\S(?:.*\S)?)\s*$",
    re.IGNORECASE,
)
_VOICE_WAKE_ONLY_RE = re.compile(
    r"^\s*(?:"
    r"(?:hey|hello|hi)\s*,?\s+h[\s.-]*i[\s.-]*o"
    r"|hotel\s+(?:india|hotel)"
    r"|hotel"
    r")\s*[:,]?\s*$",
    re.IGNORECASE,
)
_VOICE_WAKE_FOLLOW_UP_SECONDS = 3.0


def extract_public_request(text: str) -> str | None:
    """Return the request from an anchored Zoom mention, or ``None``.

    Recall documents the chat text and audience, not a structured Zoom mention
    entity. Keep parsing deliberately narrow: the invocation must start the
    message, and ``Hio:`` remains a compatibility fallback if Zoom strips the
    native ``@`` marker.
    """
    match = _PUBLIC_MENTION_RE.match(text or "")
    return match.group("request") if match else None


def extract_voice_request(text: str) -> str | None:
    """Return the request from an anchored finalized voice invocation."""
    match = _VOICE_WAKE_RE.match(text or "")
    return match.group("request") if match else None


def is_voice_wake_only(text: str) -> bool:
    """Return whether a finalized segment is only a supported wake phrase.

    Recall often finalizes the wake phrase and command as adjacent segments.
    A standalone ``hotel`` is accepted only in this armed form because Recall
    dropped the second NATO word in live tests; it is never a direct command.
    """
    return _VOICE_WAKE_ONLY_RE.match(text or "") is not None


def parse_recall_chat_event(event: dict[str, Any]) -> RecallChatEvent:
    """Parse Recall's documented real-time chat envelope, failing closed."""
    envelope_value = event.get("data")
    if not isinstance(envelope_value, dict):
        raise ValueError("missing data envelope")
    chat_value = envelope_value.get("data")
    if not isinstance(chat_value, dict):
        raise ValueError("missing chat event data")
    participant_value = chat_value.get("participant")
    message_value = chat_value.get("data")
    bot_value = envelope_value.get("bot")
    if not isinstance(participant_value, dict) or not isinstance(message_value, dict) or not isinstance(bot_value, dict):
        raise ValueError("malformed chat event")
    bot_id = str(bot_value.get("id") or "").strip()
    participant_id = str(participant_value.get("id") or "").strip()
    participant_name = str(participant_value.get("name") or "")
    text = str(message_value.get("text") or "")
    recipient = str(message_value.get("to") or "").strip().lower()
    message_id = str(event.get("webhook_id") or "").strip()
    if not bot_id or not participant_id or not message_id or not recipient or not text or len(text) > 4000:
        raise ValueError("incomplete or oversized chat event")
    return RecallChatEvent(
        bot_id=bot_id,
        participant_id=participant_id,
        participant_name=participant_name,
        text=text,
        recipient=recipient,
        message_id=message_id,
    )


def parse_recall_transcript_event(event: dict[str, Any]) -> RecallTranscriptEvent:
    """Parse Recall's documented finalized transcript envelope, failing closed."""
    envelope_value = event.get("data")
    if not isinstance(envelope_value, dict):
        raise ValueError("missing data envelope")
    transcript_value = envelope_value.get("data")
    bot_value = envelope_value.get("bot")
    if not isinstance(transcript_value, dict) or not isinstance(bot_value, dict):
        raise ValueError("malformed transcript event")
    participant_value = transcript_value.get("participant")
    words_value = transcript_value.get("words")
    if not isinstance(participant_value, dict) or not isinstance(words_value, list) or not words_value:
        raise ValueError("missing transcript participant or words")
    bot_id = str(bot_value.get("id") or "").strip()
    participant_id = str(participant_value.get("id") or "").strip()
    participant_name = str(participant_value.get("name") or "").strip() or "Unknown participant"
    message_id = str(event.get("webhook_id") or "").strip()
    parts: list[str] = []
    start_relative: float | None = None
    for word in words_value:
        if not isinstance(word, dict):
            raise ValueError("malformed transcript word")
        word_text = str(word.get("text") or "").strip()
        if word_text:
            parts.append(word_text)
        if start_relative is None:
            timestamp = word.get("start_timestamp")
            if isinstance(timestamp, dict):
                relative = timestamp.get("relative")
                if isinstance(relative, (int, float, str)):
                    try:
                        start_relative = float(relative)
                    except ValueError:
                        pass
    text = " ".join(parts).strip()
    if not bot_id or not participant_id or not message_id or not text or len(text) > 4000:
        raise ValueError("incomplete or oversized transcript event")
    return RecallTranscriptEvent(
        bot_id=bot_id,
        participant_id=participant_id,
        participant_name=participant_name,
        text=text,
        start_relative=max(0.0, start_relative or 0.0),
        message_id=message_id,
    )


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
    # Preserve the query: Zoom commonly carries the meeting passcode in
    # ``?pwd=...``.  Redaction is a presentation concern, never a mutation of
    # the provider request.
    normalized = urlunsplit(("https", host, p.path.rstrip("/"), p.query, ""))
    fp = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return normalized, fp


def build_create_bot_payload(config: ZoomChatConfig, meeting_url: str) -> dict[str, Any]:
    return {
        "meeting_url": meeting_url,
        "bot_name": BOT_NAME,
        "recording_config": {
            "transcript": {
                "provider": {
                    "recallai_streaming": {
                        "mode": "prioritize_low_latency",
                        "language_code": "en",
                    }
                },
                "diarization": {"use_separate_streams_when_available": True},
            },
            "video_mixed_mp4": None,
            "audio_mixed_raw": None,
            "audio_mixed_mp3": None,
            "video_separate_mp4": None,
            "audio_separate_raw": None,
            "audio_separate_mp3": None,
            "video_mixed_flv": None,
            "video_separate_png": None,
            "video_separate_h264": None,
            "meeting_metadata": None,
            "participant_events": {},
            "retention": None,
            "realtime_endpoints": [
                {
                    "type": "webhook",
                    "url": f"{config.callback_public_base_url}/webhooks/recall/zoom-meeting-chat",
                    "events": ["participant_events.chat_message", "transcript.data"],
                }
            ],
        },
        "automatic_leave": {
            "waiting_room_timeout": config.automatic_leave_timeout,
            "noone_joined_timeout": config.automatic_leave_timeout,
            "everyone_left_timeout": {"timeout": 60},
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
        state_path: Path | None = None,
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
        # chat_id -> (bot_id, exact Recall recipient, generation, route kind)
        self.chat_routes: dict[str, tuple[str, str, int, str]] = {}
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=config.queue_size)
        self.accepting_callbacks = True
        self.shutting_down = False
        self.state_path = state_path or (get_hermes_home() / "state" / "zoom_meeting_chat.json")
        self._load_tombstone()

    def _load_tombstone(self) -> None:
        """Restore an unresolved bot as uncertain so restarts cannot double-join."""
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != 1:
                raise ValueError("unsupported state")
            fingerprint = str(raw.get("meeting_fingerprint") or "").strip()
            if not fingerprint:
                raise ValueError("missing meeting fingerprint")
            generation = max(1, int(raw.get("generation") or 1))
            self.generation = generation
            self.active = ActiveMeeting(
                meeting_url=str(raw.get("meeting_display") or "https://zoom.us/j/redacted"),
                meeting_fingerprint=fingerprint,
                generation=generation,
                pairing_phrase="<unavailable-after-restart>",
                pairing_expires_at=0,
                phrase_revealed=True,
                bot_id=str(raw.get("bot_id") or "").strip() or None,
                uncertain=True,
            )
            self.accepting_callbacks = False
        except Exception:
            # A corrupt/unreadable state file is itself unresolved state. Keep a
            # fail-closed in-memory marker instead of risking a second bot.
            self.generation = max(1, self.generation)
            self.active = ActiveMeeting(
                meeting_url="https://zoom.us/j/redacted",
                meeting_fingerprint="unreadable-tombstone",
                generation=self.generation,
                pairing_phrase="<unavailable-after-restart>",
                pairing_expires_at=0,
                phrase_revealed=True,
                uncertain=True,
            )
            self.accepting_callbacks = False

    def _persist_tombstone(self) -> None:
        m = self.active
        if not m:
            raise RuntimeError("cannot persist empty meeting state")
        payload = {
            "version": 1,
            "meeting_fingerprint": m.meeting_fingerprint,
            "meeting_display": redact_meeting_url(m.meeting_url),
            "generation": m.generation,
            "bot_id": m.bot_id,
            "created_at": m.created_at,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
        try:
            with temp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.state_path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def _clear_tombstone(self) -> None:
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            pass

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
            "transcript_segment_count": len(m.transcript_segments),
        }
        if m.transcript_segments:
            def diagnostic(segment: TranscriptSegment) -> dict[str, Any]:
                return {
                    "participant_name": segment.participant_name,
                    "text": segment.text,
                    "start_relative": segment.start_relative,
                    "voice_wake_match": (
                        segment.participant_id == m.operator_participant_id
                        and (
                            extract_voice_request(segment.text) is not None
                            or is_voice_wake_only(segment.text)
                        )
                    ),
                }

            out["recent_transcripts"] = [
                diagnostic(segment) for segment in m.transcript_segments[-20:]
            ]
            out["latest_transcript"] = out["recent_transcripts"][-1]
        if include_phrase and not m.phrase_revealed:
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
                    out = self._status_dict(include_phrase=False)
                    out["duplicate"] = True
                    return out
                return {"ok": False, "error": "one Zoom meeting is already active or uncertain"}
            self.generation += 1
            self.accepting_callbacks = True
            phrase = secrets.token_urlsafe(18)
            self.active = ActiveMeeting(
                meeting_url=clean_url,
                meeting_fingerprint=fp,
                generation=self.generation,
                pairing_phrase=phrase,
                pairing_expires_at=time.time() + self.config.pairing_ttl_seconds,
            )
            try:
                # Persist before the remote mutation. A crash or timeout can
                # otherwise create a bot that a restarted gateway forgets.
                self._persist_tombstone()
            except Exception as exc:
                self.active = None
                return {
                    "ok": False,
                    "error": f"could not persist Zoom bot safety state; Create Bot was not called: {type(exc).__name__}",
                    "create_bot_called": False,
                }
            payload = build_create_bot_payload(self.config, clean_url)
            try:
                resp = await self.client.create_bot(payload)
                bot_id = str(resp.get("id") or resp.get("bot_id") or "").strip()
                if not bot_id:
                    self.active.uncertain = True
                    return {"ok": False, "error": "Recall Create Bot response did not include a bot id; meeting state is uncertain", "uncertain": True}
                self.active.bot_id = bot_id
                self.active.bot_participant_id = str(resp.get("participant_id") or resp.get("bot_participant_id") or "").strip() or None
                try:
                    self._persist_tombstone()
                except Exception:
                    self.active.uncertain = True
                    return {
                        "ok": False,
                        "error": "Recall bot was created but durable state could not be updated; meeting state is uncertain",
                        "uncertain": True,
                    }
                out = self._status_dict(include_phrase=True)
                self.active.phrase_revealed = True
                out["ok"] = True
                out["pairing_instructions"] = "Send pairing_phrase as a direct Zoom message to Hio within 10 minutes. It is shown only in this tool result."
                return out
            except Exception as exc:
                if self.active:
                    self.active.uncertain = True
                return {"ok": False, "error": f"Recall Create Bot did not complete; meeting state is uncertain: {type(exc).__name__}", "uncertain": True}

    async def leave(self, *, confirmed_absent: bool = False) -> dict[str, Any]:
        async with self.join_lock:
            m = self.active
            if not m:
                return {"ok": True, "active": False, "left": False}
            self.accepting_callbacks = False
            self.chat_routes.clear()
            # Transcript context is intentionally ephemeral. Clear it as soon
            # as leave starts, even if the provider leave later fails.
            m.transcript_segments.clear()
            if confirmed_absent and not m.bot_id:
                try:
                    self._clear_tombstone()
                except Exception as exc:
                    return {"ok": False, "left": False, "uncertain": True, "error": f"confirmed-absent state cleanup failed: {type(exc).__name__}"}
                self.active = None
                return {"ok": True, "left": False, "cleared_confirmed_absent": True}
            if m.bot_id:
                try:
                    await self.client.leave_bot(m.bot_id)
                    try:
                        self._clear_tombstone()
                    except Exception as exc:
                        m.uncertain = True
                        return {
                            "ok": False,
                            "left": True,
                            "uncertain": True,
                            "error": f"bot left but durable state cleanup failed: {type(exc).__name__}",
                        }
                    self.active = None
                    return {"ok": True, "left": True, "bot_id": m.bot_id}
                except Exception as exc:
                    if confirmed_absent:
                        try:
                            provider_bot = await self.client.retrieve_bot(m.bot_id)
                            changes = provider_bot.get("status_changes") or []
                            provider_statuses = [
                                str(change.get("code") or "")
                                for change in changes
                                if isinstance(change, dict)
                            ]
                            provider_status = provider_statuses[-1] if provider_statuses else ""
                            # Recall can advance an already-absent bot beyond
                            # `done` to `media_expired`. Earlier terminal events
                            # remain authoritative even when artifact states
                            # are appended later.
                            absent_statuses = {
                                "call_ended",
                                "done",
                                "fatal",
                                "media_expired",
                            }
                            if absent_statuses.intersection(provider_statuses):
                                self._clear_tombstone()
                                self.active = None
                                return {
                                    "ok": True,
                                    "left": False,
                                    "cleared_confirmed_absent": True,
                                    "provider_status": provider_status,
                                }
                        except Exception:
                            logger.warning("Zoom confirmed-absent reconciliation failed", exc_info=True)
                    m.uncertain = True
                    return {"ok": False, "left": False, "uncertain": True, "error": f"leave failed: {type(exc).__name__}"}
            return {
                "ok": False,
                "left": False,
                "uncertain": True,
                "error": "active bot id is unknown; verify the bot is absent in Recall, then call zoom_chat_leave with confirmed_absent=true",
            }

    async def shutdown(self) -> dict[str, Any]:
        self.shutting_down = True
        self.accepting_callbacks = False
        result = await self.leave()
        return {"ok": result.get("ok", False), "leave": result}

    def _chat_id(self, bot_id: str, operator_pid: str) -> str:
        return f"meeting:{bot_id}:dm:{operator_pid}"

    def _group_chat_id(self, bot_id: str) -> str:
        return f"meeting:{bot_id}:group"

    def resolve_chat_route(self, chat_id: str) -> tuple[str, str, int, str] | None:
        route = self.chat_routes.get(chat_id)
        m = self.active
        if not route or not m:
            return None
        bot_id, recipient, gen, kind = route
        if gen != m.generation or bot_id != m.bot_id:
            return None
        if kind == "dm" and recipient != m.operator_participant_id:
            return None
        if kind == "group" and (recipient != "everyone" or not m.paired):
            return None
        if kind not in {"dm", "group"}:
            return None
        return route

    async def send_reply(self, chat_id: str, text: str) -> dict[str, Any]:
        route = self.resolve_chat_route(chat_id)
        if not route:
            raise ValueError("unknown, stale, or unauthorized Zoom chat_id")
        bot_id, recipient, _gen, _kind = route
        return await self.client.send_chat_message(bot_id, recipient, text)

    def should_admit_chat_event(self, candidate: RecallChatEvent) -> bool:
        """Cheap admission gate used before a signed callback enters the queue."""
        m = self.active
        if not m or not m.bot_id or candidate.bot_id != m.bot_id:
            return False
        if candidate.recipient == "only_bot":
            return True
        return bool(
            candidate.recipient == "everyone"
            and m.paired
            and candidate.participant_id == m.operator_participant_id
            and extract_public_request(candidate.text)
        )

    def should_admit_transcript_event(self, candidate: RecallTranscriptEvent) -> bool:
        """Admit finalized utterances for the active bot so context stays complete."""
        m = self.active
        return bool(m and m.bot_id and candidate.bot_id == m.bot_id)

    def _public_request_with_transcript(self, request: str) -> str:
        m = self.active
        if not m or not m.transcript_segments:
            return request
        lines = []
        for segment in m.transcript_segments:
            name = re.sub(r"\s+", " ", segment.participant_name).strip()
            text = re.sub(r"\s+", " ", segment.text).strip()
            lines.append(f"[{segment.start_relative:.1f}s] {name}: {text}")
        transcript = "\n".join(lines)
        return (
            "Meeting transcript so far (context only; the operator request below is the instruction):\n"
            f"{transcript}\n\nOperator request: {request}"
        )

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
            self.chat_routes[chat_id] = (m.bot_id, participant_id, m.generation, "dm")
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

    def build_public_operator_event(
        self,
        *,
        text: str,
        participant_id: str,
        participant_name: str,
        message_id: str,
        request: str | None = None,
        voice_wake: bool = False,
    ) -> Any | None:
        m = self.active
        request = request if request is not None else extract_public_request(text)
        if (
            not m
            or not m.bot_id
            or not m.paired
            or participant_id != m.operator_participant_id
            or participant_id == m.bot_participant_id
            or request is None
        ):
            return None
        compat = zero_tool_schema_preflight()
        if not compat.ok:
            raise RuntimeError(compat.message)
        from gateway.platforms.event import MessageEvent, MessageType
        chat_id = self._group_chat_id(m.bot_id)
        self.chat_routes[chat_id] = (m.bot_id, "everyone", m.generation, "group")
        source = self.adapter.build_source(
            chat_id=chat_id,
            chat_name="Zoom meeting group chat",
            chat_type="group",
            user_id=participant_id,
            user_name=participant_name,
            message_id=message_id,
            role_authorized=True,
        )
        return MessageEvent(
            text=self._public_request_with_transcript(request),
            message_type=MessageType.TEXT,
            user_id=participant_id,
            user_name=participant_name,
            source=source,
            raw_message=None,
            message_id=message_id,
            internal=False,
            metadata={
                "zoom_meeting_chat": True,
                "bot_id": m.bot_id,
                "generation": m.generation,
                "zoom_audience": "everyone",
                "public_mention": not voice_wake,
                "voice_wake": voice_wake,
            },
            allow_gateway_control=False,
        )

    async def process_callback_event(self, event: dict[str, Any]) -> Any | None:
        event_type = str(event.get("event") or event.get("type") or "")
        m = self.active
        if not self.accepting_callbacks or not m or not m.bot_id:
            return None
        if event_type == "participant_events.chat_message":
            try:
                candidate = parse_recall_chat_event(event)
            except ValueError:
                return None
            if candidate.bot_id != m.bot_id:
                return None
            if candidate.recipient == "only_bot":
                return self.build_operator_event(
                    text=candidate.text,
                    participant_id=candidate.participant_id,
                    participant_name=candidate.participant_name,
                    message_id=candidate.message_id,
                )
            if candidate.recipient == "everyone":
                return self.build_public_operator_event(
                    text=candidate.text,
                    participant_id=candidate.participant_id,
                    participant_name=candidate.participant_name,
                    message_id=candidate.message_id,
                )
            return None
        if event_type == "transcript.data":
            try:
                candidate = parse_recall_transcript_event(event)
            except ValueError:
                return None
            if candidate.bot_id != m.bot_id:
                return None
            m.transcript_segments.append(
                TranscriptSegment(
                    participant_id=candidate.participant_id,
                    participant_name=candidate.participant_name,
                    text=candidate.text,
                    start_relative=candidate.start_relative,
                )
            )
            if candidate.participant_id != m.operator_participant_id:
                return None
            request = extract_voice_request(candidate.text)
            if request is not None:
                m.pending_voice_wake_participant_id = None
                m.pending_voice_wake_start_relative = None
            elif is_voice_wake_only(candidate.text):
                m.pending_voice_wake_participant_id = candidate.participant_id
                m.pending_voice_wake_start_relative = candidate.start_relative
                return None
            else:
                pending_start = m.pending_voice_wake_start_relative
                pending_matches = (
                    m.pending_voice_wake_participant_id == candidate.participant_id
                    and pending_start is not None
                    and 0.0
                    <= candidate.start_relative - pending_start
                    <= _VOICE_WAKE_FOLLOW_UP_SECONDS
                )
                m.pending_voice_wake_participant_id = None
                m.pending_voice_wake_start_relative = None
                if not pending_matches:
                    return None
                request = candidate.text.strip()
                if not request:
                    return None
            try:
                await self.client.send_chat_message(m.bot_id, "everyone", "Heard — working on it.")
            except Exception:
                # The acknowledgement is best-effort; a failed acknowledgement
                # must not suppress the requested Hermes turn and final reply.
                logger.warning("Zoom voice acknowledgement failed", exc_info=True)
            return self.build_public_operator_event(
                text=candidate.text,
                participant_id=candidate.participant_id,
                participant_name=candidate.participant_name,
                message_id=candidate.message_id,
                request=request,
                voice_wake=True,
            )
        return None

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
