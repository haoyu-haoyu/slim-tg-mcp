# Architecture

> Last updated for v0.5.0. See [`security.md`](security.md) for the
> threat model and the rationale behind defenses; this document
> focuses on *how the pieces fit together*.

`slim-tg-mcp` puts **15 lazy-load Skills + 8 always-on MCP tools**
behind a single Unix-socket-served daemon that owns the actual
Telethon session. The split is the whole point: the LLM agent only
ever sees ~1.5k tokens worth of tool descriptions, while the
specialized capabilities (write, admin, media, etc.) sit dormant
until the user's request actually invokes one.

---

## 1. Components

```
┌────────────────────┐
│  Claude Code or    │  user-facing — the agent host
│  any MCP client    │
└─────────┬──────────┘
          │ stdio                          ┌──────────────────────┐
          ▼                                │  ~/.claude/skills/*  │
   ┌────────────────────┐  fork/exec on    │  tg-messaging        │
   │  tgmcp-mcp         │  user request    │  tg-group-admin      │
   │  (8 read tools)    │ ─────────────────▶  tg-contacts         │
   └─────────┬──────────┘                  │  tg-media-upload     │
             │                              │  tg-polls            │
             │  HTTP-on-Unix-socket         │  tg-scheduling       │
             │                              │  tg-profile          │
             ▼                              │  tg-export           │
   ┌─────────────────────────┐              │  tg-privacy          │
   │  tgmcp-daemon           │              │  tg-folders          │
   │  (FastAPI on $RUN/      │              │  tg-stickers-gifs    │
   │   tgmcp/daemon.sock)    │              │  tg-bot              │
   │                         │              │  tg-topics           │
   │   ┌──────────────────┐  │              │  tg-stories          │
   │   │  TGSession map   │  │              │  tg-location         │
   │   │  active_label    │  │              └──────────┬───────────┘
   │   └────────┬─────────┘  │                         │
   │            │            │   HTTP-on-Unix-socket    │
   │            ▼            │ ◀───────────────────────┘
   │   ┌──────────────────┐  │
   │   │  Telethon (1.43) │  │
   │   └────────┬─────────┘  │
   └────────────┼────────────┘
                │ MTProto over TLS
                ▼
          ┌──────────┐
          │ Telegram │
          └──────────┘
```

The boxes:

| Process | Lifetime | Purpose |
|---|---|---|
| `tgmcp-mcp` | per-Claude-conversation | Speaks MCP over stdio, hosts the **8 read/search tools**, talks to the daemon for everything. Re-spawned on each agent restart. |
| `tgmcp-daemon` | long-lived (singleton) | Owns the Telethon session(s). FastAPI on a Unix socket. Survives many MCP restarts; the encrypted session lives in `~/.config/tgmcp/sessions/<label>.enc`. |
| Skill scripts | per-invocation | Lazy-loaded Python scripts in `skills/tg-*/`. Each one reads `--args`, calls the daemon over its socket, prints JSON to stdout. Spawned by Claude Code as `python skills/tg-foo/foo.py …`. |
| `tgmcp` CLI | per-invocation | The user-facing entry point: `tgmcp init`, `tgmcp daemon start`, `tgmcp account use`. Same `DaemonClient` as the Skills. |

Only `tgmcp-daemon` keeps state between requests. Both `tgmcp-mcp`
and the Skill scripts are stateless dispatchers — they exist to
translate one MCP/CLI call into one daemon RPC and back.

---

## 2. Data flow: a Telegram message becoming an LLM tool result

This is the read path. Walk through a single
`tg_search_global("invoice")` call:

```
┌───────────────────────────────────────────────────────────────────┐
│  1. Claude calls MCP tool tg_search_global with {query:"invoice"} │
└─────────────────────────────┬─────────────────────────────────────┘
                              │
                ┌─────────────▼──────────────┐
                │ tgmcp-mcp.call_tool()       │  src/tgmcp/mcp_server/server.py
                │                             │
                │  c.search_global(           │
                │      query="invoice",       │
                │      limit=30)              │
                └─────────────┬───────────────┘
                              │  HTTP POST /search/global
                              │  over $XDG_RUNTIME_DIR/tgmcp/daemon.sock
                              ▼
       ┌────────────────────────────────────────────────────────┐
       │  daemon middleware                                      │
       │  ┌────────────────────────────┐                         │
       │  │ metrics: start time, label │  daemon/metrics.py      │
       │  └────────────┬───────────────┘                         │
       │               ▼                                         │
       │  ┌──────────────────────────────┐                       │
       │  │ FastAPI route /search/global │  daemon/server.py     │
       │  │ → SearchGlobalReq pydantic   │                       │
       │  │   400 on bad input           │                       │
       │  └────────────┬─────────────────┘                       │
       │               ▼                                         │
       │  ┌──────────────────────────────┐                       │
       │  │ _sess() → active TGSession   │                       │
       │  └────────────┬─────────────────┘                       │
       │               ▼                                         │
       │  ┌──────────────────────────────┐                       │
       │  │ TGSession.search_global()    │  daemon/telegram.py   │
       │  │ Telethon iter_messages       │                       │
       │  └────────────┬─────────────────┘                       │
       │               ▼                                         │
       │  ┌──────────────────────────────┐                       │
       │  │ Each Message → TrustContext  │  daemon/sanitizer.py  │
       │  │ → strip zero-width           │                       │
       │  │ → neutralize injection       │                       │
       │  │ → wrap <tg_msg trust=…>      │                       │
       │  └────────────┬─────────────────┘                       │
       │               ▼                                         │
       │  ┌──────────────────────────────┐                       │
       │  │ JSON: {messages:[…]}         │                       │
       │  └────────────┬─────────────────┘                       │
       │               ▼                                         │
       │  metrics: observe_request(/search/global, 200, dt)     │
       └────────────────────────────────────────────────────────┘
                              │
                ┌─────────────▼──────────────┐
                │ tgmcp-mcp.format_messages   │
                │ → return TextContent to MCP │
                └─────────────┬───────────────┘
                              ▼
              Claude sees `<tg_msg trust="low" sender_id="…">`
              `[[neutralized:'ignore previous instructions']] click pls`
              `</tg_msg>` plus per-message metadata.
```

Key invariants on the read path:
- **Sanitization is the daemon's job, not the MCP server's.** The MCP
  server is a thin dispatcher; if you swap it for a different MCP
  binding, the daemon still wraps/normalizes.
- **The Trust label comes from the *content author*, not the relayer.**
  A forwarded message is `low` even if forwarded by the user.
- **Audit log is bypass-proof for write paths only.** Reads (search,
  list, etc.) deliberately skip the audit log to keep it small.

---

## 3. Data flow: a write through a Skill

This is the write path. Walk through a single `tg-messaging send`:

```
┌─────────────────────────────────────────────────────────────────┐
│  Claude decides to send a message → reads tg-messaging SKILL.md │
│  → "I'll run python …/skills/tg-messaging/act.py send …"        │
└─────────────────────────────┬───────────────────────────────────┘
                              │   subprocess
                              ▼
              ┌─────────────────────────────────┐
              │ skills/tg-messaging/act.py       │
              │  argparse:                       │
              │   send --chat @x --text "…"      │
              │     [--reply-to N]               │
              │  cmd_send(args) → c.send(...)    │
              └─────────────┬───────────────────┘
                            │  HTTP POST /send  (DaemonClient)
                            ▼
       ┌──────────────────────────────────────────────────────┐
       │  daemon/server.py /send                              │
       │  ┌────────────────────────────┐                      │
       │  │ SendReq pydantic           │   400 on bad input   │
       │  └────────────┬───────────────┘                      │
       │               ▼                                       │
       │  ┌────────────────────────────┐                      │
       │  │ TGSession.send()           │  daemon/telegram.py  │
       │  │   Telethon send_message    │                      │
       │  └────────────┬───────────────┘                      │
       │               ▼                                       │
       │  ┌────────────────────────────┐                      │
       │  │ audit.log("send", chat=…,  │  daemon/audit.py     │
       │  │   msg_id=N)                │  body never logged   │
       │  └────────────┬───────────────┘                      │
       │               ▼                                       │
       │  metrics: observe_request(/send, 200, dt)            │
       └───────────────┬──────────────────────────────────────┘
                       │  {"ok":true,"msg_id":42}
                       ▼
                Skill prints JSON to stdout → Claude sees confirmation
```

Write-path-specific properties:
- **Confirmation flags live at the Skill layer**, not the daemon.
  `tg-media-upload --yes`, `tg-export --yes`, `tg-stories mark-read
  --ack`, `tg-location send-live --confirm-indefinite` all enforce
  hard gates *before* the daemon RPC is issued.
- **TOCTOU defense lives at the daemon layer.** When a Skill passes a
  filesystem path (`tg-media-upload`, `tg-export`, `tg-profile photo`),
  the daemon does the `lstat → O_NOFOLLOW open → fstat (st_dev,
  st_ino) match` dance. See [security.md §3.4](security.md#34-toctou-resistant-file-ops-t2-t4).
- **Audit log records non-sensitive metadata only.** No body, no full
  paths, no phone numbers, no passwords, no privacy allowlists.

---

## 4. Multi-account & lazy session loading

```
                             ┌─ /accounts          (list known labels)
                             │
       tgmcp account use X ──┼─ POST /accounts/switch
                             │   ├─ if X not loaded:
                             │   │   ├─ asyncio.Lock(X)
                             │   │   ├─ auth.load_session(X)
                             │   │   │   (decrypt with keychain or
                             │   │   │    passphrase via TGMCP_PASSPHRASE_FD)
                             │   │   ├─ TGSession.start()
                             │   │   ├─ kind cross-check (me.bot vs envelope)
                             │   │   └─ state.sessions[X] = sess
                             │   └─ state.active_label = X
                             │
                             └─ /shutdown          (instance-id-bound)
```

A daemon can have any number of `TGSession`s loaded simultaneously
in `state.sessions: dict[label, TGSession]`. `active_label` selects
which one subsequent (non-`/accounts/*`) RPCs use.

Per-label `asyncio.Lock` serializes concurrent loads — without it
two parallel `/accounts/switch` calls to the same cold label could
both call `_open_session`, leaving the first live `TGSession`
unreachable and never stopped on lifespan teardown.

Bot accounts (kind=`"bot"`) and user accounts (kind=`"user"`) live
side-by-side in the same map. The kind is part of the encrypted
envelope's AAD, so flipping it post-creation breaks decryption (see
[security.md §3.5](security.md#35-encryption-at-rest-t3)).

---

## 5. Singleton + lifecycle

The daemon is single-instance per Unix user:

```
flock(LOCK_PATH, LOCK_EX|LOCK_NB)  ← authoritative liveness signal
        │       (kernel auto-releases on process exit)
        ▼
PID_PATH = <pid>\n<instance_id>\n   ← advisory; not used for liveness
        │
        ▼
SOCKET_PATH bound, server.serve()
        │
        ├─ POST /shutdown  (id must match instance_id)  → graceful exit
        ├─ SIGTERM         (transport-failure fallback) → graceful exit
        └─ flock release   (process death)              → SUBSEQUENT
                                                          startup wins
                                                          the race
```

The `instance_id` binding is what makes shutdown safe across
restarts: a stale "stop" command from a previous tgmcp invocation
arriving after a successor daemon has taken the socket gets refused
because the id doesn't match the current daemon's id.

---

## 6. Configuration & paths

| Path | Tier | Why |
|---|---|---|
| `$XDG_RUNTIME_DIR/tgmcp/daemon.sock` (or `/tmp/tgmcp-<uid>/daemon.sock`) | runtime | Unix socket — must be on a guaranteed-local FS for `flock` to be reliable. |
| `~/.config/tgmcp/sessions/<label>.enc` | persistent | AES-GCM-encrypted Telethon `StringSession` plus envelope (kdf, kind). |
| `~/.config/tgmcp/audit.log` | persistent | Append-only metadata audit log. |
| `~/.config/tgmcp/daemon.log` | persistent | uvicorn/fastapi access log. |

Persistent paths come from `pwd.getpwuid(os.getuid())` — never `$HOME`
— to defeat env-poisoning attacks. Runtime paths fall back to
hardcoded `/tmp` (not `tempfile.gettempdir()`) so we don't honor
`$TMPDIR` pointing at NFS mounts.

See [security.md §3.3](security.md#33-path-safety-t2-t5) for
threat-model details.

---

## 7. Observability

The daemon exposes Prometheus metrics on `/metrics` (text/plain
exposition format, OpenMetrics-compatible):

| Metric | Type | Labels | Description |
|---|---|---|---|
| `tgmcp_rpc_requests_total` | Counter | `endpoint`, `status` | One increment per request. `endpoint` is the matched FastAPI route template (drops path params); unmatched 404s bucket to `__unmatched__`. |
| `tgmcp_rpc_request_seconds` | Histogram | `endpoint` | End-to-end latency, buckets tuned for Telethon round-trips (5 ms…30 s). |
| `tgmcp_sessions_loaded` | Gauge | – | How many `TGSession` instances are currently loaded. |
| `tgmcp_daemon_up` | Gauge | – | 1 after lifespan startup, 0 during teardown / before. |

Cardinality is intentionally bounded — labels never include
user-controllable strings (chat ids, account labels, message
contents). A noisy caller cannot explode the series count.

Audit log is the *event* trail; metrics are the *aggregate* trail.
They serve different consumers (forensics vs. ops dashboards).

---

## 8. Where each Skill lives in the dataflow

The 15 Skills all share the same shape: argparse → DaemonClient →
HTTP RPC → JSON to stdout. They differ only in which daemon endpoints
they call and which confirmation flags they enforce client-side.

| Skill | Daemon endpoints used | Notable client-side gate |
|---|---|---|
| `tg-messaging` | `/send`, `/edit`, `/delete`, `/forward`, `/pin`, `/unpin`, `/react`, `/mark_read` | `--no-revoke` for delete; `--big`/`--custom-emoji-id` for premium reactions |
| `tg-group-admin` | `/chat/*` (create / add-member / kick / ban / invite / participants / signatures / slow-mode / discussion / admin-log) | Bilingual triggers + destructive-confirm checklist in SKILL.md |
| `tg-contacts` | `/contacts/{add,delete,block,unblock,search}` | E.164 enforced at schema; search filtered to actual contacts |
| `tg-media-upload` | `/send_media` | `--yes` OR `--confirm-chat`+`--confirm-file`; `_open_validated_upload` TOCTOU pipeline |
| `tg-polls` | `/poll/{create,edit,close,results}` | `edit` refuses quiz polls (correct_answers can't round-trip); option count immutable |
| `tg-scheduling` | `/scheduled/{send,edit,list,delete}`, `/draft/{save,get,clear}` | tz-aware ≤365 day window with re-check just before dispatch |
| `tg-profile` | `/profile/{update,username,photo,photo_delete,status,2fa,emoji_status}` | 2fa requires TTY; passwords via getpass, never argv |
| `tg-export` | `/export/chat` | `--yes` OR confirm pair; `_open_validated_export_dir` + dir_fd-relative ops |
| `tg-privacy` | `/privacy/{get,set}` | Audit logs key+rule count, never user-id allowlists |
| `tg-folders` | `/folders/{list,update,delete}` | folder_id 2..255; title ≤12 UTF-8 chars |
| `tg-stickers-gifs` | `/gif/{saved,send}`, `/sticker/{saved,set,send}` | List output emits canonical (doc_id, access_hash, file_reference_hex) shape |
| `tg-bot` | `/bot/{send_keyboard,answer_callback,poll_callbacks,set_commands}` | Refuses if active account isn't a bot (400 from `_require_bot_session`) |
| `tg-topics` | `/topics/{list,create,edit,delete,pin}` | `delete --yes` (irreversible) |
| `tg-stories` | `/stories/{active,pinned,mark_read,delete}` | `mark-read --ack` (lurk-safe default); `delete` hardcodes peer="me" |
| `tg-location` | `/location/{send,send_live,edit_live,stop_live}` | `send-live --confirm-indefinite` for period=0x7FFFFFFF (client-side gate; `stop_live` reuses last-known coords as a daemon-side defense, not a Skill gate) |

---

## 9. What's *not* in this architecture

- **No webhook listener.** The daemon is request/response only; it
  doesn't subscribe to Telegram updates beyond what each RPC needs.
  Bot mode includes a small in-memory CallbackQuery queue (deque
  maxlen=200, drop-oldest) drained via `/bot/poll_callbacks`, but
  there's no long-poll/SSE/WS surface.
- **No multi-tenant isolation.** One daemon = one Unix user.
- **No transcoding.** Media goes through the upload pipeline as-is;
  we don't OCR images or transcribe audio. Bytes flow through
  Telethon, the daemon never reads the body itself.
- **No persistence of read content.** Search results are not cached
  on disk; they live in the request/response cycle and the LLM's
  context.

These omissions are deliberate — each one is a category of
complexity that would significantly increase the attack surface
without serving the project's primary use case.
