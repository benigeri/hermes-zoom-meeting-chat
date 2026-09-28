from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class RecallTransport(Protocol):
    async def request(self, method: str, url: str, *, headers: Mapping[str, str], json_body: Any | None = None, timeout: float = 20.0) -> Any: ...


class UrllibRecallTransport:
    async def request(self, method: str, url: str, *, headers: Mapping[str, str], json_body: Any | None = None, timeout: float = 20.0) -> Any:
        return await asyncio.to_thread(self._request, method, url, dict(headers), json_body, timeout)

    def _request(self, method: str, url: str, headers: dict[str, str], json_body: Any | None, timeout: float) -> Any:
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec: URL allowlisted before construction
                body = resp.read()
                if not body:
                    return {}
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            raise RecallClientError(f"Recall HTTP {exc.code}: {body}") from exc


class RecallClientError(RuntimeError):
    pass


@dataclass
class RecallClient:
    api_key: str
    base_url: str
    transport: RecallTransport | None = None

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if self.transport is None:
            self.transport = UrllibRecallTransport()

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self.api_key}", "Accept": "application/json"}

    async def create_bot(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert self.transport is not None
        return await self.transport.request("POST", f"{self.base_url}/api/v1/bot/", headers=self._headers, json_body=payload)

    async def send_chat_message(self, bot_id: str, participant_id: str, text: str) -> dict[str, Any]:
        assert self.transport is not None
        payload = {"to": participant_id, "message": text}
        return await self.transport.request("POST", f"{self.base_url}/api/v1/bot/{bot_id}/chat_message/", headers=self._headers, json_body=payload)

    async def leave_bot(self, bot_id: str) -> dict[str, Any]:
        assert self.transport is not None
        return await self.transport.request("POST", f"{self.base_url}/api/v1/bot/{bot_id}/leave/", headers=self._headers, json_body={})
