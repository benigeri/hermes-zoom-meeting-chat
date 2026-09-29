from __future__ import annotations

import asyncio
import logging
from typing import Any

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .client import RecallClient
from .config import PLATFORM, ZoomChatConfig, requirements_available, validate_platform_config
from .runtime import ZoomChatRuntime, register_runtime, unregister_runtime
from .webhook import RecallWebhookReceiver

logger = logging.getLogger(__name__)


class ZoomMeetingChatAdapter(BasePlatformAdapter):
    platform_key = PLATFORM

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(PLATFORM))
        self.zconfig = ZoomChatConfig.from_platform_config(config)
        self.runtime: ZoomChatRuntime | None = None
        self.webhook: RecallWebhookReceiver | None = None
        self._consumer_task: asyncio.Task | None = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        try:
            self.zconfig.validate_ready()
        except Exception as exc:
            logger.warning("Zoom Meeting Chat not connected: %s", exc)
            return False
        loop = asyncio.get_running_loop()
        client = RecallClient(self.zconfig.api_key, self.zconfig.recall_base_url)
        self.runtime = ZoomChatRuntime(config=self.zconfig, client=client, adapter=self, loop=loop, dispatch=self.handle_message)
        self.webhook = RecallWebhookReceiver(self.runtime)
        await self.webhook.start()
        register_runtime(self.runtime)
        self._consumer_task = loop.create_task(self._consume_callbacks(), name="zoom-meeting-chat-consumer")
        self._mark_connected()
        return True

    async def _consume_callbacks(self) -> None:
        assert self.runtime is not None
        while not self.runtime.shutting_down:
            try:
                await self.runtime.consumer_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Zoom Meeting Chat callback consumer failed", exc_info=True)

    async def disconnect(self) -> None:
        runtime = self.runtime
        if runtime is not None:
            await runtime.shutdown()
            unregister_runtime(runtime.home_key)
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            self._consumer_task = None
        if self.webhook is not None:
            await self.webhook.stop()
            self.webhook = None
        self.runtime = None
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if self.runtime is None:
            return SendResult(success=False, error="Zoom Meeting Chat runtime not connected", error_kind="not_found")
        try:
            resp = await self.runtime.send_reply(str(chat_id), str(content))
            mid = str(resp.get("id") or resp.get("message_id") or "") or None
            return SendResult(success=True, message_id=mid, raw_response=resp)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), error_kind="forbidden")

    async def get_chat_info(self, chat_id):
        if str(chat_id).endswith(":group"):
            return {"name": "Zoom meeting group chat", "type": "group"}
        return {"name": "Zoom Meeting Chat DM", "type": "dm"}

    def toolsets_for_source(self, source):
        return ["no_mcp"]

    def context_policy_for_source(self, source):
        """Keep public meeting turns outside private profile context."""
        if getattr(source, "chat_type", None) == "group":
            return {"skip_memory": True, "skip_context_files": True}
        return None


def _env_enablement() -> dict | None:
    if not requirements_available():
        return None
    return {}


def register(ctx) -> None:
    ctx.register_platform(
        name=PLATFORM,
        label="Zoom Meeting Chat",
        adapter_factory=ZoomMeetingChatAdapter,
        check_fn=requirements_available,
        validate_config=validate_platform_config,
        required_env=["RECALL_API_KEY", "RECALL_WEBHOOK_SECRET"],
        env_enablement_fn=_env_enablement,
        install_hint="Set RECALL_API_KEY, RECALL_WEBHOOK_SECRET, and platform extra callback_public_base_url. The plugin stays disabled/unconfigured without credentials.",
        max_message_length=4000,
        platform_hint=(
            "Zoom Meeting Chat has two isolated routes. Direct messages are private. "
            "A group route is created only when the paired operator starts a public message with "
            "@Hio (or Hio: if Zoom strips the mention marker), or starts a finalized spoken utterance with "
            "Hotel India, Hotel Hotel, or a compatible Hey Hio form; replies on that route are visible to everyone. "
            "The group turn includes the live meeting transcript so far. "
            "For group replies, use only the operator request and meeting transcript: do not reveal or rely on "
            "personal memory, private Zoom DM history, credentials, local paths, or private-source facts. "
            "Never fall back between private and public audiences."
        ),
        emoji="🎥",
    )
