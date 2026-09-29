# Hermes Zoom Meeting Chat

Standalone Hermes platform plugin for a narrow v0.4 Zoom meeting-chat and voice-command flow backed by Recall.ai.

- One active meeting per Hermes profile
- One visible Zoom participant named `Hio`
- One paired operator, authorized by a one-use direct-message pairing phrase
- Private Zoom DMs plus a public group route invoked by the paired operator with native `@Hio`
- Finalized `Hotel India …`, `Hotel Hotel …`, or compatible `Hey Hio …` speech from the paired operator invokes the same public route
- Voice invocations receive an immediate public acknowledgement, then one complete public answer
- Finalized speech from all participants is kept in memory as meeting context until leave begins
- Other participants cannot invoke Hio; ordinary group-chat messages are ignored
- No retained recording, audio, video, transcript, or media artifacts; no summaries or calendar auto-join

The plugin stays inert unless explicitly enabled and configured with Recall credentials plus a public HTTPS callback URL. The public lane relies on Hermes's generic per-source context policy to disable private memory and context files; the voice extension adds no further Hermes-core code.

**Status:** API-contract and local integration tests pass. The v0.3 `Hey Hio` path completed a credentialed Zoom/Recall round trip. The v0.4 `Hotel India` and `Hotel Hotel` aliases were chosen from that meeting's live Recall transcript but still require a post-deploy end-to-end trigger test.

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

display:
  platforms:
    zoom_meeting_chat:
      # Recall's Zoom transport cannot edit streamed chunks in place. Buffer
      # the model output and send one complete answer instead.
      streaming: false
```

Secrets must be provided through Hermes profile-scoped secret resolution, not hard-coded YAML:

```text
RECALL_API_KEY=...
RECALL_WEBHOOK_SECRET=whsec_...
```

`RECALL_WEBHOOK_SECRET` must be the Recall workspace signing secret and must begin with `whsec_`. Unsigned callbacks and URL-token fallback are intentionally unsupported.

`callback_bind_host` must remain loopback (`127.0.0.1`, `localhost`, or `::1`). Put HTTPS and public exposure in a reverse proxy or tunnel whose proxy-to-plugin hop is local or authenticated and encrypted. Suppress webhook bodies and secrets in proxy access/error logs.

## Operation

1. From a trusted Hermes surface, call `zoom_chat_join(meeting_url)`.
2. The tool returns a one-use pairing phrase exactly once.
3. Send that phrase as a direct Zoom message to `Hio` within 10 minutes.
4. After pairing, only that immutable Zoom participant ID can trigger Hermes.
5. Private messages use the exact live DM route `meeting:{bot_id}:dm:{participant_id}` and reply only to that participant.
6. To invoke Hio publicly, begin a message in Zoom's group chat with `@Hio`, such as `@Hio summarize the decision`. If Zoom/Recall strips the `@`, `Hio:` is accepted as a compatibility fallback. Hio replies to `everyone`.
7. To invoke Hio by voice, begin a finalized utterance with `Hotel India` or `Hotel Hotel`, such as `Hotel India, what did we decide?`. Compatible `Hey Hio` forms remain aliases. Only the paired participant ID can trigger this route. Hio first posts `Heard — working on it.`, then posts one complete answer to `everyone` in Zoom chat.
8. Recall streams finalized utterances from all participants. The plugin keeps them in memory during the meeting and supplies the transcript so far to every public typed or spoken invocation.
9. The public route uses its own Hermes group-chat session and technically suppresses profile memory and context files. Its only permitted ambient model capability is Hermes's `x_search` through the Tool Search bridge; private and platform-management tools remain unavailable. Hio must answer from the operator request, meeting transcript, and any explicit public web lookup.
10. Use `zoom_chat_leave` to leave. The plugin stops callback admission, removes live routes, and clears its in-memory transcript before it calls Recall's leave endpoint, including when the provider leave later fails.

While a meeting is active, `zoom_chat_status` reports the 20 most recent finalized transcript segments and whether each matched the paired operator's voice wake phrase. This diagnostic state is in memory only and clears on leave.

Recall's Send Chat Message API accepts only the recipient, message text, and pin flag. It does not expose Zoom's native reply/thread target, so public `@Hio` answers and voice answers are top-level meeting-chat messages. Hermes still keeps one isolated group conversation for the meeting, but the plugin cannot create or continue a native Zoom thread through Recall.

An uncertain create or leave is stored under the active Hermes profile and blocks another join across gateway reconnects and restarts. If the saved state has a bot ID, `zoom_chat_leave` retries the idempotent leave request. If the bot ID is unknown, verify in the Recall dashboard that no bot exists, then call `zoom_chat_leave` with `confirmed_absent: true`. The plugin never clears ambiguous bot-lifecycle state merely because the gateway stopped.

## Safety boundaries

The plugin runs a fail-closed compatibility preflight before creating a bot and before inbound dispatch. It uses Hermes's real platform tool resolver plus final model tool schema builder. It permits either zero schemas or only the ambient `x_search` schema assembled into the exact `tool_search`, `tool_describe`, and `tool_call` bridge; it rejects every other schema. It also rejects non-default context engines and missing `known_plugin_toolsets.zoom_meeting_chat` coverage.

Public admission is fail closed: the event must be addressed to `everyone`, come from the already paired participant ID, and start with an anchored Hio invocation. Public and private replies have separate exact destination mappings, so a failed send cannot fall back across audiences.

Recall receives the meeting URL and processes live meeting speech for transcription. The bot payload sets `recording_config.retention: null` and disables audio/video artifact fields, so Recall does not retain transcript or media artifacts under that configuration; this does not make claims about Recall operational metadata.

The plugin stores finalized transcript segments only in the active runtime's memory and clears them as soon as leave starts, including on provider leave failure. It does not write that accumulator to the lifecycle tombstone. Each invoked public Hermes turn still persists the exact prompt it processed—which includes the transcript so far—in the normal isolated Zoom group session. That session follows Hermes's normal retention and can be deleted through Hermes's existing session controls; the plugin does not delete shared gateway session state on its own. Private Zoom DMs also follow normal Hermes session retention.

## Verification

Run from this repository:

```bash
python -m pytest -q
hermes plugins validate .
hermes plugins doctor . --ci
```

No live Recall or Zoom mutations are performed by the test suite.
