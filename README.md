# Hermes Zoom Meeting Chat

Standalone Hermes platform plugin for a narrow v0.1 Zoom meeting-chat flow backed by Recall.ai.

- One active meeting per Hermes profile
- One visible Zoom participant named `Hio`
- One paired operator, authorized by a one-use direct-message pairing phrase
- Direct Zoom messages only; public/group meeting chat and other participants are ignored
- No transcript, recording, audio, video, captions, media output, summaries, or calendar auto-join
- No Hermes core changes

The plugin stays inert unless explicitly enabled and configured with Recall credentials plus a public HTTPS callback URL.

## Files

- `plugin.yaml` — native Hermes platform manifest; declares the three management tools.
- `__init__.py` — import-light plugin entry point.
- `adapter.py` — Hermes `BasePlatformAdapter` implementation.
- `client.py` — minimal Recall REST client with injectable fake transport.
- `runtime.py` — profile-scoped one-meeting runtime, pairing, routing, lifecycle, payload construction.
- `webhook.py` — Recall workspace HMAC verification and bounded callback admission.
- `tools.py` — `zoom_chat_join`, `zoom_chat_leave`, `zoom_chat_status` management tools.
- `tests/` — fake Recall/webhook/plugin-discovery tests.

## Configuration sketch

Install into a Hermes plugin directory, enable it only on a trusted control surface, and configure the platform separately from the Zoom chat surface:

```yaml
plugins:
  enabled:
    - zoom_meeting_chat-platform

platform_toolsets:
  # Do not expose management tools to Zoom-originated turns.
  zoom_meeting_chat: [no_mcp]

known_plugin_toolsets:
  # Must include every currently registered plugin toolset, including this admin toolset.
  zoom_meeting_chat:
    - zoom_meeting_chat_admin

gateway:
  platforms:
    zoom_meeting_chat:
      enabled: true
      extra:
        recall_base_url: https://us-west-2.recall.ai
        callback_bind_host: 127.0.0.1
        callback_bind_port: 8765
        callback_public_base_url: https://your-public-callback.example
        in_call_not_recording_timeout: 1800
        automatic_leave_timeout: 7200
```

Secrets must be provided through Hermes profile-scoped secret resolution, not hard-coded YAML:

```text
RECALL_API_KEY=...
RECALL_WEBHOOK_SECRET=whsec_...
```

`RECALL_WEBHOOK_SECRET` must be the Recall workspace signing secret and must begin with `whsec_`. Unsigned callbacks and URL-token fallback are intentionally unsupported.

## Operation

1. From a trusted Hermes surface, call `zoom_chat_join(meeting_url)`.
2. The tool returns a one-use pairing phrase exactly once.
3. Send that phrase as a direct Zoom message to `Hio` within 10 minutes.
4. After pairing, only that immutable Zoom participant ID can trigger Hermes.
5. Replies are sent only to the exact live DM route `meeting:{bot_id}:dm:{participant_id}`.
6. Use `zoom_chat_leave` to leave. If create/leave is uncertain, check Recall before rejoining.

## Safety boundaries

The plugin runs a fail-closed compatibility preflight before creating a bot and before inbound dispatch. It uses Hermes's real platform tool resolver plus final model tool schema builder and requires zero model-facing tool schemas. It also rejects non-default context engines and missing `known_plugin_toolsets.zoom_meeting_chat` coverage.

Recall receives the meeting URL and callback URL. Hermes may retain tool arguments and Zoom DMs in normal session storage. `retention: null` disables Recall artifact retention for this chat-event bot configuration; it does not disable Hermes session storage or make claims about Recall operational metadata.

## Verification

Run from this repository:

```bash
python -m pytest -q
hermes plugins validate .
hermes plugins doctor . --ci
```

No live Recall or Zoom mutations are performed by the test suite.
