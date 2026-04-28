# Changelog

All notable changes to this project follow [Semantic Versioning](https://semver.org/).

## [0.5.0] - 2026-04-28

Phase 4: closed remaining gaps with chigwell/telegram-mcp by adding
**bot mode**, **forum topics**, **stories**, **live location**,
**premium reactions + emoji status**, plus operational pieces
(Prometheus metrics, architecture doc).

### Added — four new skills (Phase 4)

- **tg-bot** — Bot-mode operations on a separate auth path. New
  `tgmcp init --bot-token <token>` flow (token via stdin or env;
  argv warned). Auth envelope's AAD now binds `(label, kind)` so
  flipping kind in a stolen envelope breaks decryption. TGSession
  cross-checks `me.bot` against envelope kind on start. Skills:
  `send` (inline keyboards, callback / url buttons with strict
  scheme + size validation), `poll` (drains in-memory CallbackQuery
  queue, deque maxlen=200 dropping oldest), `answer`, `commands`.
  Skill refuses if active account isn't a bot.

- **tg-topics** — Forum supergroup topics: list / create / edit /
  delete / pin. Telethon 1.43 puts these RPCs in `messages.*` (not
  `channels.*`); CreateForumTopicRequest needs a random_id (we use
  `secrets.randbits(63)`); list filters out `ForumTopicDeleted`
  variants. Delete requires `--yes` (irreversible).

- **tg-stories** — Read peer's active and pinned stories, mark-read
  (`--ack` required — viewed receipt is observable, lurk-safe
  default), delete own (peer hardcoded to "me"). Send is
  intentionally NOT exposed; story creation needs media + privacy
  rules + period and deserves its own batch.

- **tg-location** — Static pin (`send`), live share with period in
  `[60, 86400] s` or 0x7FFFFFFF for indefinite, edit-live, stop-live.
  Indefinite share requires `--confirm-indefinite`. `stop_live`
  reads the existing message's `media.geo` and reuses last-known
  coords (NOT (0,0), which would re-anchor to Null Island).

### Added — premium capabilities

- `tg-messaging react` extended with `--custom-emoji-id` and `--big`
  flags (Telegram Premium). XOR enforced between `--emoji` and
  `--custom-emoji-id` at both schema and skill layer (counts presence,
  not truthiness — `--custom-emoji-id 0` is valid).
- `tg-profile emoji-status` (Telegram Premium): set or clear emoji
  status with optional `--until` auto-removal time (must be tz-aware
  and in the future).
- Daemon exception handler now also classifies exceptions from
  `telethon.errors.*` and `telethon.tl.*` modules as upstream (502),
  so PremiumAccountRequiredError (no RPCError suffix) maps correctly.

### Added — observability

- **Prometheus `/metrics` endpoint**. Counter
  `tgmcp_rpc_requests_total` (endpoint, status), histogram
  `tgmcp_rpc_request_seconds` (endpoint, custom buckets), gauges
  `tgmcp_sessions_loaded` and `tgmcp_daemon_up`. Endpoint label uses
  the FastAPI route template; unmatched paths bucket to
  `__unmatched__` to bound cardinality. `/metrics` excluded after
  routing so it doesn't dominate its own histogram.

### Added — documentation

- **`docs/architecture.md`** — 9-section architecture doc covering
  components, read-path / write-path data flows, multi-account lazy
  loading, singleton lifecycle, paths, observability, and a per-Skill
  endpoint+gate matrix for all 15 skills.

### Tests

- 509 unit tests (up from 365 at v0.4.0). Each Codex review across
  Phase 4 closed an issue with a regression test pinning the fix.

### Acknowledgments

Phase 4 ran 14 additional rounds of Codex review (gpt-5.4) on top of
the 42 prior rounds — total 56 review iterations across the project.
Across all four phases combined: **11 BLOCKER + 60+ MAJOR + 35+
MINOR** issues found and fixed, every one with a pinned test.


## [0.4.0] - 2026-04-28

Phase 3: closed the gap with chigwell/telegram-mcp by adding the
remaining write-side coverage and the read-side enumeration tools we
were missing.

### Added — four new skills

- **tg-privacy** — Read or change all 10 Telegram privacy keys
  (status, photo, calls, forwards, chat_invite, phone, added_by_phone,
  voice, about, p2p) with the full 6-rule grammar
  (allow/disallow × all/contacts/users). PrivacyRule model validator
  enforces that `*_users` rules carry a non-empty `user_ids` list and
  that other kinds do NOT carry one (silent drop would surprise
  callers). Audit logs the key + rule count, never the user-id
  allowlists.

- **tg-folders** — List, create-or-update, and delete chat folders
  (Telegram dialog filters). folder_id is bounded 2..255 (Telegram
  reserves 0/1). Title cap is the real Telegram limit of 12 UTF-8
  chars (not 64). Model validator requires at least one inclusion
  source so empty folders bounce here as 400 instead of as
  FILTER_INCLUDE_EMPTY upstream.

- **tg-stickers-gifs** — Saved-GIF list+send, sticker-pack list +
  resolve-to-stickers + send. Direct GIF search is intentionally NOT
  shipped — Telegram's user API does not expose a SearchGifs RPC
  (that surface is via inline bots). The list→send pipeline is
  self-consistent: every list endpoint emits the canonical
  (doc_id, access_hash, file_reference_hex, mime_type) shape that
  `send_gif` / `send_sticker` consume directly.

- **Channel admin extension to tg-group-admin** — Five new
  subcommands raising the skill from 8 to 13: `participants`
  (paginated, with filter ∈ {all, admins, kicked, banned, bots,
  search}), `signatures` (broadcast channel author signing toggle —
  Telethon's keyword is `signatures_enabled`, not `enabled`),
  `slow-mode` (megagroup, with the exact Telegram-allowed slot set
  validated at the schema layer), `discussion` (bind/unbind a
  discussion megagroup to a broadcast), and `admin-log` (admin
  recent-events log; CHAT_ADMIN_REQUIRED if not admin).

### Added — enhancements to existing skills

- **tg-polls**: new `edit` subcommand. Refuses quiz polls (correct
  answers and solution live on InputMediaPoll, not Poll, and Telegram
  doesn't echo them back via GetMessages — silently editing a quiz
  would drop those fields). Refuses option-count changes since votes
  are tied to opaque option bytes.
- **tg-scheduling**: new `edit` subcommand mirroring
  /scheduled/send's full validation cascade — tz-aware,
  10 s ≤ Δ ≤ 365 days, plus a just-before-dispatch recheck so the
  request can't drift below the minimum during in-process delay.
- **tg-profile**: new `2fa` subcommand for cloud-password (set /
  change / remove). Passwords are read via getpass and **never
  appear on argv**; the helper refuses to run when stdin is not a
  TTY (getpass would silently degrade to plain reads). Audit logs
  only the transition kind ("set" / "change" / "remove"), never any
  password material.

### Changed — server-wide

- All client-side schema errors now return HTTP **400** with
  `{"error": "ValidationError", "detail": ...}`. FastAPI's default
  422 on body validation is mapped via a `RequestValidationError`
  exception handler, so the CLI / skill dispatchers / DaemonClient
  consumers can branch on a single status code.

### Tests

- 365 unit tests (up from 280 at v0.3.0). Each Codex review round
  in Phase 3 closed an issue with a regression test pinning the fix.

### Acknowledgments

Phase 3 ran 12 additional rounds of Codex review (gpt-5.4) on top of
the 30 Phase 1+2 already had — total 42 review iterations across the
project. Across the three phases combined: **9 BLOCKER + 50+ MAJOR +
30+ MINOR** issues found and fixed, every one of them with a pinned
test.


## [0.3.0] - 2026-04-28

Phase 2 complete: every skill from the original plan is now shipped.

### Added — four new skills

- **tg-media-upload** — Upload local files (photo / video / document /
  voice). Daemon validates the path against a layered defense:
  - Symlink rejection at the leaf AND in every parent component.
  - Realpath-based containment check rejecting any path inside the
    daemon's runtime directory.
  - File size hard cap at 2 GiB.
  - `_open_validated_upload` opens the file with
    `O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`, fstats it, compares
    `(st_dev, st_ino)` against the pre-open lstat to catch
    regular-file replacements between validate and open, and re-checks
    `S_ISREG` to defeat FIFO/device swaps. Telethon receives the file
    object — never re-resolves by path.
  - Dispatcher requires explicit confirmation: `--yes` OR matching
    `--confirm-chat` AND `--confirm-file` (typo-resistant double
    keystroke).
  - Audit log records `name + sha256(parent)[:8]`, never the absolute
    path.
- **tg-polls** — Create / close / read results. Anonymous, public,
  multiple-choice, and quiz modes. The result decoder builds an
  explicit `(option_bytes → answer_index)` map from the poll's own
  answers and looks up `r.option` by exact bytes — works for polls
  authored by other clients, not just ours. `close_poll` does
  `copy.copy(original_poll)` and only flips `closed=True` so optional
  metadata like `close_period` / `close_date` is preserved.
- **tg-scheduling** — Schedule, list, and cancel messages, plus draft
  save / get / clear. The schedule timestamp is validated as
  timezone-aware and ≥10 s / ≤365 days; the handler **re-checks the
  window** right before sending in case the request sat in process
  long enough to drift. `get_draft` filters out placeholder
  `DraftMessage` objects (chats with no real text and no `reply_to`)
  so callers don't mistake "user opened the chat" for "user has a
  saved draft".
- **tg-profile** — Update first/last name, bio, public username,
  profile photo, online status. Username regex
  (`^[a-zA-Z][a-zA-Z0-9_]{3,30}[a-zA-Z0-9]$`) forbids trailing
  underscores, matching Telegram's actual upstream rule.
  `delete_current_profile_photo` uses
  `client.get_profile_photos('me')` and `telethon.utils.get_input_photo`
  (Telethon does NOT expose a `get_full_user` helper). `update_profile`
  reads back the bio via `GetFullUserRequest` because `about/bio` is
  on `UserFull`, not the bare `User` from `get_me()`. Profile-photo
  upload reuses the same `_open_validated_upload` pipeline.
- **tg-export** — Export a chat's history to a caller-chosen directory
  as JSON, optionally with media. The first skill that legitimately
  accepts a caller-controlled write path; defenses are correspondingly
  thorough:
  - `_validate_export_dir` rejects symlinks, paths inside
    `RUNTIME_DIR` / `CONFIG_DIR` (realpath containment check),
    foreign-owned dirs, and missing dirs (no auto-mkdir).
  - `_open_validated_export_dir` opens the validated path with
    `O_DIRECTORY|O_NOFOLLOW`, fstat re-verifies, and hands a `dir_fd`
    to `TGSession.export_chat`. **Every subsequent filesystem op is
    relative to that fd** (`mkdir(name, dir_fd=)`,
    `os.open(name, ..., dir_fd=)`). No path is ever re-resolved after
    validation.
  - `chat_<peer_id>` and `media/` subdirs are mkdir'd only after an
    `os.stat(name, dir_fd=, follow_symlinks=False)` proves any
    pre-existing entry is a real owned directory (avoids the
    macOS/Linux errno divergence under `O_NOFOLLOW`).
  - `messages.json` opens `O_CREAT|O_EXCL|O_NOFOLLOW` — pre-existing
    file fails the open, never silent clobber.
  - Each media download streams into a file opened the same way and
    handed to Telethon as a file object.
  - `since_date` / `until_date` validated as timezone-aware and
    normalized to UTC; comparing naive to Telethon's UTC dates would
    `TypeError` at runtime.
  - Dispatcher uses the same double-keystroke confirmation pattern as
    media upload (`--yes` or `--confirm-chat` AND `--confirm-out-dir`).

### Changed

- `TGSession.send_media` and `TGSession.export_chat` signatures now
  take file-like objects / dir fds instead of path strings,
  eliminating the entire "Telethon reopens by name" TOCTOU class.

### Tests

- 280 unit tests (up from 168 at v0.2.0). Each Codex review across
  Phase 2 closed an issue with a regression test pinning the fix.

### Acknowledgments

This release ran 8 additional rounds of Codex review (gpt-5.4) on top
of the 22 rounds Phase 1 already had; total 30 rounds covering
8 BLOCKER / 40+ MAJOR / 20+ MINOR issues across the project.


## [0.2.0] - 2026-04-28

### Added — write-side coverage

- **tg-messaging extension**: `edit`, `delete`, `forward`, `pin`, `unpin`,
  `react`, `mark_read` daemon endpoints + a single subcommand dispatcher at
  `skills/tg-messaging/act.py`. Bilingual triggers and an explicit
  confirm-before-destructive checklist in `SKILL.md`.
- **tg-group-admin skill**: create groups (basic / megagroup / broadcast),
  add / kick / ban / unban members, generate invite links (with optional
  expiry and usage limit), rename, leave. Daemon endpoints under
  `/chat/*`. Refuses basic-group creation with no resolvable invitees so
  callers get a useful error instead of `UsersTooFewError`.
- **tg-contacts skill**: add (E.164 phone enforced at the schema layer),
  delete, block, unblock, search. `/contacts/search` filters Telethon's
  global search down to the user's actual contacts (and excludes bots) so
  a prompt-injected agent can't enumerate strangers under the "contacts"
  label.

### Added — runtime

- **Multi-account runtime switching** via `POST /accounts/switch`. Sessions
  are loaded lazily, cached in `state.sessions` keyed by label, and
  serialized per-label with an `asyncio.Lock` so two concurrent switches
  to the same cold label can't both call `_open_session` (the second
  assignment would otherwise orphan the first live `TGSession`).
  `tgmcp account use <label>` issues the RPC; `--passphrase` is a flag
  that triggers a hidden prompt (the secret never lands on argv).
  `tgmcp account list` now annotates `active` and `loaded` sessions when
  the daemon is running.
- **Unified account-status fields**: `/health`, `/accounts`, and
  `/accounts/switch` all return `active_label` + `loaded_labels` so
  clients see one shape across the API.

### Added — operations & verification

- `scripts/e2e_smoke.py`: interactive end-to-end script against a real
  (burner) Telegram account. Walks daemon-up → list dialogs → send →
  search → edit → delete (verified via post-delete search rather than
  trusting any count) → daemon-down. Refuses to run without
  `TGMCP_E2E_CONFIRM=yes`.

### Fixed

- `delete_messages` no longer interprets Telethon's
  `AffectedMessages.pts_count` as a per-message delete count (it's the
  updates-state delta). The HTTP contract is `{ok, requested}`; docs and
  the e2e script now emphasize that `revoke=True` is best-effort.
- `set_chat_title` no longer awaits a bound method object (a leftover
  debug line that crashed every call with `TypeError`). Pinned with a
  behavioral test that captures the issued Telethon request.
- Audit log records `account_switch` with the label only — the
  passphrase is wiped from the local frame and request body as soon as
  `auth.load_session` returns. `/contacts/add` audits only the last 4
  digits of the phone.

### Changed

- `state.session: TGSession` → `state.sessions: dict[str, TGSession]`
  with `state.active_label`. `_sess()` resolves through the active label.
  Every loaded session is stopped on lifespan teardown.

### Tests

- 168 unit tests (up from 125 at v0.1.0), CI runs lint + the full suite
  on Python 3.10 / 3.11 / 3.12 on every push.


## [0.1.0] - 2026-04-27

Initial release. Daemon + 8 always-loaded MCP search/read tools + 1
lazy-load skill (`tg-messaging` send-only). 22 rounds of code review,
50 issues fixed. See README.md for the architecture and security
properties at v0.1.0.
