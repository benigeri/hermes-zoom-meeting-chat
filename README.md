# Hermes Zoom Meeting Chat

Standalone Hermes platform plugin for a narrow v0.6.0 Zoom meeting-chat, voice-command, and calendar auto-join flow backed by Recall.ai.

- One active meeting per Hermes profile
- One visible Zoom participant named `Hio`
- One trusted operator, learned from a one-use pairing phrase and then matched by a hash of Zoom's stable `conf_user_id`
- Private Zoom DMs plus a public group route invoked by the trusted operator with native `@Hio`
- Finalized `Hotel India …`, `Hotel Hotel …`, or compatible `Hey Hio …` speech from the trusted operator invokes the same public route; bounded fillers such as `Okay` and `All right` are accepted only before the wake phrase
- If Recall splits the wake phrase from its command, the trusted speaker's next finalized segment is accepted for three seconds; a standalone `hotel` only arms this bounded fallback
- Voice invocations receive an immediate public acknowledgement, then one complete public answer
- Finalized speech from all participants is kept in memory as meeting context until leave begins
- Other participants cannot invoke Hio; ordinary group-chat messages are ignored
- No retained recording, audio, video, or full-transcript artifacts; bounded trusted-operator voice diagnostics persist until the next successful join
- Optional native-Hermes cron companion auto-joins accepted Zoom events from Paul's primary Google Calendar
- Signed Recall bot-status webhooks clear ended meeting state so back-to-back auto-joins do not remain blocked by a stale local tombstone

The plugin stays inert unless explicitly enabled and configured with Recall credentials plus a public HTTPS callback URL. After trusted-operator admission, Zoom uses Hermes's normal profile context, tools, and approval/control path. Public replies remain visible to everyone in the meeting. The voice extension adds no Zoom-specific Hermes-core code.

**Status:** API-contract and local integration tests pass. The v0.3 `Hey Hio` path completed a credentialed Zoom/Recall round trip. The v0.4 `Hotel India` and `Hotel Hotel` aliases were chosen from that meeting's live Recall transcript but still require a post-deploy end-to-end trigger test.

## Files

- `plugin.yaml` — native Hermes platform manifest; declares the three management tools.
- `__init__.py` — import-light plugin entry point.
- `adapter.py` — Hermes `BasePlatformAdapter` implementation.
- `client.py` — minimal Recall REST client with injectable fake transport.
- `runtime.py` — profile-scoped one-meeting runtime, pairing, routing, lifecycle, payload construction.
- `webhook.py` — Recall workspace HMAC verification and bounded callback admission.
- `tools.py` — manual join, secret-safe calendar join, leave, and status management tools.
- `scripts/calendar_auto_join_monitor.py` — deterministic primary-calendar Zoom candidate collector for Hermes cron.
- `tests/` — fake Recall/webhook/plugin-discovery and calendar-candidate tests.

## Configuration sketch

Install into a Hermes plugin directory, enable it only on a trusted control surface, and configure the platform separately from the Zoom chat surface:

```yaml
plugins:
  enabled:
    - zoom_meeting_chat-platform

platform_toolsets:
  # Give authenticated Zoom operator turns the same profile toolsets as Slack.
  # Keep this list synchronized with platform_toolsets.slack.
  zoom_meeting_chat:
    - browser
    - clarify
    - code_execution
    - cronjob
    - deepgram_recordings
    - delegation
    - file
    - image_gen
    - memory
    - session_search
    - skills
    - terminal
    - todo
    - tts
    - vision
    - web
    - zoom_meeting_chat_admin

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

`RECALL_WEBHOOK_SECRET` must match the Recall/Svix endpoint signing secret and must begin with `whsec_`. Unsigned callbacks and URL-token fallback are intentionally unsupported.

In Recall's regional Webhooks dashboard, create one endpoint at
`https://<callback_public_base_url>/webhooks/recall/zoom-meeting-chat` and subscribe it to `bot.call_ended`, `bot.done`, and `bot.fatal`. The endpoint secret must match `RECALL_WEBHOOK_SECRET`. These signed lifecycle events are separate from the per-bot real-time chat and transcript endpoint configured in the Create Bot payload.

`callback_bind_host` must remain loopback (`127.0.0.1`, `localhost`, or `::1`). Put HTTPS and public exposure in a reverse proxy or tunnel whose proxy-to-plugin hop is local or authenticated and encrypted. Suppress webhook bodies and secrets in proxy access/error logs.

## Operation

1. From a trusted Hermes surface, call `zoom_chat_join(meeting_url)`.
2. The tool returns a one-use fallback pairing phrase exactly once.
3. On first use, send that phrase as a direct Zoom message to `Hio` within 10 minutes. The plugin stores only a SHA-256 hash of the paired account's stable Zoom `conf_user_id`, in a mode-`0600` profile state file.
4. On later calls, matching `conf_user_id` events auto-authorize Paul's current meeting participant ID. If Zoom omits the stable ID because Paul joined as a guest or logged out, the plugin fails closed and requires the one-use phrase for that meeting.
5. Private messages use the exact live DM route `meeting:{bot_id}:dm:{participant_id}` and reply only to that participant.
6. To invoke Hio publicly, begin a message in Zoom's group chat with `@Hio`, such as `@Hio summarize the decision`. If Zoom/Recall strips the `@`, `Hio:` is accepted as a compatibility fallback. Hio replies to `everyone`.
7. To invoke Hio by voice, begin a finalized utterance with `Hotel India` or `Hotel Hotel`, such as `Hotel India, what did we decide?`. Compatible `Hey Hio` forms remain aliases. Bounded leading fillers such as `Okay` and `All right` are accepted, but incidental mid-sentence mentions are not. Only the trusted operator's current participant ID can trigger this route. Hio first posts `Heard — working on it.`, then posts one complete answer to `everyone` in Zoom chat.
8. Recall streams finalized utterances from all participants. The plugin keeps them in memory during the meeting and supplies the transcript so far to every public typed or spoken invocation.
9. Authenticated Zoom operator turns use the same profile context, toolsets, and Hermes approval/control path as Slack. The public route uses its own Hermes group-chat session, but its answers are visible to everyone in the meeting. Normal tool-level approval gates still apply.
10. Use `zoom_chat_leave` to leave. The plugin stops callback admission, removes live routes, and clears its in-memory transcript before it calls Recall's leave endpoint, including when the provider leave later fails. A matching signed terminal bot-status webhook also clears the local active state and tombstone after Recall ends the bot automatically.

### Calendar auto-join companion

Calendar auto-join uses Hermes's native scheduler rather than a second bot runtime. Configure a one-minute agent cron with a thin `~/.hermes/scripts/` entrypoint that runs `scripts/calendar_auto_join_monitor.py` as its deterministic `monitor`, then expose only the `zoom_meeting_chat_admin` toolset to that job. (Hermes monitor paths are relative to `~/.hermes/scripts/`.) The collector reads the primary Google Calendar through Hermes-managed OAuth and emits only the event ID plus start/end times; it omits the event title and every Zoom URL. The cron calls `zoom_chat_join_calendar_event(event_id)`, which refetches the event and resolves its secret-bearing Zoom URL inside the admin tool without putting that URL in monitor state or the model prompt. A candidate is emitted only when:

- the event is timed, confirmed, and accepted by the authenticated user (organizer-owned events without a self-attendee row are accepted);
- a `zoom.us/j/…` URL appears in the location, description, hangout link, or conference entry points; and
- the meeting is underway or begins within 90 seconds.

For back-to-back calls, the newest eligible start wins so the cron can leave the ending meeting and join the next one. The plugin still enforces one active meeting per profile. After the first manual pairing, a successful auto-join authorizes Paul's stable Zoom account automatically; the returned one-use phrase remains a fallback when Zoom omits the stable account ID. Events without Zoom links, declined/cancelled events, and all-day blocks are ignored.

The scheduler job should deliver only successful joins and actionable failures to Paul's trusted control channel. The deterministic monitor emits `candidate: null` while idle; when that baseline first wakes the agent, the agent must return `[SILENT]`. This companion covers the authenticated primary Google Calendar only; it does not imply access to separate Google accounts or non-Zoom conference providers.

While a meeting is active, `zoom_chat_status` reports the 20 most recent finalized transcript segments and whether each matched the trusted operator's voice wake phrase. The full transcript diagnostic stays in memory and clears on leave. To diagnose missed wakes after a call, the plugin persists at most 50 finalized utterances from the trusted operator only, with relative time, parser outcome, and match status, in a mode-`0600` profile state file. It retains that bounded log after leave and clears it after the next successful join. It never persists other speakers' utterances in this diagnostic log.

Recall's Send Chat Message API accepts only the recipient, message text, and pin flag. It does not expose Zoom's native reply/thread target, so public `@Hio` answers and voice answers are top-level meeting-chat messages. Hermes still keeps one isolated group conversation for the meeting, but the plugin cannot create or continue a native Zoom thread through Recall.

An uncertain create or leave is stored under the active Hermes profile and blocks another join across gateway reconnects and restarts. If the saved state has a bot ID, `zoom_chat_leave` retries the idempotent leave request. If the bot ID is unknown, verify in the Recall dashboard that no bot exists, then call `zoom_chat_leave` with `confirmed_absent: true`. The plugin never clears ambiguous bot-lifecycle state merely because the gateway stopped.

## Safety boundaries

Only post-admission trusted-operator events reach Hermes. Those events opt into Hermes's normal gateway-control path so approvals, clarification replies, and other native controls work as they do in Slack. Pre-pair, wrong-identity, and missing-stable-identity events do not dispatch. The adapter does not invent a Zoom-specific tool or context policy; profile configuration supplies the same toolsets as Slack.

Public admission is fail closed: the event must be addressed to `everyone`, come from the current participant ID bound to the trusted stable Zoom account (or the one-use fallback pairing), and start with an anchored Hio invocation. Names and host status never grant access. Public and private replies have separate exact destination mappings, so a failed send cannot fall back across audiences.

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
