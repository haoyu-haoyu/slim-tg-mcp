# Changelog

All notable changes to this project follow [Semantic Versioning](https://semver.org/).

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
