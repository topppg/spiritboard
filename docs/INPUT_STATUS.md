# Input status across agent interfaces

Spiritboard tracks unanswered questions and approvals by storage profile, native
session and request ID. One request's answer cannot clear another request.
Pending input does not expire because its transcript is old, and survives a board
restart. Agent lifecycle events and native transcript records supply evidence;
terminal screens and terminal brands are not involved.

An uninstrumented agent may display a question before recording it. Without an
event or a persisted request, Spiritboard cannot know that question exists.
Stale activity therefore reads **status unknown**. Profile tooltips and opened
session details show whether input detection is transcript-only, configured, or
has observed lifecycle events. Observing some events does not establish complete
coverage for every tool or custom agent host.

Explicit native execution, resume and turn-end events also update an older
transcript's execution state. The board uses their observation time for freshness,
sorting, filters and turn notifications while preserving transcript timestamps
and history.
A request-resolution event alone does not establish that the agent is working.
Execution evidence becomes stale after 60 seconds without another report; an open
process alone cannot establish continued work during a long silent operation.

## Setup

Install or remove only Spiritboard's named hooks/extensions explicitly:

```sh
spiritboard inputs install
spiritboard inputs status
spiritboard inputs remove
```

Use the board's `--data-dir` if it has a custom data directory. `--config` accepts
the same profile file as `serve`; repeat `--source` or `--profile-id` to select
sources. `spiritboard discover` lists profile IDs without revealing their paths.
For example:

```sh
spiritboard inputs install --source claude --source codex
spiritboard inputs install --profile-id example-opencode --opencode-config-dir /example/opencode-config
```

OpenCode's database and config directories are separate. Select one OpenCode
profile per config directory. Custom databases require an explicit
`--opencode-config-dir` unless the database is selected by the current process's
`OPENCODE_DB`. This prevents binding another agent instance to the wrong profile.
Pi's installation needs its agent root containing `sessions/`; a flat
`PI_CODING_AGENT_SESSION_DIR` alone cannot identify where extensions load.

Installation preserves other settings and hooks, refuses malformed or linked
configuration, and keeps private backups of changed JSON settings next to them.
It writes Claude `settings.json`, Codex `hooks.json`, OpenCode
`plugins/spiritboard-input.js`, or Pi `extensions/spiritboard-input.js` in the
selected agent configuration. Removal deletes only Spiritboard entries/assets.
It does not install dependencies, change credentials, or approve agent actions.

In Codex, review the generated hooks with **`/hooks`** before trusting them;
installation alone does not bypass Codex's trust gate. Restart or reload agent
extensions where required. Existing open questions are not replayed by installing
hooks. The running Spiritboard version must include this receiver, and the
Python environment used by the generated command must remain installed.

## Source coverage

| Source | Question/approval evidence | Resolution and limits |
| --- | --- | --- |
| Claude | Recorded `AskUserQuestion` / `ExitPlanMode`; pre-tool, permission and identifiable MCP elicitation hooks before persistence. | Matching tool/elicitation results, tool-batch completion, new prompts or session/turn end. Hooks work across local terminal, IDE and Desktop Code interfaces that load the configured profile. |
| Codex | Recorded direct input tools; lifecycle hooks; app-server question, approval and runtime-status events when a host forwards its existing connection. | Matching result/request-resolution events or user/session lifecycle. Async tool acknowledgement and turn completion do not resolve an outstanding async question. Specialized tools can bypass hooks. |
| OpenCode | Plugin `question.asked` and `permission.asked`. | Matching reply/rejection, disposal of the owning plugin instance. A plugin-loaded runtime supplies events independently of its TUI, IDE or desktop client. |
| Pi | Extension `ui_prompt_start` for select, confirm, input, editor and custom dialogs. | Matching `ui_prompt_end`, genuine session start/replacement or shutdown. Nested dialogs share the harness's prompt span. |
| DeepSeek Harness | Persisted `approval/asked`. | Matching `approval/decided` or turn end. Missing decompression support or a truncated log can leave state unknown. |
| Grok Bot | Cached unresolved widgets, forms and box requests. | Explicit resolution fields. Unrelated activity and the previous 24-hour cutoff no longer discard pending input. Cache coverage is limited by what the native app has downloaded. |

Native capabilities and versions still matter. Cloud/remote execution and custom
SDK hosts must deliver the same events where the agent actually runs; local
configuration cannot instrument an unrelated remote host. Spiritboard never
starts a second agent server to guess another running server's in-memory state.

Claude's permission hooks do not report the instant a user approves a long-running
command. That last-known approval may remain pending until tool or batch
completion; an automatic denial can also enter the permission flow. Precise
decision telemetry is a separate, opt-in integration and is not enabled here.
Identical concurrent permission inputs without native request IDs cannot be
distinguished by their correlation hash. These are limitations of the supplied
evidence, not terminal-specific handling.

OpenCode hydration is feature-detected and reuses its existing authenticated
client transport to read pending questions and permissions after initialization.
When typed list methods are absent, the adapter uses the current SDK's guarded
internal transport. Unsupported or failed hydration leaves coverage uncertain
rather than claiming an empty pending list. An abrupt harness crash without a shutdown event
can leave its last-known request pending until a subsequent lifecycle/resolution
signal. Reloading a board preserves that evidence; it does not prove the agent
is still waiting. A positively verified closed local session stays closed;
directory-based Pi/OpenCode liveness guesses cannot override explicit input.

These mappings follow the [Claude hooks reference](https://code.claude.com/docs/en/hooks),
[Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Codex app-server](https://learn.chatgpt.com/docs/app-server),
[OpenCode plugins](https://opencode.ai/docs/plugins/) and
[Pi extension lifecycle](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md).

## Shared event contract

Any host can forward native input state without a terminal adapter. The local
receiver accepts JSON on stdin, exits successfully without output, and never
returns a permission decision to the agent:

```sh
spiritboard input-event --source codex --profile-id example-profile --event request
```

The synthetic stdin for that command is:

```json
{"session_id":"synthetic-session","request_id":"choice-1","kind":"question"}
```

The matching answer uses `--event resolve` and the same session/request IDs.
`kind` is `question` or `approval`; actions are `request`, `resolve`, `resume`
(new user turn), `idle` (finished turn), and `close` (session ended). The
`resume`, `idle` and `close` boundaries clear earlier requests. Runtime status
reports use `active` or `inactive` to report working or idle without clearing unanswered input, such
as an asynchronous question that outlives a turn. IDs must be scoped to the
profile/session. Use a new ID for each interaction when native IDs are available; provide `occurred_at` as an ISO timestamp when replaying events so
duplicates and out-of-order delivery retain their original ordering.

Claude/Codex native hook payloads and OpenCode/Pi plugin events are normalized by
their source adapter. For a Codex app-server connection, forward the existing
JSON-RPC envelope with its `method`, request `id`, `threadId`, native `itemId` and eventual
`serverRequest/resolved`. Spiritboard does not create or control that connection.

## Local storage and privacy

The receiver reads at most 1 MiB. It discards prompts, answers, tool arguments,
tool results, transcript paths, and private exception text. Only profile/session
IDs, request correlation IDs or hashes, event kinds and timestamps enter private
`inputs.sqlite3` in the board data directory. Callback errors produce no stdout
or permission decisions. No telemetry exporter or external service is enabled.

Unresolved input overlays live snapshots. It does not fabricate transcript
heartbeats, alter transcript `last_seen`, change stored native observations,
rewrite historical snapshots, or send callback input to AI summary providers.
Resolved ordering metadata is retained for one day and pruned at startup and
hourly. Resolutions still referenced by a stored native question are retained,
including outside the display limit, so an old transcript cannot resurrect an
answered question. Unanswered requests remain until an explicit resolution/lifecycle event.
The generic receiver supports out-of-order events within that retained history.
