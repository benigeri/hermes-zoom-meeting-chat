from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

PLATFORM = "zoom_meeting_chat"
PLUGIN_ID = "zoom_meeting_chat-platform"
ADMIN_TOOLSET = "zoom_meeting_chat_admin"
BOT_NAME = "Hio"
ALLOWED_RECALL_BASE_URLS = {
    "https://us-west-2.recall.ai",
    "https://us-east-1.recall.ai",
    "https://eu-central-1.recall.ai",
    "https://ap-northeast-1.recall.ai",
}


def _secret(name: str, default: str = "") -> str:
    from agent.secret_scope import get_secret

    return (get_secret(name, default) or default).strip()


def _extra(extra: Mapping[str, Any] | None, key: str, default: Any = None) -> Any:
    if not isinstance(extra, Mapping):
        return default
    return extra.get(key, default)


def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, iv))


@dataclass(frozen=True)
class ZoomChatConfig:
    api_key: str
    webhook_secret: str
    recall_base_url: str
    callback_public_base_url: str
    callback_bind_host: str
    callback_bind_port: int
    queue_size: int
    pairing_ttl_seconds: int
    in_call_not_recording_timeout: int
    automatic_leave_timeout: int
    webhook_replay_window_seconds: int = 300

    @classmethod
    def from_platform_config(cls, pconfig: Any) -> "ZoomChatConfig":
        extra = getattr(pconfig, "extra", None) or {}
        base = str(_extra(extra, "recall_base_url", "https://us-west-2.recall.ai")).rstrip("/")
        if base not in ALLOWED_RECALL_BASE_URLS:
            raise ValueError(
                "recall_base_url must be one of: " + ", ".join(sorted(ALLOWED_RECALL_BASE_URLS))
            )
        public = str(_extra(extra, "callback_public_base_url", "")).strip().rstrip("/")
        if public and urlsplit(public).scheme != "https":
            raise ValueError("callback_public_base_url must be HTTPS")
        host = str(_extra(extra, "callback_bind_host", "127.0.0.1")).strip() or "127.0.0.1"
        if host.lower() not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("callback_bind_host must be loopback (127.0.0.1, localhost, or ::1)")
        return cls(
            api_key=_secret("RECALL_API_KEY"),
            webhook_secret=_secret("RECALL_WEBHOOK_SECRET"),
            recall_base_url=base,
            callback_public_base_url=public,
            callback_bind_host=host,
            callback_bind_port=_int(_extra(extra, "callback_bind_port", 8765), 8765, 1, 65535),
            queue_size=_int(_extra(extra, "queue_size", 32), 32, 1, 1000),
            pairing_ttl_seconds=_int(_extra(extra, "pairing_ttl_seconds", 600), 600, 30, 3600),
            in_call_not_recording_timeout=_int(_extra(extra, "in_call_not_recording_timeout", 1800), 1800, 60, 14400),
            automatic_leave_timeout=_int(_extra(extra, "automatic_leave_timeout", 7200), 7200, 60, 28800),
        )

    def validate_ready(self) -> None:
        if not self.api_key:
            raise ValueError("RECALL_API_KEY is required")
        if not self.webhook_secret.startswith("whsec_"):
            raise ValueError("RECALL_WEBHOOK_SECRET must be set and begin with whsec_")
        if not self.callback_public_base_url:
            raise ValueError("callback_public_base_url is required")
        if urlsplit(self.callback_public_base_url).scheme != "https":
            raise ValueError("callback_public_base_url must be HTTPS")


def requirements_available() -> bool:
    try:
        return bool(_secret("RECALL_API_KEY") and _secret("RECALL_WEBHOOK_SECRET").startswith("whsec_"))
    except Exception:
        return False


def validate_platform_config(pconfig: Any) -> bool:
    try:
        ZoomChatConfig.from_platform_config(pconfig).validate_ready()
        return True
    except Exception:
        return False
